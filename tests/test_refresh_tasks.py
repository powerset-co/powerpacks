"""Scheduled refresh creation keeps Codex runs in the creation chat.

Changelog:
  2026-10-08: verify the scheduled prompt runs the ask worker after refresh.
"""

import json
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tomllib
import unittest
from unittest.mock import MagicMock, patch

from packs.ingestion.primitives.refresh import tasks


class ScheduleTests(unittest.TestCase):
    def test_prompt_runs_ask_worker_after_refresh(self):
        command = "uv run --no-sync --project . python packs/ingestion/primitives/ask_worker/ask_worker.py run"
        self.assertIn(f"After the refresh, run `{command}`.", tasks.PROMPT)
        self.assertLess(tasks.PROMPT.index(tasks.REFRESH_COMMAND), tasks.PROMPT.index(command))
        self.assertNotIn("nothing else", tasks.PROMPT)

    def test_cadence_and_clock_time_roundtrip(self):
        for cadence, day in (("daily", "MO"), ("weekdays", "MO"), ("weekly", "SU")):
            schedule = tasks.Schedule(cadence, "14:45", day)
            self.assertEqual(tasks.Schedule.from_rrule(schedule.rrule()), schedule)

    def test_claude_install_writes_selected_cadence_and_preserves_other_tasks(self):
        for cadence, day, cron in (("daily", "MO", "30 9 * * *"), ("weekdays", "MO", "30 9 * * 1-5"), ("weekly", "SU", "30 9 * * 0")):
            with self.subTest(cadence=cadence), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                config = home / "scheduled-tasks.json"
                config.write_text(json.dumps({"scheduledTasks": [{"id": "unrelated"}]}))
                with patch.object(tasks, "_claude_task_files", return_value=[config]), \
                     patch.object(tasks, "CLAUDE_TASK", home / "skill/SKILL.md"), \
                     patch.object(tasks, "_claude_closed"):
                    tasks._install_claude(home, tasks.Schedule(cadence, "09:30", day))
                rows = json.loads(config.read_text())["scheduledTasks"]
                self.assertEqual(rows[0], {"id": "unrelated"})
                self.assertEqual(rows[1]["cronExpression"], cron)

    def test_invalid_time_and_nonlocal_timezone_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            tasks.Schedule(time="25:01")
        with patch.object(tasks, "_local_timezone", return_value="America/Los_Angeles"), \
             self.assertRaisesRegex(ValueError, "computer's timezone"):
            tasks.Schedule(timezone="America/New_York")


class CodexHeartbeatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.app_db = self.home / "codex-dev.db"
        self.threads_db = self.home / "state_5.sqlite"
        self.automation_id = "refresh-message-sources-2"
        self.thread_id = "00000000-0000-4000-8000-000000000001"
        self.rollout = self.home / "creation-chat.jsonl"
        self.rollout.write_text("")
        self.folder = self.home / "automations" / self.automation_id
        self.folder.mkdir(parents=True)
        config = {"version": 1, "id": self.automation_id, "kind": "heartbeat", "name": tasks.TASK_NAME,
                  "prompt": tasks.PROMPT, "status": "ACTIVE", "rrule": tasks.Schedule().rrule(),
                  "target_thread_id": self.thread_id, "created_at": 1, "updated_at": 1}
        (self.folder / "automation.toml").write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in config.items()))
        (self.folder / "memory.md").write_text("Past refresh result\n")
        with sqlite3.connect(self.app_db) as conn:
            conn.execute("CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT, prompt TEXT, kind TEXT, "
                         "status TEXT, target_thread_id TEXT, rrule TEXT, created_at INTEGER, updated_at INTEGER)")
            conn.execute("INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (self.automation_id, tasks.TASK_NAME, tasks.PROMPT, "heartbeat", "ACTIVE", self.thread_id,
                          tasks.Schedule().rrule(), 1, 1))
            conn.execute("INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         ("unrelated", "Other task", "Other prompt", "heartbeat", "ACTIVE", self.thread_id,
                          tasks.Schedule().rrule(), 1, 1))
        with sqlite3.connect(self.threads_db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, created_at_ms INTEGER, "
                         "rollout_path TEXT, thread_source TEXT, title TEXT, cwd TEXT, sandbox_policy TEXT, approval_mode TEXT)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (self.thread_id, 1000, str(self.rollout), "cli", "Refresh source schedule", str(self.home),
                          '{"type":"danger-full-access"}', "never"))
        for name, value in {"CODEX_HOME": self.home, "CODEX_APP_DB": self.app_db,
                            "CODEX_THREADS_DB": self.threads_db,
                            "CLAUDE_TASK": self.home / "claude-task/SKILL.md",
                            "CLAUDE_SESSIONS": self.home / "claude-sessions",
                            "CLAUDE_PROJECTS": self.home / "claude-projects"}.items():
            patcher = patch.object(tasks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        connect = sqlite3.connect
        remove = tasks.shutil.rmtree
        move = tasks.shutil.move
        popen = tasks.subprocess.Popen
        copy = tasks.shutil.copy2
        write = Path.write_text
        replace = Path.replace

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

        def safe_copy(source, destination, *args, **kwargs):
            inside_home(source)
            inside_home(destination)
            return copy(source, destination, *args, **kwargs)

        def safe_popen(*args, **kwargs):
            inside_home(kwargs["env"]["CODEX_HOME"])
            inside_home(kwargs["cwd"])
            return popen(*args, **kwargs)

        def safe_write(path, *args, **kwargs):
            inside_home(path)
            return write(path, *args, **kwargs)

        def safe_replace(path, target):
            inside_home(path)
            inside_home(target)
            return replace(path, target)

        for owner, name, function in ((tasks.sqlite3, "connect", safe_connect),
                                     (tasks.shutil, "rmtree", safe_remove),
                                     (tasks.shutil, "move", safe_move), (tasks.shutil, "copy2", safe_copy),
                                     (tasks.subprocess, "Popen", safe_popen)):
            patcher = patch.object(owner, name, side_effect=function)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, function in (("write_text", safe_write), ("replace", safe_replace)):
            patcher = patch.object(Path, name, new=function)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_mutations_cannot_leave_temporary_home(self):
        for mutation in (lambda: tasks.sqlite3.connect("/outside-test-home/database.sqlite"),
                         lambda: tasks.shutil.rmtree("/outside-test-home/folder"),
                         lambda: tasks.shutil.move(self.folder, "/outside-test-home/backup")):
            with self.assertRaisesRegex(AssertionError, "outside temporary home"):
                mutation()

    def test_schedule_update_preserves_native_id_and_chat_until_app_import(self):
        self.assertEqual(tasks.installed_runners(), ["codex"])
        self.assertEqual(tasks.read_task(self.home).codex_install_status, "installed")
        with patch.object(tasks.subprocess, "run") as opened, patch.object(tasks, "_create_codex_thread") as created:
            tasks.install("codex", self.home, tasks.Schedule("weekdays", "17:15"))
        created.assert_not_called()
        opened.assert_called_once_with(["open", f"codex://threads/{self.thread_id}"], check=True)
        result = tasks.read_task(self.home)
        self.assertEqual(result.codex_install_status, "pending")
        self.assertNotIn("codex", result.installs)
        self.assertEqual((result.schedule_settings.cadence, result.schedule_settings.time), ("weekdays", "17:15"))
        with sqlite3.connect(self.app_db) as conn:
            config = tomllib.loads((self.folder / "automation.toml").read_text())
            conn.execute("UPDATE automations SET rrule = ?, updated_at = ? WHERE id = ?",
                         (config["rrule"], config["updated_at"], self.automation_id))
        self.assertEqual(tasks.read_task(self.home).codex_install_status, "installed")

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_real_cli_persists_thread_before_pending_schedule_is_written(self):
        empty = self.home / "empty-codex-home"
        empty.mkdir()
        with patch.object(tasks, "CODEX_HOME", empty), \
             patch.object(tasks, "CODEX_APP_DB", empty / "sqlite/codex-dev.db"), \
             patch.object(tasks, "CODEX_THREADS_DB", empty / "state_5.sqlite"), \
             patch.object(tasks.subprocess, "run") as opened:
            tasks.install("codex", self.home, tasks.Schedule("weekly", "19:30", "FR"))
            result = tasks.read_task(self.home)
            self.assertEqual(result.codex_install_status, "pending")
            self.assertNotIn("codex", result.installs)
            config = tomllib.loads((empty / "automations" / tasks.TASK_ID / "automation.toml").read_text())
            with sqlite3.connect(f"file:{empty / 'state_5.sqlite'}?mode=ro", uri=True) as conn:
                thread = conn.execute("SELECT cwd, rollout_path, sandbox_policy, approval_mode FROM threads WHERE id = ?",
                                      (config["target_thread_id"],)).fetchone()
            self.assertEqual(thread[0], str(self.home))
            self.assertTrue(Path(thread[1]).is_file())
            self.assertIn(json.loads(thread[2])["type"], ("disabled", "danger-full-access"))
            self.assertEqual(thread[3], "never")
            self.assertEqual(config["kind"], "heartbeat")
            self.assertNotIn("archive", config["prompt"])
            opened.assert_called_once_with(["open", result.codex_thread_url], check=True)
            with patch.object(tasks, "_create_codex_thread") as created:
                tasks.install("codex", self.home, tasks.Schedule("daily", "06:45"))
            created.assert_not_called()

    def test_native_full_access_policies_are_accepted_and_other_permissions_rejected(self):
        for policy, approval, accepted in (("disabled", "never", True), ("danger-full-access", "never", True),
                                          ("read-only", "never", False), ("disabled", "on-request", False)):
            with self.subTest(policy=policy, approval=approval):
                with sqlite3.connect(self.threads_db) as conn:
                    conn.execute("UPDATE threads SET sandbox_policy = ?, approval_mode = ?",
                                 (json.dumps({"type": policy}), approval))
                if accepted:
                    tasks._verify_codex_thread(self.thread_id, self.home)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Full access"):
                        tasks._verify_codex_thread(self.thread_id, self.home)

    def test_subprocess_error_and_timeout_leave_configs_and_backups_untouched(self):
        before = {path: path.read_bytes() for path in self.folder.iterdir()}
        for ready in (True, False):
            process = MagicMock()
            process.__enter__.return_value = process
            process.stdin = io.BytesIO()
            process.stdout = io.BytesIO(b'{"id":0,"error":{"message":"Codex startup failed"}}\n')
            with self.subTest(ready=ready), patch.object(tasks, "_codex_config", return_value=None), \
                 patch.object(tasks.subprocess, "Popen", return_value=process), \
                 patch.object(tasks.subprocess, "run") as opened, \
                 patch.object(tasks.select, "select", return_value=([process.stdout] if ready else [], [], [])), \
                 self.assertRaisesRegex(RuntimeError, "Codex startup failed" if ready else "timed out"):
                tasks.install("codex", self.home)
            process.wait.assert_called_once_with(timeout=5)
            opened.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in self.folder.iterdir()}, before)
            self.assertFalse(list(self.home.glob("*.bkup")))
            self.assertFalse((self.home / "automations" / tasks.TASK_ID).exists())

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
