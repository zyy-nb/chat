import json
import tempfile
import unittest
from pathlib import Path

from codex_web_bridge.server import BridgeConfig, TaskManager, TaskState


class BridgeConfigTests(unittest.TestCase):
    def test_config_resolves_relative_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "default_project": "demo",
                        "projects": {"demo": {"path": "project"}},
                    }
                ),
                encoding="utf-8",
            )
            config = BridgeConfig.load(config_path)
            self.assertEqual(config.projects["demo"].path, project.resolve())


class GitSafetyTests(unittest.TestCase):
    def test_publish_rejects_path_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "default_project": "demo",
                        "projects": {"demo": {"path": "project"}},
                    }
                ),
                encoding="utf-8",
            )
            manager = TaskManager(BridgeConfig.load(config_path))
            # The path guard is exercised through the public publish method only
            # after a synthetic task is registered, avoiding a Codex process.
            project = manager.config.projects["demo"]
            state = TaskState("task_test", project, "test")
            manager.tasks[state.task_id] = state
            with self.assertRaises(RuntimeError):
                manager.publish(state.task_id, None, None, ["..\\outside.txt"], False)
            manager.codex.close()

    def test_codex_events_are_kept_on_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "default_project": "demo",
                        "projects": {"demo": {"path": "project"}},
                    }
                ),
                encoding="utf-8",
            )
            manager = TaskManager(BridgeConfig.load(config_path))
            state = TaskState("task_events", manager.config.projects["demo"], "test")
            state.thread_id = "thread-events"
            manager.tasks[state.task_id] = state
            manager.thread_to_task[state.thread_id] = state.task_id

            manager._on_codex_message(
                {
                    "method": "turn/started",
                    "params": {"threadId": state.thread_id, "turn": {"id": "turn-1"}},
                }
            )
            manager._on_codex_message(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": state.thread_id, "delta": "READY"},
                }
            )
            manager._on_codex_message(
                {
                    "method": "turn/diff/updated",
                    "params": {"threadId": state.thread_id, "diff": "diff --git a/a b/a"},
                }
            )
            manager._on_codex_message(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": state.thread_id,
                        "turn": {"id": "turn-1", "status": "completed", "items": []},
                    },
                }
            )

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.output, "READY")
            self.assertIn("diff --git", state.diff)
            manager.codex.close()


if __name__ == "__main__":
    unittest.main()
