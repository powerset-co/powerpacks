import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py"
spec = importlib.util.spec_from_file_location("gmail_network_import", MODULE_PATH)
gmail_network_import = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = gmail_network_import
spec.loader.exec_module(gmail_network_import)


class GmailNetworkImportTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = gmail_network_import.main(argv)
        output = buf.getvalue().strip()
        payload = json.loads(output) if output else {}
        return code, payload

    def test_run_writes_powerpacks_local_artifacts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            code, payload = self.invoke([
                "run",
                "--email", "Jane.Example@Example.com",
                "--name", "Jane Example",
                "--account-email", "me@gmail.com",
                "--account-id", "gmail-account-1",
                "--operator-id", "operator-12345678",
                "--output-dir", str(Path(tmp) / "out"),
                "--ledger", str(ledger),
                "--run-id", "run-test",
                "--force",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertIn("Local one-person Gmail seed completed", payload["summary"])
            artifacts = payload["artifacts"]
            targeted = Path(artifacts["targeted_emails_csv"])
            aggregated = Path(artifacts["gmail_contacts_aggregated_csv"])
            threads = Path(artifacts["gmail_threads_csv"])
            accounts = Path(artifacts["accounts_csv"])
            for path in (targeted, aggregated, threads, accounts):
                self.assertTrue(path.exists())
                self.assertIn(str(Path(tmp) / "out"), str(path))
            with targeted.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["primary_email"], "jane.example@example.com")
            self.assertEqual(rows[0]["primary_email_type"], "work")
            state = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(state["steps"]["seed_one"]["status"], "completed")
            self.assertEqual(state["steps"]["prepare_local_workspace"]["status"], "completed")
            self.assertEqual(state["steps"]["write_next_steps"]["status"], "completed")
            self.assertNotIn("aleph_root", state)

    def test_continue_after_completed_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            self.invoke([
                "run",
                "--email", "jane@example.com",
                "--name", "Jane Example",
                "--output-dir", str(Path(tmp) / "out"),
                "--ledger", str(ledger),
                "--force",
            ])
            code, payload = self.invoke(["continue", "--ledger", str(ledger)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")

    def test_parse_email_header_port(self):
        parsed = gmail_network_import.parse_email_header('"Jane Example" <jane@example.com>, john@example.org')
        self.assertEqual(parsed, [("Jane Example", "jane@example.com"), ("", "john@example.org")])

    def test_connect_no_open_prints_web_oauth_route(self):
        code, payload = self.invoke(["connect", "--no-open", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["app_url"], "https://search.powerset.dev/gmail/connect")
        self.assertFalse(payload["opened_browser"])
        self.assertIn("does not put the local bearer token", payload["auth_model"])

    def test_connect_waits_by_default(self):
        stats_before = {"connected_accounts": []}
        stats_after = {"connected_accounts": [{"email": "default@example.com"}]}
        with mock.patch.object(gmail_network_import, "powerset_token", return_value="token"), \
             mock.patch.object(gmail_network_import, "fetch_gmail_stats", side_effect=[stats_before, stats_after]), \
             mock.patch.object(gmail_network_import.time, "sleep"):
            code, payload = self.invoke([
                "connect",
                "--no-open",
                "--timeout-seconds", "1",
                "--poll-seconds", "0.5",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "linked")
        self.assertEqual(payload["email"], "default@example.com")

    def test_connect_wait_polls_until_new_account_is_linked(self):
        stats_before = {"connected_accounts": []}
        stats_after = {"connected_accounts": [{"email": "new@example.com"}]}
        with mock.patch.object(gmail_network_import, "powerset_token", return_value="token"), \
             mock.patch.object(gmail_network_import, "fetch_gmail_stats", side_effect=[stats_before, stats_after]), \
             mock.patch.object(gmail_network_import.time, "sleep"):
            code, payload = self.invoke([
                "connect",
                "--no-open",
                "--wait",
                "--timeout-seconds", "1",
                "--poll-seconds", "0.5",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "linked")
        self.assertEqual(payload["email"], "new@example.com")


if __name__ == "__main__":
    unittest.main()
