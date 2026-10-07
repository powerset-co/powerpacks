"""Scheduled refresh creation keeps Codex runs in the creation chat."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from packs.ingestion.primitives.refresh import tasks


class RefreshTaskTests(unittest.TestCase):
    def test_codex_creation_uses_native_same_chat_schedule(self):
        repo = Path("/tmp/powerpacks")
        with patch.object(tasks.subprocess, "run") as opened:
            tasks._install_codex(repo)

        command = opened.call_args.args[0]
        self.assertEqual(command[0], "open")
        url = urlparse(command[1])
        self.assertEqual((url.scheme, url.netloc, url.path), ("codex", "threads", "/new"))
        request = parse_qs(url.query)["prompt"][0]
        self.assertIn("automation_update", request)
        self.assertIn("kind heartbeat", request)
        self.assertIn("destination thread", request)
        self.assertIn("attached to this chat", request)
        self.assertIn('"Start each run in new chat" OFF', request)
        self.assertIn("Keep this chat open", request)
        self.assertIn("daily at 6:00 AM", request)
        self.assertIn(str(repo), request)
        self.assertIn(tasks.REFRESH_COMMAND, request)
        self.assertIn("Do not run it now", request)
        self.assertNotIn("```toml", request)
        self.assertNotIn("cron", request)
        self.assertNotIn("archive", request)


class CodexHeartbeatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.app_db = self.home / "codex-dev.db"
        self.threads_db = self.home / "state_5.sqlite"
        self.automation_id = "refresh-message-sources-2"
        self.thread_id = "00000000-0000-4000-8000-000000000001"
        self.rollout = self.home / "creation-chat.jsonl"
        self.rollout.write_text("")
        self.folder = self.home / "automations" / self.automation_id
        self.folder.mkdir(parents=True)
        (self.folder / "automation.toml").write_text(f'id = "{self.automation_id}"\nkind = "heartbeat"\n')
        (self.folder / "memory.md").write_text("Past refresh result\n")
        with sqlite3.connect(self.app_db) as conn:
            conn.execute("CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT, prompt TEXT, kind TEXT, "
                         "status TEXT, target_thread_id TEXT)")
            conn.execute("INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?)",
                         (self.automation_id, tasks.TASK_NAME, tasks.PROMPT, "heartbeat", "ACTIVE", self.thread_id))
            conn.execute("INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?)",
                         ("unrelated", "Other task", "Other prompt", "heartbeat", "ACTIVE", self.thread_id))
        with sqlite3.connect(self.threads_db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, created_at_ms INTEGER, "
                         "rollout_path TEXT, thread_source TEXT, title TEXT)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                         (self.thread_id, 1000, str(self.rollout), "cli", "Refresh source schedule"))
        for name, value in {"CODEX_HOME": self.home, "CODEX_APP_DB": self.app_db,
                            "CODEX_THREADS_DB": self.threads_db,
                            "CLAUDE_SESSIONS": self.home / "claude-sessions",
                            "CLAUDE_PROJECTS": self.home / "claude-projects"}.items():
            patcher = patch.object(tasks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        connect = sqlite3.connect
        remove = tasks.shutil.rmtree
        move = tasks.shutil.move

        def inside_home(path):
            self.assertTrue(Path(str(path).removeprefix("file:").split("?")[0]).resolve().is_relative_to(self.home.resolve()),
                            f"Test attempted access outside temporary home: {path}")

        def safe_connect(path, *args, **kwargs):
            inside_home(path)
            return connect(path, *args, **kwargs)

        def safe_remove(path, *args, **kwargs):
            inside_home(path)
            return remove(path, *args, **kwargs)

        def safe_move(source, destination, *args, **kwargs):
            inside_home(source)
            inside_home(destination)
            return move(source, destination, *args, **kwargs)

        for owner, name, function in ((tasks.sqlite3, "connect", safe_connect),
                                     (tasks.shutil, "rmtree", safe_remove),
                                     (tasks.shutil, "move", safe_move)):
            patcher = patch.object(owner, name, side_effect=function)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_mutations_cannot_leave_temporary_home(self):
        for mutation in (lambda: tasks.sqlite3.connect("/outside-test-home/database.sqlite"),
                         lambda: tasks.shutil.rmtree("/outside-test-home/folder"),
                         lambda: tasks.shutil.move(self.folder, "/outside-test-home/backup")):
            with self.assertRaisesRegex(AssertionError, "outside temporary home"):
                mutation()

    def test_native_generated_id_prevents_duplicate_installation(self):
        self.assertEqual(tasks.installed_runners(), ["codex"])
        with patch.object(tasks.subprocess, "run") as opened:
            tasks.install("codex", self.home)
        opened.assert_not_called()

    def test_each_heartbeat_run_uses_creation_chat_and_ignores_other_turns(self):
        rows = []
        for turn_id, started, automation_id, summary in (
            ("creation", 1, None, "Created the schedule"),
            ("scheduled-1", 2, self.automation_id, "ok\ngmail: refreshed"),
            ("steering", 3, None, "Changed the schedule"),
            ("other-task", 4, "unrelated", "Other report"),
            ("scheduled-2", 5, self.automation_id, "NEEDS ATTENTION\ngmail: needs_you"),
        ):
            text = f"<heartbeat><automation_id>{automation_id}</automation_id></heartbeat>" if automation_id else "User prompt"
            rows.extend([
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id, "started_at": started}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id,
                 "started_at": started, "last_agent_message": summary}},
            ])
        self.rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

        result = tasks.read_task(self.home)
        self.assertEqual([run.id for run in result.runs], ["scheduled-2", "scheduled-1"])
        self.assertEqual([run.status for run in result.runs], ["failed", "ok"])
        self.assertEqual(result.runs[0].started_at, "1970-01-01T00:00:05Z")
        self.assertTrue(all(run.open_url == f"codex://threads/{self.thread_id}" for run in result.runs))
        self.assertTrue(all(run.resume_command == f"codex resume {self.thread_id}" for run in result.runs))
        with sqlite3.connect(self.threads_db) as conn:
            conn.execute("UPDATE threads SET thread_source = 'automation', title = ?",
                         (f"Automation ID: {tasks.TASK_ID}",))
        self.assertEqual([run.id for run in tasks.read_task(self.home).runs], ["scheduled-2", "scheduled-1"])

    def test_heartbeat_error_without_a_report_is_failed(self):
        rows = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "failed-run"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"text": f"<heartbeat><automation_id>{self.automation_id}</automation_id></heartbeat>"}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "failed-run", "started_at": 6,
                "last_agent_message": None, "error": {"message": "Selected model is at capacity."}}},
        ]
        self.rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = tasks.read_task(self.home).runs[0]
        self.assertEqual((run.status, run.summary), ("failed", "Selected model is at capacity."))

    def test_removal_resolves_native_id_and_preserves_chat_and_automation_data(self):
        original = self.rollout.read_bytes()
        tasks.uninstall("codex")

        self.assertEqual(tasks.installed_runners(), [])
        with sqlite3.connect(self.app_db) as conn:
            self.assertEqual(conn.execute("SELECT id FROM automations").fetchall(), [("unrelated",)])
        with sqlite3.connect(self.threads_db) as conn:
            self.assertEqual(conn.execute("SELECT id FROM threads").fetchall(), [(self.thread_id,)])
        self.assertEqual(self.rollout.read_bytes(), original)
        self.assertFalse(self.folder.exists())
        backups = list(self.home.glob(f"{self.automation_id}.*.bkup"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "memory.md").read_text(), "Past refresh result\n")


if __name__ == "__main__":
    unittest.main()
