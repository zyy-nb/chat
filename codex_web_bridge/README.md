# ChatGPT 网页 ↔ 本机 Codex 桥接

这个目录提供一个安全的 MVP：ChatGPT 网页通过 MCP 调用工具，本机 MCP 服务再通过 Codex App Server 驱动桌面电脑上的 Codex。它支持本地项目任务、持续对话、状态/输出/diff、命令或文件变更审批，以及显式 Git 提交和 push。

## 运行

在仓库根目录执行：

```powershell
Copy-Item codex_web_bridge/config.example.json codex_web_bridge/config.json
& .codex-bridge-venv\Scripts\python.exe -m pip install -r codex_web_bridge\requirements.txt
$env:CODEX_BRIDGE_TOKEN = "请替换为随机长令牌"
& codex_web_bridge\run_server.ps1
```

复制后请先编辑 `config.json`，把 `projects.chat.path` 改成专用的 `chat` 仓库目录。不要填写包含无关数据的上级工作区目录。

服务地址为 `http://127.0.0.1:8000/mcp`。

ChatGPT 连接要求：在 ChatGPT 开发者模式中添加 MCP 连接。ChatGPT 端需要能访问 HTTPS MCP 地址；本地开发可以使用 OpenAI Secure MCP Tunnel 或其他安全 HTTPS 转发。不要把未认证的本地服务直接暴露到公网。

## 工具

- `list_projects`：列出配置中的项目名称。
- `execute_task`：在本机 Codex 中开始新任务，返回 `task_id`。
- `get_task_status`：读取进度、输出、diff、待审批请求和 Git 状态。
- `continue_task`：继续同一个本机 Codex thread。
- `approve_action`：批准或拒绝命令/文件变更。
- `cancel_task`：中断当前 turn。
- `publish_result`：创建分支、提交，并在 `push=true` 时推送到 GitHub。

## Git 安全策略

`publish_result` 默认不 push，并且默认只暂存已跟踪文件的修改。要发布新文件，必须通过 `paths` 传入相对路径；这样不会把工作区中原本就存在的飞行数据、输出文件或其他未跟踪文件误上传。

如果 `chat` 路径已经指向专用仓库，可以在 `config.json` 中将 `git.auto_push` 改为 `true`。每个完成的任务会自动使用 `codex/<task_id>` 分支提交并 push；当前工作区包含无关数据，因此示例默认关闭此选项。

例如：

```json
{
  "task_id": "task_123456789abc",
  "branch_name": "codex/update-report",
  "commit_message": "Update report",
  "paths": ["codex_web_bridge/server.py", "codex_web_bridge/README.md"],
  "push": true
}
```

## 设计边界

桥接服务只允许操作配置的项目目录，Codex 默认使用 `workspace-write` 沙箱和 `on-request` 审批策略。网页 ChatGPT 是任务编排端，本机 Codex 是执行端；这不是读取 `chatgpt.com` 内部 DOM，也不会把任意系统 shell 直接暴露给公网。
