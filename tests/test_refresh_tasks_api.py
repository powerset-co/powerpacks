"""Scheduled task routes validate schedule choices and expose install failures."""

import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode, urlparse

from packs.ingestion.primitives.refresh import api, tasks


class TasksApiTests(unittest.TestCase):
    def post(self, fields):
        body = urlencode(fields).encode()
        handler = MagicMock()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        route = api.TasksApi()
        self.assertTrue(route.post(handler, urlparse(api.INSTALL_PATH)))
        return route, handler, json.loads(handler.wfile.getvalue())

    @patch.object(tasks, "_local_timezone", return_value="America/Los_Angeles")
    def test_install_forwards_schedule_and_returns_pending_chat(self, _timezone):
        task = tasks.Task(tasks.TASK_ID, tasks.TASK_NAME, "Every Friday at 7:30 PM", tasks.REFRESH_COMMAND,
                          [], [], tasks.Schedule("weekly", "19:30", "FR"), "codex://threads/persisted", "pending")
        with patch.object(api, "install") as installed, patch.object(api, "read_task", return_value=task):
            route, handler, result = self.post({"runner": "codex", "cadence": "weekly", "time": "19:30",
                                               "day": "FR", "timezone": "America/Los_Angeles"})
        installed.assert_called_once_with("codex", route.repo, task.schedule_settings)
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(result["codex_install_status"], "pending")
        self.assertEqual(result["codex_thread_url"], "codex://threads/persisted")
        self.assertEqual(result["schedule_settings"]["time"], "19:30")

    @patch.object(tasks, "_local_timezone", return_value="America/Los_Angeles")
    def test_invalid_choices_fail_before_install(self, _timezone):
        for fields, error in (({"time": "25:00"}, "HH:MM"),
                              ({"timezone": "America/New_York"}, "computer's timezone"),
                              ({"cadence": "monthly"}, "daily, weekdays, or weekly")):
            with self.subTest(fields=fields), patch.object(api, "install") as installed:
                _, handler, result = self.post({"runner": "codex", **fields})
            installed.assert_not_called()
            handler.send_response.assert_called_once_with(400)
            self.assertIn(error, result["error"])

    def test_subprocess_failure_is_visible(self):
        with patch.object(api, "install", side_effect=RuntimeError("Codex timed out creating the scheduled task chat.")):
            _, handler, result = self.post({"runner": "codex"})
        handler.send_response.assert_called_once_with(500)
        self.assertEqual(result["error"], "Codex timed out creating the scheduled task chat.")


if __name__ == "__main__":
    unittest.main()
