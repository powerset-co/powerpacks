"""Import tools prepare binaries without login, syncing, or source data changes."""
from __future__ import annotations

import unittest
import os
from unittest.mock import patch

from packs.ingestion.primitives.discover.messages.wacli import binary
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.ingestion.primitives.setup.automations import msgvault_home, shell
from packs.powerset.primitives.install import tools


class ImportToolsTests(unittest.TestCase):
    def test_tool_discovery_keeps_the_selected_python_first(self):
        with patch.dict(os.environ, {"PATH": "/selected/python/bin:/usr/bin"}):
            tools.ImportTools(sources=()).run()
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], "/selected/python/bin")

    @patch.object(shell, "run_command")
    @patch.object(msgvault_home, "ensure_msgvault", return_value={"installed": True})
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", return_value="/fixture/bin/tool")
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_rerun_does_not_install_current_tools(self, _directory, _which, wacli, msgvault, run):
        for _ in range(2):
            self.assertEqual(tools.ImportTools(sources=("gmail", "whatsapp")).run()["status"], "ok")
        run.assert_not_called()
        self.assertEqual(msgvault.call_count, 2)
        self.assertEqual(wacli.call_count, 2)

    @patch.object(shell, "run_command")
    @patch.object(msgvault_home, "ensure_msgvault")
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", return_value="/fixture/bin/tool")
    def test_whatsapp_needs_no_gmail_browser_or_auth_tools(self, _which, _wacli, msgvault, run):
        self.assertEqual(tools.ImportTools(sources=("whatsapp",)).run()["status"], "ok")
        msgvault.assert_not_called()
        run.assert_not_called()

    @patch.object(binary, "ensure_wacli_report", side_effect=PrimitiveBlocked({"message": "Failed to download wacli"}))
    @patch("shutil.which", return_value="/fixture/bin/tool")
    def test_interrupted_wacli_download_is_failure_not_human_action(self, _which, _wacli):
        result = tools.ImportTools(sources=("whatsapp",)).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Failed to download", result["message"])

    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", return_value=None)
    def test_missing_homebrew_is_a_password_action(self, _which, _wacli):
        result = tools.ImportTools(sources=("whatsapp",)).run()
        self.assertEqual(result["status"], "needs_user_action")
        self.assertIn("password", result["message"])

    @patch.object(shell, "run_command", return_value=shell.CommandResult(ok=True))
    @patch.object(msgvault_home, "ensure_msgvault", return_value={"installed": True})
    @patch("shutil.which", side_effect=lambda name: None if name in {"node", "npm"} else "/fixture/bin/" + name)
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_node_and_npm_use_one_install(self, _directory, _which, _msgvault, run):
        self.assertEqual(tools.ImportTools(sources=("gmail",)).run()["status"], "ok")
        run.assert_called_once_with(["/fixture/bin/brew", "install", "node"], timeout=900)

    @patch.object(shell, "run_command", return_value=shell.CommandResult(ok=False, stderr="download interrupted"))
    @patch.object(binary, "ensure_wacli_report", return_value={"status": "ok", "action": "current"})
    @patch("shutil.which", side_effect=lambda name: None if name == "qrencode" else "/fixture/bin/" + name)
    def test_brew_download_failure_does_not_send_user_to_terminal(self, _which, _wacli, _run):
        result = tools.ImportTools(sources=("whatsapp",)).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("download interrupted", result["message"])


if __name__ == "__main__":
    unittest.main()
