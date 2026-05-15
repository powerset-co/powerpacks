import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, rel):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


accounts = load_module("accounts_helper", "packs/ingestion/accounts.py")
account_registry = load_module("account_registry", "packs/ingestion/primitives/account_registry/account_registry.py")
onboarding = load_module("onboarding", "packs/ingestion/primitives/onboarding/onboarding.py")
linkedin_mcp_import = load_module("linkedin_mcp_import", "packs/ingestion/primitives/linkedin_mcp_import/linkedin_mcp_import.py")


class IngestionAccountsOnboardingTests(unittest.TestCase):
    def invoke(self, module, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = module.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def advance_to_linkedin_csv(self, accounts_path, ledger):
        code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
        self.assertEqual(code, 0)
        code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "skip"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["step"], "gmail")
        code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "skip"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["step"], "linkedin_csv")
        return payload

    def test_account_registry_init_and_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            code, payload = self.invoke(account_registry, ["init", "--path", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(path.exists())
            self.assertEqual(payload["status"], "initialized")
            code, payload = self.invoke(account_registry, [
                "mark", "--path", str(path), "--channel", "twitter", "--username", "alice", "--success",
            ])
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["twitter"]["linked"])
            self.assertEqual(registry["accounts"]["twitter"]["usernames"], ["alice"])

    def test_onboarding_plan_uses_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            accounts.save_registry(accounts.default_registry(), path)
            accounts.update_channel("gmail", path=path, username="me@example.com", success=True)
            code, payload = self.invoke(onboarding, ["plan", "--accounts", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("todo", payload)
            self.assertNotIn("gmail", [item["channel"] for item in payload["todo"]])

    def test_conversational_run_and_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_user_input")
            self.assertEqual(payload["step"], "messages")
            code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "skip"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "gmail")
            code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_user_action")
            self.assertEqual(payload["step"], "gmail")
            self.assertEqual(payload["url"], "https://search.powerset.dev/gmail/connect")
            self.assertIn("--timeout-seconds 600", payload["command"])
            self.assertNotIn("--no-wait", payload["command"])

    def test_messages_yes_advances_when_contacts_already_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "messages")
            with mock.patch.object(onboarding, "artifact_exists", return_value=True):
                code, payload = self.invoke(onboarding, [
                    "continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "gmail")
            self.assertEqual(payload["completed_action"]["message"], "Messages contacts already imported.")
            registry = accounts.load_registry(accounts_path)
            self.assertTrue(registry["accounts"]["messages"]["linked"])

    def test_messages_yes_returns_agent_action_when_import_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
            self.assertEqual(code, 0)
            with mock.patch.object(onboarding, "artifact_exists", return_value=False):
                code, payload = self.invoke(onboarding, [
                    "continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_agent_action")
            self.assertEqual(payload["step"], "messages")
            self.assertIn("import_contacts_pipeline.py run", payload["command"])

    def test_linkedin_csv_yes_opens_archive_page_before_csv_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            self.advance_to_linkedin_csv(accounts_path, ledger)
            code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_user_action")
            self.assertEqual(payload["step"], "linkedin_csv")
            self.assertEqual(payload["url"], onboarding.LINKEDIN_DATA_ARCHIVE_URL)
            self.assertIn("open-linkedin-archive", payload["command"])
            self.assertIn("larger data archive", payload["archive_option"])
            self.assertEqual(payload["drop_path"], str(accounts_path.parent / "linkedin" / "Connections.csv"))
            self.assertEqual(payload["scan_reply"], "scan")
            self.assertIn("open-downloads", payload["open_downloads_command"])
            self.assertIn("open-linkedin-drop-folder", payload["open_drop_folder_command"])
            state = onboarding.load_run(ledger)
            self.assertEqual(state["phase"], "awaiting_csv_path")

    def test_linkedin_csv_scan_downloads_copies_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            downloads = Path(tmp) / "Downloads"
            downloads.mkdir()
            (downloads / "Connections.csv").write_text("First Name,Last Name\nAda,Lovelace\n", encoding="utf-8")
            self.advance_to_linkedin_csv(accounts_path, ledger)
            code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes"])
            self.assertEqual(code, 0)
            with mock.patch.object(onboarding, "default_downloads_dir", return_value=downloads):
                code, payload = self.invoke(onboarding, [
                    "continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "scan",
                ])
            self.assertEqual(code, 0)
            copied_to = accounts_path.parent / "linkedin" / "Connections.csv"
            self.assertTrue(copied_to.exists())
            self.assertEqual(payload["completed_action"]["artifact"], str(copied_to))
            self.assertEqual(payload["scan"]["status"], "copied")
            self.assertEqual(payload["step"], "linkedin_mcp")
            registry = accounts.load_registry(accounts_path)
            self.assertTrue(registry["accounts"]["linkedin_csv"]["linked"])

    def test_open_linkedin_archive_command(self):
        with mock.patch.object(onboarding.webbrowser, "open", return_value=True) as open_mock:
            code, payload = self.invoke(onboarding, ["open-linkedin-archive"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["opened"])
        self.assertEqual(payload["url"], onboarding.LINKEDIN_DATA_ARCHIVE_URL)
        open_mock.assert_called_once_with(onboarding.LINKEDIN_DATA_ARCHIVE_URL)

    def test_find_linkedin_connections_extracts_zip_to_repo_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            downloads = Path(tmp) / "Downloads"
            downloads.mkdir()
            with zipfile.ZipFile(downloads / "Basic_LinkedInDataExport.zip", "w") as archive:
                archive.writestr("Complete/Connections.csv", "First Name,Last Name\nGrace,Hopper\n")
            code, payload = self.invoke(onboarding, [
                "find-linkedin-connections",
                "--accounts", str(accounts_path),
                "--downloads", str(downloads),
                "--copy-to-repo",
            ])
            self.assertEqual(code, 0)
            copied_to = accounts_path.parent / "linkedin" / "Connections.csv"
            self.assertEqual(payload["status"], "copied")
            self.assertEqual(payload["copied_to"], str(copied_to))
            self.assertTrue(copied_to.exists())

    def test_open_linkedin_drop_folder_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            with mock.patch.object(onboarding.subprocess, "run") as run_mock:
                code, payload = self.invoke(onboarding, ["open-linkedin-drop-folder", "--accounts", str(accounts_path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["opened"])
            self.assertEqual(payload["drop_path"], str(accounts_path.parent / "linkedin" / "Connections.csv"))
            run_mock.assert_called_once()

    def test_linkedin_mcp_instructions_and_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            code, payload = self.invoke(linkedin_mcp_import, ["instructions", "--accounts", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["mcp_config"]["mcpServers"]["linkedin"]["command"], "uvx")
            code, _ = self.invoke(linkedin_mcp_import, ["mark-linked", "--accounts", str(path), "--username", "https://www.linkedin.com/in/me"])
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["linkedin_mcp"]["linked"])


if __name__ == "__main__":
    unittest.main()
