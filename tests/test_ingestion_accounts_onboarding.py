import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

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


class IngestionAccountsOnboardingTests(unittest.TestCase):
    def invoke(self, module, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = module.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

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

    def create_msgvault_db(self, path: Path):
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
            CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                source_id INTEGER,
                conversation_id INTEGER,
                message_type TEXT,
                sent_at TEXT,
                received_at TEXT,
                internal_date TEXT,
                deleted_at TEXT,
                deleted_from_source_at TEXT
            );
            CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
            INSERT INTO sources (id, source_type, identifier, display_name) VALUES
                (1, 'gmail', 'me@gmail.com', 'Me'),
                (2, 'gmail', 'work@example.com', 'Work');
            INSERT INTO participants (id, email_address, display_name, domain) VALUES
                (1, 'jane@example.com', 'Jane Example', 'example.com'),
                (2, 'me@gmail.com', 'Me', 'gmail.com'),
                (3, 'pat@example.org', 'Pat Example', 'example.org'),
                (4, 'work@example.com', 'Work', 'example.com');
            INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES
                (10, 1, 100, 'email', '2026-01-01T00:00:00Z'),
                (11, 2, 101, 'email', '2026-01-02T00:00:00Z');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
                (10, 1, 'from', 'Jane Example'),
                (10, 2, 'to', 'Me'),
                (11, 3, 'from', 'Pat Example'),
                (11, 4, 'to', 'Work');
        """)
        con.commit()
        con.close()

    def write_gmail_artifact_run(self, run_dir: Path, *, email: str = "me@gmail.com") -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        people = run_dir / "people.csv"
        accounts_csv = run_dir / "accounts.csv"
        manifest = run_dir / "manifest.json"
        people.write_text("id,primary_email,source_channels\np1,jane@example.com,gmail_msgvault\n", encoding="utf-8")
        accounts_csv.write_text(
            "account_id,account_email,provider,source,added_at\n"
            f"msgvault:abc,{email},gmail,msgvault,2026-01-01T00:00:00Z\n",
            encoding="utf-8",
        )
        manifest.write_text(json.dumps({
            "status": "completed",
            "source": "msgvault",
            "task": "import_gmail_network_msgvault",
            "artifacts": {"people_csv": str(people)},
        }), encoding="utf-8")

    def test_check_ignores_non_default_gmail_test_artifacts(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.write_gmail_artifact_run(Path(".powerpacks/network-import/gmail/network-test-gmail"))
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, ["check", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["artifacts"], [])
            self.assertNotIn("gmail", [item["channel"] for item in payload["steps"] if item["linked"]])

    def test_check_detects_default_msgvault_gmail_artifact(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.write_gmail_artifact_run(Path(".powerpacks/network-import/gmail/msgvault-realrun"), email="me@gmail.com")
                path = Path(tmp) / "accounts.json"
                code, _payload = self.invoke(onboarding, ["check", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["usernames"], ["me@gmail.com"])
            self.assertEqual(registry["accounts"]["gmail"]["artifacts"], [".powerpacks/network-import/gmail/msgvault-realrun/people.csv"])

    def test_onboarding_step_prompts_for_linkedin_csv(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("gmail", path=path, skipped=True)
                code, payload = self.invoke(onboarding, ["step", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_input")
            self.assertEqual(payload["channel"], "linkedin_csv")
            self.assertIn("--linkedin-csv", payload["next_command"])

    def test_onboarding_step_detects_linkedin_artifact(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                artifact = Path(".powerpacks/network-import/linkedin/run-1/people_harmonic_all.csv")
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("id\n", encoding="utf-8")
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("gmail", path=path, skipped=True)
                code, payload = self.invoke(onboarding, ["step", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["linkedin_csv"]["linked"])
            self.assertEqual(payload["status"], "next_action")

    def test_onboarding_step_discovers_gmail_msgvault_accounts(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                db = Path(tmp) / "msgvault.db"
                self.create_msgvault_db(db)
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, ["step", "--accounts", str(path), "--gmail-db", str(db)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_input")
            self.assertEqual(payload["channel"], "gmail")
            self.assertIn("other Gmail addresses", payload["prompt"])
            self.assertEqual([row["account_email"] for row in payload["discovered_accounts"]], ["me@gmail.com", "work@example.com"])
            self.assertIn("--gmail-all", payload["all_command"])
            self.assertIn("--gmail-add-email EMAIL", payload["add_other_email_command"])

    def test_onboarding_step_returns_agent_actions_for_extra_gmail_email(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-add-email", "Other@Example.com",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_agent_action")
            self.assertEqual(payload["channel"], "gmail")
            self.assertEqual(payload["emails"], ["other@example.com"])
            commands = payload["commands"]
            self.assertEqual(commands[0]["label"], "add_oauth_test_users")
            self.assertIn("msgvault_setup.py add-test-users other@example.com", commands[0]["command"])
            self.assertIn("msgvault_setup.py add-account --email other@example.com", commands[1]["command"])
            self.assertEqual(commands[2]["label"], "start_msgvault_sync_other@example.com")
            self.assertIn("msgvault sync-full other@example.com", commands[2]["command"])
            self.assertIn("tmux new-session", commands[2]["command"])
            self.assertIn(".powerpacks/ingestion/logs/msgvault-sync-other-example-com.log", commands[2]["command"])
            self.assertEqual(commands[3]["label"], "rerun_onboarding")

    def test_onboarding_step_imports_multiple_gmail_accounts(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                db = Path(tmp) / "msgvault.db"
                self.create_msgvault_db(db)
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-db", str(db),
                    "--gmail-output-dir", str(Path(tmp) / "out"), "--gmail-all",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "gmail")
            self.assertEqual([row["account_email"] for row in payload["imported_accounts"]], ["me@gmail.com", "work@example.com"])
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["usernames"], ["me@gmail.com", "work@example.com"])


if __name__ == "__main__":
    unittest.main()
