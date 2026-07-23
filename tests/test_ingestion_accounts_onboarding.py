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
onboarding = load_module("onboarding", "packs/ingestion/primitives/setup/onboarding.py")


class IngestionAccountsOnboardingTests(unittest.TestCase):
    def invoke(self, module, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = module.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload


    def test_onboarding_plan_uses_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            accounts.save_registry(accounts.default_registry(), path)
            accounts.update_channel("gmail", path=path, username="me@example.com", success=True)
            code, payload = self.invoke(onboarding, ["plan", "--accounts", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("todo", payload)
            self.assertNotIn("gmail", [item["channel"] for item in payload["todo"]])
            self.assertEqual([item["channel"] for item in payload["todo"]], ["linkedin_csv", "messages", "twitter"])

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
                self.write_gmail_artifact_run(Path(".powerpacks/network-import/discover/gmail/network-test-gmail"))
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, ["check", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["artifacts"], [])
            self.assertNotIn("gmail", [item["channel"] for item in payload["steps"] if item["linked"]])

    def test_check_does_not_link_default_msgvault_gmail_import_artifact(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.write_gmail_artifact_run(Path(".powerpacks/network-import/discover/gmail/msgvault-realrun"), email="me@gmail.com")
                path = Path(tmp) / "accounts.json"
                code, _payload = self.invoke(onboarding, ["check", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["usernames"], [])
            self.assertEqual(registry["accounts"]["gmail"]["artifacts"], [])

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

    def test_onboarding_step_ignores_linkedin_import_artifact(self):
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
            self.assertFalse(registry["accounts"]["linkedin_csv"]["linked"])
            self.assertEqual(payload["status"], "needs_input")
            self.assertEqual(payload["channel"], "linkedin_csv")

    def test_onboarding_step_records_linkedin_csv_without_import(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                csv_path = Path(tmp) / "Connections.csv"
                csv_path.write_text("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n", encoding="utf-8")
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("gmail", path=path, skipped=True)
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path),
                    "--linkedin-csv", str(csv_path),
                    "--linkedin-source-user", "me@example.com",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "linkedin_csv")
            self.assertNotIn("approval_command", payload)
            self.assertFalse((Path(tmp) / ".powerpacks/network-import/linkedin/import-run.json").exists())
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["linkedin_csv"]["linked"])
            self.assertEqual(registry["accounts"]["linkedin_csv"]["usernames"], ["me@example.com"])
            self.assertEqual(registry["accounts"]["linkedin_csv"]["artifacts"], [str(csv_path)])

    def test_onboarding_step_messages_checks_readiness_not_contacts_csv(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("gmail", path=path, skipped=True)
                accounts.update_channel("linkedin_csv", path=path, skipped=True)
                readiness = {
                    "imessage": {"status": "ready", "payload": {"chat_db": {"path": "chat.db"}, "addressbook_matches": 3}},
                    "whatsapp": {"status": "needs_auth", "authenticated": False, "auth_command": "whatsapp-auth", "payload": {}},
                    "privacy": {"reads_message_bodies": False, "syncs_whatsapp": False, "exports_contacts": False},
                }
                with mock.patch.object(onboarding, "messages_link_status", return_value=readiness):
                    code, payload = self.invoke(onboarding, ["step", "--accounts", str(path)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_agent_action")
            self.assertEqual(payload["channel"], "messages")
            self.assertIn("authorize_whatsapp", payload["commands"][0]["label"])
            self.assertNotIn("--messages-contacts-csv", payload["message"])
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["messages"]["linked"])

    def test_onboarding_step_records_messages_readiness_without_import(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("gmail", path=path, skipped=True)
                accounts.update_channel("linkedin_csv", path=path, skipped=True)
                readiness = {
                    "imessage": {"status": "ready", "payload": {"chat_db": {"path": "chat.db"}, "addressbook_matches": 3}},
                    "whatsapp": {"status": "needs_auth", "authenticated": False, "auth_command": "whatsapp-auth", "payload": {}},
                    "privacy": {"reads_message_bodies": False, "syncs_whatsapp": False, "exports_contacts": False},
                }
                with mock.patch.object(onboarding, "messages_link_status", return_value=readiness):
                    code, payload = self.invoke(onboarding, ["step", "--accounts", str(path), "--skip-messages-whatsapp"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "messages")
            self.assertIn("No iMessage extraction, WhatsApp sync", payload["message"])
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["messages"]["linked"])
            self.assertEqual(registry["accounts"]["messages"]["usernames"], ["imessage"])
            self.assertEqual(registry["accounts"]["messages"]["artifacts"], [])
            self.assertEqual(registry["accounts"]["messages"]["config"]["planned_contacts_csv"], ".powerpacks/messages/contacts.csv")
            self.assertEqual(registry["accounts"]["messages"]["config"]["whatsapp"]["status"], "skipped")

    def test_onboarding_messages_contacts_csv_flag_is_not_linking_input(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("gmail", path=path, skipped=True)
                accounts.update_channel("linkedin_csv", path=path, skipped=True)
                readiness = {
                    "imessage": {"status": "ready", "payload": {"chat_db": {"path": "chat.db"}, "addressbook_matches": 3}},
                    "whatsapp": {"status": "needs_auth", "authenticated": False, "auth_command": "whatsapp-auth", "payload": {}},
                    "privacy": {"reads_message_bodies": False, "syncs_whatsapp": False, "exports_contacts": False},
                }
                with mock.patch.object(onboarding, "messages_link_status", return_value=readiness):
                    code, payload = self.invoke(onboarding, [
                        "step",
                        "--accounts", str(path),
                        "--messages-contacts-csv", str(Path(tmp) / "missing.csv"),
                        "--skip-messages-whatsapp",
                    ])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "messages")
            self.assertNotIn("--messages-contacts-csv", payload["next_command"])
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["messages"]["linked"])
            self.assertEqual(registry["accounts"]["messages"]["artifacts"], [])
            self.assertEqual(registry["accounts"]["messages"]["config"]["contacts_csv"], "")

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

    def test_onboarding_step_fresh_gmail_asks_for_user_email(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                msgvault_home = Path(tmp) / "msgvault-home"
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-db", str(msgvault_home / "msgvault.db"),
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_input")
            self.assertEqual(payload["channel"], "gmail")
            self.assertEqual(payload["question"], "Which Gmail address should we link first?")
            self.assertEqual(payload["email_source"], "user_provided")
            self.assertIn("Do not infer it", payload["prompt"])
            self.assertIn("--gmail-add-email EMAIL", payload["first_gmail_command"])
            self.assertNotIn("sync", payload["prompt"].lower())
            self.assertNotIn("example_sync", payload)

    def test_onboarding_step_returns_agent_actions_for_extra_gmail_email(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                msgvault_home = Path(tmp) / "msgvault-home"
                msgvault_home.mkdir()
                (msgvault_home / "config.toml").write_text('[oauth]\nclient_secrets = "client_secret.json"\n', encoding="utf-8")
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-db", str(msgvault_home / "msgvault.db"),
                    "--gmail-add-email", "Other@Example.com",
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
            self.assertEqual(commands[2]["label"], "record_authorized_gmail")
            self.assertFalse(any("sync-full" in command["command"] for command in commands))
            self.assertEqual(payload["record_command_after_authorization"].split()[0:4], ["uv", "run", "--project", "."])
            self.assertIn("--gmail-authorized-email other@example.com", payload["record_command_after_authorization"])
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["usernames"], [])
            self.assertEqual(registry["accounts"]["gmail"]["config"]["selected_accounts"], [])
            self.assertEqual(registry["accounts"]["gmail"]["config"]["pending_accounts"], ["other@example.com"])
            self.assertEqual(registry["accounts"]["gmail"]["config"]["oauth_test_users"], ["other@example.com"])

    def test_onboarding_step_confirms_authorized_gmail_without_sync(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                msgvault_home = Path(tmp) / "msgvault-home"
                msgvault_home.mkdir()
                path = Path(tmp) / "accounts.json"
                registry = accounts.default_registry()
                registry["accounts"]["gmail"]["config"]["pending_accounts"] = ["other@example.com"]
                accounts.save_registry(registry, path)
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-db", str(msgvault_home / "msgvault.db"),
                    "--gmail-authorized-email", "Other@Example.com",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["linked_accounts"], ["other@example.com"])
            self.assertNotIn("sync-full", json.dumps(payload))
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["usernames"], ["other@example.com"])
            self.assertEqual(registry["accounts"]["gmail"]["config"]["selected_accounts"], ["other@example.com"])
            self.assertEqual(registry["accounts"]["gmail"]["config"]["pending_accounts"], [])

    def test_onboarding_step_returns_browser_setup_for_first_gmail_email(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                msgvault_home = Path(tmp) / "msgvault-home"
                path = Path(tmp) / "accounts.json"
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-db", str(msgvault_home / "msgvault.db"),
                    "--gmail-add-email", "Other@Example.com",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_agent_action")
            commands = payload["commands"]
            self.assertEqual(commands[0]["label"], "create_oauth_app_and_authorize_other@example.com")
            self.assertIn("msgvault_setup.py browser-setup --email other@example.com", commands[0]["command"])
            self.assertIn(f"--project {onboarding.gmail_oauth_project_id('other@example.com')}", commands[0]["command"])
            self.assertIn("--add-account", commands[0]["command"])
            self.assertIn("--home", commands[0]["command"])
            self.assertEqual(commands[1]["label"], "record_authorized_gmail")
            self.assertIn("--gmail-authorized-email other@example.com", commands[1]["command"])
            self.assertFalse(any("sync-full" in command["command"] for command in commands))

    def test_onboarding_step_rejects_unrequested_authorized_gmail(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                msgvault_home = Path(tmp) / "msgvault-home"
                msgvault_home.mkdir()
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                code, payload = self.invoke(onboarding, [
                    "step", "--accounts", str(path), "--gmail-db", str(msgvault_home / "msgvault.db"),
                    "--gmail-authorized-email", "Other@Example.com",
                ])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "needs_input")
            self.assertEqual(payload["unknown_authorized_accounts"], ["other@example.com"])
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["gmail"]["linked"])

    def test_onboarding_step_records_multiple_gmail_accounts_without_import(self):
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
            self.assertEqual(payload["linked_accounts"], ["me@gmail.com", "work@example.com"])
            self.assertNotIn("imported_accounts", payload)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["gmail"]["linked"])
            self.assertEqual(registry["accounts"]["gmail"]["usernames"], ["me@gmail.com", "work@example.com"])
            self.assertEqual(registry["accounts"]["gmail"]["artifacts"], [])
            self.assertEqual(registry["accounts"]["gmail"]["config"]["selected_accounts"], ["me@gmail.com", "work@example.com"])
            self.assertFalse(Path(tmp, "out").exists())
            self.assertFalse(Path(tmp, ".powerpacks/network-import/discover/ledger.json").exists())
            self.assertFalse(Path(tmp, ".powerpacks/network-import/discover/gmail").exists())

    def test_onboarding_step_auto_links_empty_bootstrap_skipped_gmail_from_msgvault(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                db = Path(tmp) / "msgvault.db"
                self.create_msgvault_db(db)
                path = Path(tmp) / "accounts.json"
                registry = accounts.default_registry()
                registry["accounts"]["gmail"]["linked"] = False
                registry["accounts"]["gmail"]["skipped"] = True
                registry["accounts"]["gmail"]["notes"] = "Skipped for bootstrap-only local search pipeline test."
                accounts.save_registry(registry, path)
                code, payload = self.invoke(onboarding, ["step", "--accounts", str(path), "--gmail-db", str(db)])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "gmail")
            self.assertEqual(payload["linked_accounts"], ["me@gmail.com", "work@example.com"])
            self.assertIn("Auto-linked", payload["message"])
            self.assertNotIn("sync-full", json.dumps(payload))
            registry = accounts.load_registry(path)
            gmail = registry["accounts"]["gmail"]
            self.assertTrue(gmail["linked"])
            self.assertFalse(gmail["skipped"])
            self.assertEqual(gmail["usernames"], ["me@gmail.com", "work@example.com"])
            self.assertEqual(gmail["config"]["selected_accounts"], ["me@gmail.com", "work@example.com"])

    def test_onboarding_completed_emits_subagent_handoff(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                for channel in ["gmail", "linkedin_csv", "messages", "twitter"]:
                    accounts.update_channel(channel, path=path, linked=False, skipped=True)
                code, payload = self.invoke(onboarding, ["step", "--accounts", str(path), "--operator-id", "operator-1"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            handoff = payload["handoff"]
            self.assertEqual(handoff["source_order"], ["gmail", "linkedin_csv", "messages", "twitter"])
            self.assertIn("setup/setup.py handoff", handoff["handoff_command"])
            self.assertIn("--operator-id operator-1", handoff["handoff_command"])
            self.assertIn("Your sources are connected", handoff["confirmation_prompt"])
            self.assertIn("won't upload anything automatically", handoff["confirmation_prompt"])
            self.assertEqual(handoff["codex_orchestration"]["main_thread"], "Handle account linking, browser/login actions, user confirmations, and worker handoffs.")
            self.assertIn("Run handoff_command next", handoff["codex_orchestration"]["flow"])
            self.assertIn("Do not describe ledgers", handoff["codex_orchestration"]["user_summary"])
            self.assertNotIn("worker_phases", handoff)
            self.assertNotIn("preferred_handoff_command", handoff)

    def test_onboarding_twitter_handle_replaces_skipped_state(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                accounts.update_channel("twitter", path=path, linked=False, skipped=True)
                code, payload = self.invoke(onboarding, [
                    "step",
                    "--accounts", str(path),
                    "--twitter-handle", "examplehandle",
                ])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "twitter")
            registry = accounts.load_registry(path)
            twitter = registry["accounts"]["twitter"]
            self.assertTrue(twitter["linked"])
            self.assertFalse(twitter["skipped"])
            self.assertEqual(twitter["usernames"], ["examplehandle"])
            self.assertEqual(twitter["config"]["handle"], "examplehandle")

    def test_onboarding_messages_check_bypasses_unresolved_gmail(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                readiness = {
                    "imessage": {"status": "ready", "payload": {"chat_db": {"path": "chat.db"}, "addressbook_matches": 3}},
                    "whatsapp": {"status": "needs_auth", "authenticated": False, "auth_command": "whatsapp-auth", "payload": {}},
                    "privacy": {"reads_message_bodies": False, "syncs_whatsapp": False, "exports_contacts": False},
                }
                with mock.patch.object(onboarding, "messages_link_status", return_value=readiness):
                    code, payload = self.invoke(onboarding, [
                        "step",
                        "--accounts", str(path),
                        "--messages-check",
                        "--skip-messages-whatsapp",
                    ])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "progressed")
            self.assertEqual(payload["channel"], "messages")
            registry = accounts.load_registry(path)
            self.assertFalse(registry["accounts"]["gmail"]["linked"])
            self.assertTrue(registry["accounts"]["messages"]["linked"])
            self.assertEqual(registry["accounts"]["messages"]["config"]["whatsapp"]["status"], "skipped")

    def test_onboarding_handoff_uses_recorded_linkedin_csv(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                csv_path = Path(tmp) / "Connections.csv"
                csv_path.write_text("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n", encoding="utf-8")
                path = Path(tmp) / "accounts.json"
                accounts.save_registry(accounts.default_registry(), path)
                for channel in ["gmail", "messages", "twitter"]:
                    accounts.update_channel(channel, path=path, linked=False, skipped=True)
                accounts.update_channel(
                    "linkedin_csv",
                    path=path,
                    username="me@example.com",
                    artifact=str(csv_path),
                    success=True,
                )
                code, payload = self.invoke(onboarding, ["step", "--accounts", str(path), "--operator-id", "operator-1"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 0)
            handoff = payload["handoff"]
            self.assertIn("setup/setup.py handoff", handoff["handoff_command"])
            self.assertIn(f"--accounts {str(path)}", handoff["handoff_command"])
            self.assertNotIn("--linkedin-csv", handoff["handoff_command"])
            self.assertNotIn("worker_phases", handoff)
            registry = accounts.load_registry(path)
            self.assertEqual(registry["accounts"]["linkedin_csv"]["artifacts"], [str(csv_path)])
            self.assertEqual(registry["accounts"]["linkedin_csv"]["usernames"], ["me@example.com"])


if __name__ == "__main__":
    unittest.main()
