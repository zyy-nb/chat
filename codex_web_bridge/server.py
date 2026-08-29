"""ChatGPT MCP bridge for a local Codex App Server.

The bridge intentionally exposes a small, allow-listed tool surface instead of
turning the local machine into an unrestricted remote shell.  ChatGPT calls the
MCP tools; this process starts/controls Codex App Server over JSONL stdio and
returns structured task state, output, diffs, approvals, and Git information.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from mcp.server.fastmcp import FastMCP
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    import uvicorn
except ImportError as exc:  # pragma: no cover - exercised during setup errors
    raise SystemExit(
        "Missing runtime dependencies. Install them with: "
        "python -m pip install -r codex_web_bridge/requirements.txt"
    ) from exc


MAX_EVENT_HISTORY = 120
MAX_OUTPUT_CHARS = 24_000
APP_NAME = "chatgpt-local-codex-bridge"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trim_text(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


class BridgeError(RuntimeError):
    """A user-facing bridge error."""


class RpcError(BridgeError):
    """A JSON-RPC error returned by Codex App Server."""


@dataclass
class PendingRpc:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    path: Path
    remote: str = "origin"
    default_branch: str = "main"


@dataclass
class BridgeConfig:
    projects: dict[str, ProjectConfig]
    default_project: str
    codex_command: str = "codex.cmd"
    approval_policy: str = "on-request"
    sandbox: str = "workspace-write"
    timeout_seconds: int = 900
    max_output_chars: int = MAX_OUTPUT_CHARS
    require_token: bool = False

    @classmethod
    def load(cls, config_path: Path | None) -> "BridgeConfig":
        raw: dict[str, Any] = {}
        if config_path is not None:
            config_path = config_path.expanduser().resolve()
            if not config_path.is_file():
                raise BridgeError(f"配置文件不存在: {config_path}")
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            base_dir = config_path.parent
        else:
            base_dir = Path.cwd()

        env_root = os.environ.get("CODEX_BRIDGE_PROJECT_ROOT")
        default_root = Path(env_root).expanduser() if env_root else base_dir
        projects_raw = raw.get("projects") or {
            "default": {"path": str(default_root)}
        }

        projects: dict[str, ProjectConfig] = {}
        for name, entry in projects_raw.items():
            if not isinstance(entry, dict):
                raise BridgeError(f"项目配置必须是对象: {name}")
            path_value = entry.get("path")
            if not path_value:
                raise BridgeError(f"项目缺少 path: {name}")
            path = Path(str(path_value)).expanduser()
            if not path.is_absolute():
                path = (base_dir / path).resolve()
            else:
                path = path.resolve()
            projects[name] = ProjectConfig(
                name=name,
                path=path,
                remote=str(entry.get("remote", "origin")),
                default_branch=str(entry.get("default_branch", "main")),
            )

        default_project = str(raw.get("default_project") or next(iter(projects)))
        if default_project not in projects:
            raise BridgeError(f"default_project 未配置: {default_project}")

        codex = raw.get("codex") or {}
        security = raw.get("security") or {}
        return cls(
            projects=projects,
            default_project=default_project,
            codex_command=str(codex.get("command", "codex.cmd")),
            approval_policy=str(codex.get("approval_policy", "on-request")),
            sandbox=str(codex.get("sandbox", "workspace-write")),
            timeout_seconds=int(codex.get("timeout_seconds", 900)),
            max_output_chars=int(codex.get("max_output_chars", MAX_OUTPUT_CHARS)),
            require_token=bool(security.get("require_token", False)),
        )


def safe_project(config: BridgeConfig, name: str | None) -> ProjectConfig:
    project_name = name or config.default_project
    try:
        project = config.projects[project_name]
    except KeyError as exc:
        choices = ", ".join(sorted(config.projects))
        raise BridgeError(f"未知项目 {project_name!r}，可用项目: {choices}") from exc
    if not project.path.is_dir():
        raise BridgeError(f"项目目录不存在: {project.path}")
    return project


class CodexAppServer:
    """Small JSONL client for the local Codex App Server."""

    def __init__(
        self,
        command: str,
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        self.command = command
        self.on_message = on_message
        self.process: subprocess.Popen[bytes] | None = None
        self.pending: dict[str, PendingRpc] = {}
        self.request_counter = 0
        self.send_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.initialized = False
        self.stderr_lines: list[str] = []

    def _build_command(self) -> list[str]:
        configured = os.environ.get("CODEX_BRIDGE_CODEX_BIN", self.command)
        parts = shlex.split(configured, posix=False)
        if not parts:
            raise BridgeError("Codex 命令为空")

        executable = parts[0]
        resolved = shutil.which(executable) or executable
        args = parts[1:] + ["app-server", "--stdio"]

        # Windows cannot execute .cmd directly through CreateProcess.  Calling
        # the batch shim via ComSpec keeps the bridge compatible with npm's
        # global Codex installation while avoiding PowerShell profile effects.
        if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
            command_line = "call " + subprocess.list2cmdline([resolved, *args])
            return [os.environ.get("ComSpec", "cmd.exe"), "/d", "/s", "/c", command_line]
        return [resolved, *args]

    def _ensure_started(self) -> None:
        with self.start_lock:
            with self.state_lock:
                if (
                    self.process is not None
                    and self.process.poll() is None
                    and self.initialized
                ):
                    return
                command = self._build_command()
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                try:
                    self.process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=creationflags,
                    )
                    self.initialized = False
                except OSError as exc:
                    raise BridgeError(f"无法启动 Codex App Server: {command}: {exc}") from exc

                assert self.process.stdout is not None
                assert self.process.stderr is not None
                threading.Thread(
                    target=self._read_stdout,
                    args=(self.process.stdout,),
                    name="codex-app-server-stdout",
                    daemon=True,
                ).start()
                threading.Thread(
                    target=self._read_stderr,
                    args=(self.process.stderr,),
                    name="codex-app-server-stderr",
                    daemon=True,
                ).start()

            # The first request is deliberately outside state_lock: the reader
            # thread must be able to deliver the response while we wait.
            try:
                self.request(
                    "initialize",
                    {
                        "clientInfo": {"name": APP_NAME, "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=20,
                    ensure_started=False,
                )
                self.notify("initialized", {})
                self.initialized = True
            except Exception:
                self.close()
                raise

    def _read_stdout(self, stream: Any) -> None:
        for raw_line in iter(stream.readline, b""):
            try:
                message = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and "method" not in message:
                pending = self.pending.get(str(message["id"]))
                if pending is not None:
                    if "error" in message:
                        pending.error = message["error"]
                    else:
                        pending.result = message.get("result") or {}
                    pending.event.set()
                continue
            try:
                self.on_message(message)
            except Exception as exc:  # pragma: no cover - defensive isolation
                self.stderr_lines.append(f"notification handler error: {exc}")

        # Wake callers if Codex exits while a request is pending.
        for pending in list(self.pending.values()):
            pending.error = {"message": "Codex App Server 已退出"}
            pending.event.set()
        self.initialized = False

    def _read_stderr(self, stream: Any) -> None:
        for raw_line in iter(stream.readline, b""):
            text = raw_line.decode("utf-8", errors="replace").strip()
            if text:
                self.stderr_lines.append(text[-2000:])
                del self.stderr_lines[:-40]

    def _send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                raise BridgeError("Codex App Server 未运行")
            payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            process.stdin.write(payload)
            process.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30,
        ensure_started: bool = True,
    ) -> dict[str, Any]:
        if ensure_started:
            self._ensure_started()
        with self.state_lock:
            self.request_counter += 1
            request_id = self.request_counter
            pending = PendingRpc()
            self.pending[str(request_id)] = pending
        try:
            self._send({"method": method, "id": request_id, "params": params})
            if not pending.event.wait(timeout):
                raise BridgeError(f"Codex 请求超时: {method}")
            if pending.error is not None:
                raise RpcError(f"Codex 请求失败 {method}: {pending.error}")
            return pending.result or {}
        finally:
            self.pending.pop(str(request_id), None)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def respond(
        self,
        request_id: Any,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        self._send(message)

    def close(self) -> None:
        with self.state_lock:
            process = self.process
            self.process = None
            self.initialized = False
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


@dataclass
class TaskState:
    task_id: str
    project: ProjectConfig
    task_text: str
    thread_id: str | None = None
    turn_id: str | None = None
    status: str = "queued"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    output: str = ""
    diff: str = ""
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)

    def add_output(self, text: str) -> None:
        if text:
            self.output = trim_text(self.output + text)
            self.updated_at = now_iso()

    def add_event(self, method: str, params: dict[str, Any]) -> None:
        self.events.append({"method": method, "params": params})
        del self.events[:-MAX_EVENT_HISTORY]
        self.updated_at = now_iso()

    def as_dict(self, manager: "TaskManager") -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project.name,
            "cwd": str(self.project.path),
            "task": self.task_text,
            "status": self.status,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "output": trim_text(self.output, manager.config.max_output_chars),
            "diff": trim_text(self.diff, manager.config.max_output_chars),
            "error": self.error,
            "pending_approvals": [
                {key: value for key, value in approval.items() if key != "_rpc_id"}
                for approval in self.pending_approvals.values()
            ],
            "git": manager.git_state(self.project),
        }


class TaskManager:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.tasks: dict[str, TaskState] = {}
        self.thread_to_task: dict[str, str] = {}
        self.codex = CodexAppServer(config.codex_command, self._on_codex_message)
        atexit.register(self.codex.close)

    def _task_for_thread(self, thread_id: str | None) -> TaskState | None:
        if not thread_id:
            return None
        task_id = self.thread_to_task.get(str(thread_id))
        return self.tasks.get(task_id) if task_id else None

    def _on_codex_message(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return
        with self.lock:
            task = self._task_for_thread(params.get("threadId"))
            if task is None:
                return

            if "id" in message and (
                method.endswith("requestApproval")
                or method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}
            ):
                request_id = str(message["id"])
                task.pending_approvals[request_id] = {
                    "_rpc_id": message["id"],
                    "request_id": request_id,
                    "kind": method,
                    "thread_id": task.thread_id,
                    "turn_id": task.turn_id,
                    "item_id": params.get("itemId"),
                    "reason": params.get("reason"),
                    "command": params.get("command"),
                    "cwd": params.get("cwd"),
                    "available_decisions": params.get("availableDecisions"),
                    "created_at": now_iso(),
                }
                task.status = "needs_approval"
                task.add_event(method, params)
                return

            task.add_event(method, params)
            if method == "turn/started":
                turn = params.get("turn") or {}
                task.turn_id = str(turn.get("id")) if turn.get("id") else task.turn_id
                task.status = "running"
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                turn_status = str(turn.get("status", "completed"))
                task.status = {
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(turn_status, turn_status)
                task.error = (turn.get("error") or {}).get("message") if isinstance(turn, dict) else None
                self._capture_items(task, turn.get("items", []) if isinstance(turn, dict) else [])
                task.done.set()
            elif method == "turn/diff/updated":
                task.diff = trim_text(str(params.get("diff", "")))
            elif method == "item/agentMessage/delta":
                task.add_output(str(params.get("delta", "")))
            elif method == "item/commandExecution/outputDelta":
                task.add_output(str(params.get("delta", "")))
            elif method == "item/completed":
                self._capture_items(task, [params.get("item") or {}])
            elif method == "serverRequest/resolved":
                request_id = str(params.get("requestId", ""))
                task.pending_approvals.pop(request_id, None)
                if not task.pending_approvals and task.status == "needs_approval":
                    task.status = "running"

    def _capture_items(self, task: TaskState, items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text and text not in task.output:
                    task.add_output("\n" + text)
            elif kind == "commandExecution":
                output = item.get("aggregatedOutput")
                if isinstance(output, str):
                    task.add_output("\n" + output)
            elif kind == "fileChange":
                changes = item.get("changes")
                if isinstance(changes, list):
                    paths = [str(change.get("path")) for change in changes if isinstance(change, dict)]
                    if paths:
                        task.add_output("\nChanged files: " + ", ".join(paths))

    def _start_thread(self, task: TaskState, model: str | None) -> None:
        params: dict[str, Any] = {
            "cwd": str(task.project.path),
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            "runtimeWorkspaceRoots": [str(task.project.path)],
            "ephemeral": False,
        }
        if model:
            params["model"] = model
        response = self.codex.request("thread/start", params, timeout=30)
        thread = response.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise BridgeError(f"Codex 未返回 thread id: {response}")
        task.thread_id = str(thread_id)
        self.thread_to_task[task.thread_id] = task.task_id

    def _start_turn(self, task: TaskState, instruction: str) -> None:
        if not task.thread_id:
            raise BridgeError("任务尚未建立 Codex thread")
        response = self.codex.request(
            "turn/start",
            {
                "threadId": task.thread_id,
                "input": [{"type": "text", "text": instruction}],
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(task.project.path)],
                    "networkAccess": False,
                },
            },
            timeout=30,
        )
        turn = response.get("turn") or {}
        if turn.get("id"):
            task.turn_id = str(turn["id"])
        task.status = "running"

    def _wait_for_turn(self, task: TaskState, timeout_seconds: int) -> None:
        if task.done.wait(max(1, timeout_seconds)):
            return
        with self.lock:
            if task.status == "running":
                task.status = "timeout"
            task.error = f"超过等待时间 {timeout_seconds} 秒；可稍后调用 get_task_status 查询。"

    def start_task(
        self,
        instruction: str,
        project_name: str | None,
        timeout_seconds: int | None,
        model: str | None,
    ) -> dict[str, Any]:
        instruction = instruction.strip()
        if not instruction:
            raise BridgeError("task 不能为空")
        project = safe_project(self.config, project_name)
        task = TaskState(
            task_id="task_" + uuid.uuid4().hex[:12],
            project=project,
            task_text=instruction,
        )
        with self.lock:
            self.tasks[task.task_id] = task
        try:
            self._start_thread(task, model)
            self._start_turn(task, instruction)
            self._wait_for_turn(task, timeout_seconds or self.config.timeout_seconds)
        except Exception as exc:
            with self.lock:
                task.status = "failed"
                task.error = str(exc)
                task.done.set()
        return task.as_dict(self)

    def continue_task(
        self,
        task_id: str,
        instruction: str,
        timeout_seconds: int | None,
    ) -> dict[str, Any]:
        task = self.require_task(task_id)
        if task.status in {"running", "needs_approval"}:
            raise BridgeError("任务仍在运行中；请先查询状态或处理审批")
        task.task_text = instruction.strip()
        task.status = "queued"
        task.error = None
        task.done.clear()
        try:
            self._start_turn(task, instruction)
            self._wait_for_turn(task, timeout_seconds or self.config.timeout_seconds)
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.done.set()
        return task.as_dict(self)

    def require_task(self, task_id: str) -> TaskState:
        with self.lock:
            task = self.tasks.get(task_id)
        if task is None:
            raise BridgeError(f"任务不存在: {task_id}")
        return task

    def status(self, task_id: str) -> dict[str, Any]:
        return self.require_task(task_id).as_dict(self)

    def approve(self, task_id: str, request_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
            raise BridgeError("decision 必须是 accept、acceptForSession、decline 或 cancel")
        task = self.require_task(task_id)
        approval = task.pending_approvals.get(str(request_id))
        if approval is None:
            raise BridgeError(f"审批请求不存在或已处理: {request_id}")
        if not approval["kind"].endswith(
            ("commandExecution/requestApproval", "fileChange/requestApproval")
        ):
            raise BridgeError(f"暂不支持的审批类型: {approval['kind']}")
        try:
            self.codex.respond(approval["_rpc_id"], {"decision": decision})
        except Exception as exc:
            raise BridgeError(f"发送审批决定失败: {exc}") from exc
        task.pending_approvals.pop(str(request_id), None)
        task.status = "running"
        return task.as_dict(self)

    def respond_to_request(
        self, task_id: str, request_id: str, response: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer a non-approval server request such as user input."""
        task = self.require_task(task_id)
        approval = task.pending_approvals.get(str(request_id))
        if approval is None:
            raise BridgeError(f"请求不存在或已处理: {request_id}")
        if approval["kind"].endswith("requestApproval"):
            raise BridgeError("审批请求请使用 approve_action")
        self.codex.respond(approval["_rpc_id"], response)
        task.pending_approvals.pop(str(request_id), None)
        if not task.pending_approvals and task.status == "needs_approval":
            task.status = "running"
        return task.as_dict(self)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.require_task(task_id)
        if not task.thread_id or not task.turn_id:
            raise BridgeError("任务尚未产生可取消的 turn")
        self.codex.request(
            "turn/interrupt",
            {"threadId": task.thread_id, "turnId": task.turn_id},
            timeout=15,
        )
        task.status = "cancelled"
        task.done.set()
        return task.as_dict(self)

    def git_command(self, project: ProjectConfig, args: list[str]) -> tuple[int, str, str]:
        result = subprocess.run(
            ["git", *args],
            cwd=project.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def git_state(self, project: ProjectConfig) -> dict[str, Any]:
        code, status, error = self.git_command(project, ["status", "--short", "--branch"])
        branch_code, branch, branch_error = self.git_command(project, ["branch", "--show-current"])
        if code != 0:
            return {"available": False, "error": error or "不是 Git 仓库"}
        return {
            "available": True,
            "branch": branch if branch_code == 0 else "",
            "status": status,
            "error": branch_error or None,
        }

    def publish(
        self,
        task_id: str,
        branch_name: str | None,
        commit_message: str | None,
        paths: list[str] | None,
        push: bool,
    ) -> dict[str, Any]:
        task = self.require_task(task_id)
        project = task.project
        branch = branch_name or f"codex/{task_id}"
        if not branch or branch.startswith("-") or any(ch.isspace() for ch in branch):
            raise BridgeError("branch_name 含有非法字符")

        # Explicitly avoid `git add -A` by default: this workspace contains
        # existing user data that must not be uploaded accidentally.
        root = project.path.resolve()
        if paths:
            safe_paths: list[str] = []
            for value in paths:
                candidate = (root / value).resolve()
                if candidate != root and root not in candidate.parents:
                    raise BridgeError(f"发布路径超出项目目录: {value}")
                safe_paths.append(str(candidate.relative_to(root)))
            add_args = ["add", "--", *safe_paths]
        else:
            add_args = ["add", "-u"]

        current_code, current_branch, current_error = self.git_command(project, ["branch", "--show-current"])
        if current_code != 0:
            raise BridgeError(current_error or "当前目录不是 Git 仓库")
        if current_branch != branch:
            exists_code, _, _ = self.git_command(project, ["show-ref", "--verify", f"refs/heads/{branch}"])
            switch_args = ["switch", branch] if exists_code == 0 else ["switch", "-c", branch]
            switch_code, _, switch_error = self.git_command(project, switch_args)
            if switch_code != 0:
                raise BridgeError(switch_error or f"无法切换分支: {branch}")

        add_code, _, add_error = self.git_command(project, add_args)
        if add_code != 0:
            raise BridgeError(add_error or "git add 失败")

        diff_code, _, diff_error = self.git_command(project, ["diff", "--cached", "--quiet"])
        committed = False
        commit_output = ""
        if diff_code == 1:
            message = (commit_message or f"Complete {task_id}").strip()
            commit_code, commit_output, commit_error = self.git_command(project, ["commit", "-m", message])
            if commit_code != 0:
                raise BridgeError(commit_error or "git commit 失败")
            committed = True
        elif diff_code not in {0}:
            raise BridgeError(diff_error or "无法检查暂存区")

        pushed = False
        push_output = ""
        if push:
            push_code, push_output, push_error = self.git_command(
                project, ["push", "-u", project.remote, branch]
            )
            if push_code != 0:
                raise BridgeError(push_error or "git push 失败")
            pushed = True

        return {
            "task_id": task_id,
            "project": project.name,
            "branch": branch,
            "remote": project.remote,
            "committed": committed,
            "pushed": pushed,
            "commit_output": commit_output,
            "push_output": push_output,
            "git": self.git_state(project),
        }


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Optional shared-token guard for the local MCP endpoint."""

    def __init__(self, app: Any, expected_token: str) -> None:
        super().__init__(app)
        self.expected_token = expected_token

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not supplied or not secrets.compare_digest(supplied, self.expected_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_server(manager: TaskManager) -> FastMCP:
    mcp = FastMCP(
        "Local Codex Bridge",
        instructions=(
            "Use this plugin when the user asks to operate on a configured local project. "
            "Start with execute_task for a new task, then use the returned task_id. "
            "Use get_task_status for progress, approve_action for explicit approvals, "
            "continue_task for follow-ups, and publish_result only after the user wants "
            "a Git commit or push. Never invent project paths or approval ids."
        ),
        host="127.0.0.1",
        port=8000,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )

    @mcp.tool()
    def list_projects() -> dict[str, Any]:
        """List configured local projects without exposing unrelated filesystem paths."""
        return {
            "projects": [
                {
                    "name": project.name,
                    "default_branch": project.default_branch,
                    "remote": project.remote,
                }
                for project in manager.config.projects.values()
            ],
            "default_project": manager.config.default_project,
        }

    @mcp.tool()
    def execute_task(
        task: str,
        project: str | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Run a new instruction in local Codex against a configured project."""
        if timeout_seconds is not None and not 1 <= timeout_seconds <= 3600:
            raise BridgeError("timeout_seconds 必须在 1 到 3600 之间")
        return manager.start_task(task, project, timeout_seconds, model)

    @mcp.tool()
    def get_task_status(task_id: str) -> dict[str, Any]:
        """Get current status, output, diff, approvals, and Git state for a task."""
        return manager.status(task_id)

    @mcp.tool()
    def continue_task(
        task_id: str,
        instruction: str,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Continue a completed local Codex conversation with a follow-up instruction."""
        if timeout_seconds is not None and not 1 <= timeout_seconds <= 3600:
            raise BridgeError("timeout_seconds 必须在 1 到 3600 之间")
        return manager.continue_task(task_id, instruction, timeout_seconds)

    @mcp.tool()
    def approve_action(task_id: str, request_id: str, decision: str) -> dict[str, Any]:
        """Approve or decline a pending Codex command/file-change request."""
        return manager.approve(task_id, request_id, decision)

    @mcp.tool()
    def cancel_task(task_id: str) -> dict[str, Any]:
        """Interrupt an active Codex turn."""
        return manager.cancel(task_id)

    @mcp.tool()
    def respond_to_request(
        task_id: str, request_id: str, response: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer a pending Codex user-input or elicitation request."""
        return manager.respond_to_request(task_id, request_id, response)

    @mcp.tool()
    def publish_result(
        task_id: str,
        branch_name: str | None = None,
        commit_message: str | None = None,
        paths: list[str] | None = None,
        push: bool = False,
    ) -> dict[str, Any]:
        """Commit selected task changes and optionally push them to the configured remote.

        By default only tracked modifications are staged. Pass explicit relative
        paths when new files should be included; this prevents accidental upload
        of pre-existing untracked data in a project directory.
        """
        return manager.publish(task_id, branch_name, commit_message, paths, push)

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChatGPT MCP bridge for local Codex")
    parser.add_argument("--config", type=Path, help="Path to config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = BridgeConfig.load(args.config)
    token = os.environ.get("CODEX_BRIDGE_TOKEN", "").strip()
    if config.require_token and not token:
        raise SystemExit("配置要求 CODEX_BRIDGE_TOKEN，但环境变量为空")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise SystemExit("非本机监听必须设置 CODEX_BRIDGE_TOKEN")

    manager = TaskManager(config)
    mcp = create_server(manager)
    app = mcp.streamable_http_app()
    if token:
        app.add_middleware(BearerTokenMiddleware, expected_token=token)

    print(f"Local Codex Bridge listening on http://{args.host}:{args.port}/mcp", flush=True)
    if not token:
        print("Warning: no bearer token; keep this endpoint on localhost or behind Secure MCP Tunnel.", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
