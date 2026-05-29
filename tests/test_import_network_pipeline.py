import argparse
import csv
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py"
SPEC = importlib.util.spec_from_file_location("import_network_pipeline", SCRIPT)
import_network_pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = import_network_pipeline
SPEC.loader.exec_module(import_network_pipeline)


def write_msgvault_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
        CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, source_id INTEGER, conversation_id INTEGER, message_type TEXT, sent_at TEXT, received_at TEXT, internal_date TEXT, deleted_at TEXT, deleted_from_source_at TEXT);
        CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
        INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'me@example.com', 'Me');
        INSERT INTO participants (id, email_address, display_name, domain) VALUES (1, 'jane@example.com', 'Jane Example', 'example.com');
        INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES (1, 1, 10, 'email', '2026-01-01T00:00:00Z');
        INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES (1, 1, 'from', 'Jane Example');
    """)
    con.commit()
    con.close()


class ImportNetworkPipelineTests(unittest.TestCase):
    def test_gmail_api_estimate_uses_oauth_token_without_message_reads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "tokens").mkdir()
            client = home / "client_secret.json"
            client.write_text(json.dumps({
                "installed": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }), encoding="utf-8")
            (home / "config.toml").write_text(f'[oauth]\nclient_secrets = "{client}"\n', encoding="utf-8")
            (home / "tokens/me@example.com.json").write_text(json.dumps({
                "access_token": "access-token",
                "client_id": "client-id",
                "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
            }), encoding="utf-8")
            with mock.patch.object(import_network_pipeline, "gmail_label_totals", return_value=({
                "INBOX": 90,
                "SENT": 10,
                "CATEGORY_SOCIAL": 5,
                "CATEGORY_PROMOTIONS": 25,
                "CATEGORY_FORUMS": 10,
                "CATEGORY_UPDATES": 15,
            }, "")) as labels:
                with mock.patch.object(import_network_pipeline, "gmail_message_id_count") as count:
                    estimate = import_network_pipeline.estimate_gmail_account_via_api(
                        home,
                        "me@example.com",
                        "-category:social -category:promotions -category:forums -category:updates",
                        ["CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES"],
                    )
            self.assertEqual(estimate["status"], "ok")
            self.assertEqual(estimate["messages_total_estimate"], 100)
            self.assertIsNone(estimate["messages_matching_sync_query_estimate"])
            self.assertIsNone(estimate["messages_excluded_by_sync_query_estimate"])
            self.assertFalse(estimate["privacy"]["message_bodies_read"])
            self.assertFalse(estimate["privacy"]["message_ids_listed"])
            labels.assert_called_once()
            count.assert_not_called()

    def test_gmail_api_estimate_can_count_query_ids_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "tokens").mkdir()
            client = home / "client_secret.json"
            client.write_text(json.dumps({
                "installed": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }), encoding="utf-8")
            (home / "config.toml").write_text(f'[oauth]\nclient_secrets = "{client}"\n', encoding="utf-8")
            (home / "tokens/me@example.com.json").write_text(json.dumps({
                "access_token": "access-token",
                "client_id": "client-id",
                "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
            }), encoding="utf-8")
            with mock.patch.object(import_network_pipeline, "gmail_label_totals", return_value=({
                "INBOX": 90,
                "SENT": 10,
                "CATEGORY_SOCIAL": 5,
                "CATEGORY_PROMOTIONS": 25,
                "CATEGORY_FORUMS": 10,
                "CATEGORY_UPDATES": 15,
            }, "")):
                with mock.patch.object(import_network_pipeline, "gmail_message_id_count", return_value=({"count": 60, "complete": True, "pages": 2}, "")) as count:
                    estimate = import_network_pipeline.estimate_gmail_account_via_api(
                        home,
                        "me@example.com",
                        "-category:social -category:promotions -category:forums -category:updates",
                        ["CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES"],
                        max_pages=10,
                    )
            self.assertEqual(estimate["messages_matching_sync_query_estimate"], 60)
            self.assertEqual(estimate["messages_excluded_by_sync_query_estimate"], 40)
            self.assertTrue(estimate["privacy"]["message_ids_listed"])
            self.assertEqual(count.call_args.args[1], "-category:social -category:promotions -category:forums -category:updates")

    def test_msgvault_db_defaults_only_when_gmail_is_requested(self) -> None:
        self.assertEqual(
            import_network_pipeline.resolve_msgvault_db(argparse.Namespace(msgvault_db="", gmail_account_email="me@example.com")),
            str(import_network_pipeline.DEFAULT_MSGVAULT_DB),
        )

    def test_from_accounts_populates_sources_and_worker_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            linkedin = tmp / "Connections.csv"
            contacts = tmp / "contacts.csv"
            linkedin.write_text("First Name,Last Name,URL\n", encoding="utf-8")
            contacts.write_text("name\n", encoding="utf-8")
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "version": 2,
                "accounts": {
                    "gmail": {"linked": True, "usernames": ["old@example.com"], "artifacts": [], "config": {"msgvault_db": str(db), "selected_accounts": ["me@example.com", "work@example.com"]}},
                    "linkedin_csv": {"linked": True, "usernames": ["me"], "artifacts": [], "config": {"csv_path": str(linkedin), "source_label": "me"}},
                    "twitter": {"linked": True, "usernames": ["arthur"], "artifacts": [], "config": {"handle": "arthur"}},
                    "messages": {"linked": True, "usernames": [], "artifacts": [str(contacts)], "config": {"contacts_csv": str(contacts)}},
                },
            }), encoding="utf-8")
            args = import_network_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = import_network_pipeline.apply_account_sources(args)
            self.assertEqual(args.msgvault_db, str(db))
            self.assertEqual(args.gmail_account_emails, ["me@example.com", "work@example.com"])
            self.assertEqual(args.linkedin_csv, str(linkedin))
            self.assertEqual(args.linkedin_source_user, "me")
            self.assertEqual(args.twitter_handle, "arthur")
            self.assertEqual(args.messages_contacts_csv, str(contacts))
            payload = import_network_pipeline.dry_run_plan(args, tmp / "ledger.json", "network-test", tmp / "run")
            jobs = payload["worker_groups"]["import"]["jobs"]
            self.assertEqual({job["source"] for job in jobs}, {"gmail", "linkedin_csv", "twitter", "messages"})
            self.assertEqual([job["account_email"] for job in jobs if job["source"] == "gmail"], ["me@example.com", "work@example.com"])
            self.assertTrue(all(job["parallelizable"] for job in jobs))

    def test_from_setup_finds_accounts_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({"version": 2, "accounts": {}}), encoding="utf-8")
            setup_ledger = tmp / "setup.json"
            setup_ledger.write_text(json.dumps({"handoff": {"commands": {"import_network_run": f"uv run --project . python x.py run --from-accounts {accounts}"}}}), encoding="utf-8")
            args = import_network_pipeline.build_parser().parse_args(["run", "--from-setup", str(setup_ledger), "--dry-run"])
            args = import_network_pipeline.apply_account_sources(args)
            self.assertEqual(args.from_accounts, str(accounts))

    def test_from_accounts_accepts_status_linked_channels_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            linkedin = tmp / "Connections.csv"
            contacts = tmp / "contacts.csv"
            linkedin.write_text("First Name,Last Name,URL\n", encoding="utf-8")
            contacts.write_text("name\n", encoding="utf-8")
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "version": 2,
                "channels": {
                    "gmail": {"status": "linked", "config": {"selected_accounts": ["me@example.com"]}},
                    "linkedin_csv": {"status": "linked", "artifacts": [str(linkedin)], "usernames": ["me"]},
                    "twitter": {"status": "skipped", "usernames": ["stale"]},
                    "messages": {"status": "linked", "artifacts": [str(contacts)]},
                },
            }), encoding="utf-8")
            args = import_network_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = import_network_pipeline.apply_account_sources(args)
            self.assertEqual(args.gmail_account_emails, ["me@example.com"])
            self.assertEqual(args.linkedin_csv, str(linkedin))
            self.assertEqual(args.linkedin_source_user, "me")
            self.assertEqual(args.messages_contacts_csv, str(contacts))
            self.assertEqual(args.twitter_handle, "")

    def test_pending_gmail_accounts_are_not_import_ready_until_linked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "version": 2,
                "accounts": {
                    "gmail": {
                        "skipped": False,
                        "usernames": [],
                        "artifacts": [],
                        "config": {
                            "msgvault_db": str(tmp / "msgvault.db"),
                            "pending_accounts": ["pending@example.com"],
                            "selected_accounts": [],
                            "account_emails": [],
                        },
                    }
                },
            }), encoding="utf-8")
            args = import_network_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = import_network_pipeline.apply_account_sources(args)
            self.assertEqual(args.gmail_account_emails, [])
            self.assertEqual(args.msgvault_db, "")

            data = json.loads(accounts.read_text(encoding="utf-8"))
            data["accounts"]["gmail"]["linked"] = True
            data["accounts"]["gmail"]["usernames"] = ["pending@example.com"]
            data["accounts"]["gmail"]["config"]["selected_accounts"] = ["pending@example.com"]
            data["accounts"]["gmail"]["config"]["account_emails"] = ["pending@example.com"]
            data["accounts"]["gmail"]["config"]["pending_accounts"] = []
            accounts.write_text(json.dumps(data), encoding="utf-8")
            args = import_network_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = import_network_pipeline.apply_account_sources(args)
            self.assertEqual(args.gmail_account_emails, ["pending@example.com"])
            self.assertEqual(args.msgvault_db, str(tmp / "msgvault.db"))

    def test_parallel_source_workers_record_child_ledgers_and_wait_for_fan_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {
                    "operator_id": "local",
                    "linkedin_csv": str(tmp / "Connections.csv"),
                    "linkedin_source_user": "me",
                    "msgvault_db": str(tmp / "msgvault.db"),
                    "gmail_account_emails": ["me@example.com", "work@example.com"],
                    "gmail_linkedin_provider": "off",
                },
                "steps": {},
                "artifacts": {},
            }
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                if cmd[0] == "msgvault" and "sync-full" in cmd:
                    return 0, {"status": "completed"}, ""
                if any("linkedin_network_import.py" in part for part in cmd):
                    return 0, {"status": "completed", "artifacts": {"people_csv": str(tmp / "linkedin_people.csv")}}, ""
                email = cmd[cmd.index("--account-email") + 1]
                people = tmp / f"gmail-{email}.csv"
                return 0, {"status": "completed", "artifacts": {"people_csv": str(people), "linkedin_resolution_queue_csv": str(tmp / f"queue-{email}.csv")}, "counts": {"contacts_seen": 1, "contacts_written": 1}}, ""
            with mock.patch.object(import_network_pipeline.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                    ok = import_network_pipeline.run_source_import_workers(ledger_path, ledger)
            default_query = "-category:social -category:promotions -category:forums -category:updates"
            self.assertCountEqual([cmd for cmd in calls if cmd[0] == "msgvault" and "sync-full" in cmd], [["msgvault", "--home", str(tmp), "sync-full", "me@example.com", "--query", default_query], ["msgvault", "--home", str(tmp), "sync-full", "work@example.com", "--query", default_query]])
            for email in ["me@example.com", "work@example.com"]:
                sync_index = calls.index(["msgvault", "--home", str(tmp), "sync-full", email, "--query", default_query])
                import_index = next(i for i, cmd in enumerate(calls) if "--account-email" in cmd and cmd[cmd.index("--account-email") + 1] == email)
                self.assertLess(sync_index, import_index)
                import_cmd = calls[import_index]
                self.assertIn("--exclude-label", import_cmd)
                self.assertIn("CATEGORY_PROMOTIONS", import_cmd)
            self.assertTrue(ok)
            self.assertEqual(ledger["steps"]["source_imports"]["status"], "completed")
            self.assertEqual(ledger["steps"]["gmail_msgvault"]["status"], "completed")
            self.assertIn("gmail_msgvault:me-example.com", ledger["steps"])
            self.assertIn("gmail_msgvault:work-example.com", ledger["steps"])
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["sync_query"], default_query)
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["gmail_estimate"]["status"], "token_missing")
            self.assertIn("linkedin_people_csv", ledger["artifacts"])
            self.assertEqual(len(ledger["artifacts"]["gmail_people_csvs"]), 2)
            self.assertTrue(ledger["worker_groups"]["import"]["parallel"])

    def test_gmail_category_mail_can_be_included_for_sync_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {
                    "operator_id": "local",
                    "msgvault_db": str(tmp / "msgvault.db"),
                    "gmail_account_emails": ["me@example.com"],
                    "gmail_linkedin_provider": "off",
                    "include_category_mail": True,
                },
                "steps": {},
                "artifacts": {},
            }
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                if cmd[0] == "msgvault" and "sync-full" in cmd:
                    return 0, {"status": "completed"}, ""
                people = tmp / "gmail.csv"
                return 0, {"status": "completed", "artifacts": {"people_csv": str(people)}, "counts": {"contacts_seen": 1, "contacts_written": 1}}, ""
            with mock.patch.object(import_network_pipeline.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                    self.assertTrue(import_network_pipeline.run_source_import_workers(ledger_path, ledger))
            self.assertIn(["msgvault", "--home", str(tmp), "sync-full", "me@example.com"], calls)
            import_cmd = next(cmd for cmd in calls if any("gmail_network_import.py" in part for part in cmd))
            self.assertIn("--include-category-mail", import_cmd)
            self.assertNotIn("--exclude-label", import_cmd)

    def test_linkedin_approval_blocks_before_fan_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            ledger = {"run_id": "network-test", "run_dir": str(tmp / "network-test"), "input": {"linkedin_csv": str(tmp / "Connections.csv"), "linkedin_source_user": "me"}, "steps": {}, "artifacts": {}}
            with mock.patch.object(import_network_pipeline, "run_cmd", return_value=(20, {"status": "blocked_approval"}, "")):
                ok = import_network_pipeline.run_source_import_workers(ledger_path, ledger)
            self.assertFalse(ok)
            self.assertEqual(ledger["steps"]["source_imports"]["status"], "blocked")
            self.assertEqual(ledger["blocked"]["step_id"], "linkedin")

    def test_merge_includes_all_gmail_people_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"include_existing_artifacts": False},
                "steps": {},
                "artifacts": {
                    "gmail_people_csvs": [str(tmp / "gmail-a.csv"), str(tmp / "gmail-b.csv")],
                    "linkedin_people_csv": str(tmp / "linkedin.csv"),
                },
            }
            seen_cmds = []
            def fake_run_cmd(cmd, timeout=None):
                seen_cmds.append(cmd)
                return 0, {"people_csv": str(tmp / "merged.csv"), "network_contacts_csv": "contacts.csv", "network_contact_sources_csv": "sources.csv", "network_companies_csv": "companies.csv", "manifest": "manifest.json", "merged_rows": 3}, ""
            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertTrue(import_network_pipeline.run_merge(ledger_path, ledger))
            cmd = seen_cmds[0]
            self.assertIn("--no-discover", cmd)
            input_values = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--input"]
            self.assertEqual(input_values, [str(tmp / "linkedin.csv"), str(tmp / "gmail-a.csv"), str(tmp / "gmail-b.csv")])

    def test_include_existing_artifacts_uses_only_expected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            canonical = base / "merged/people.csv"
            stale_discovered = base / "gmail/old-scratch/people.csv"
            current_gmail = tmp / "current-gmail.csv"
            current_linkedin = tmp / "current-linkedin.csv"
            messages = tmp / ".powerpacks/messages/contacts.csv"
            for path in (canonical, stale_discovered, current_gmail, current_linkedin, messages):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("id\nx\n", encoding="utf-8")
            ledger = {
                "input": {"include_existing_artifacts": True},
                "artifacts": {
                    "gmail_people_csvs": [str(current_gmail)],
                    "linkedin_people_csv": str(current_linkedin),
                },
            }
            merge_dir = tmp / "run/merged"
            with mock.patch.object(import_network_pipeline, "DEFAULT_BASE_DIR", base):
                paths = import_network_pipeline.merge_input_paths(ledger, merge_dir)
            self.assertIn(str(canonical), paths)
            self.assertIn(str(current_linkedin), paths)
            self.assertIn(str(current_gmail), paths)
            self.assertNotIn(str(stale_discovered), paths)
            message_inputs = [path for path in paths if path.endswith("source-inputs/messages/contacts.csv")]
            self.assertEqual(len(message_inputs), 1)
            self.assertTrue(Path(message_inputs[0]).exists())

    def test_merge_includes_linked_messages_contacts_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            contacts.write_text("name,phone,source,message_count,last_message\nJane,+15551234567,imessage,3,2026-01-01\n", encoding="utf-8")
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"include_existing_artifacts": False, "messages_contacts_csv": str(contacts)},
                "steps": {},
                "artifacts": {},
            }
            seen_cmds = []
            def fake_run_cmd(cmd, timeout=None):
                seen_cmds.append(cmd)
                return 0, {"people_csv": str(tmp / "merged.csv"), "network_contacts_csv": "contacts.csv", "network_contact_sources_csv": "sources.csv", "network_companies_csv": "companies.csv", "manifest": "manifest.json", "merged_rows": 1}, ""
            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertTrue(import_network_pipeline.run_merge(ledger_path, ledger))
            input_values = [cmd[i + 1] for cmd in seen_cmds for i, part in enumerate(cmd) if part == "--input"]
            self.assertEqual(len(input_values), 1)
            self.assertTrue(input_values[0].endswith("source-inputs/messages/contacts.csv"))
            self.assertTrue(Path(input_values[0]).exists())

    def test_multi_gmail_resolution_and_enrichment_iterates_all_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"gmail_linkedin_provider": "harness"},
                "steps": {},
                "artifacts": {
                    "gmail_linkedin_resolution_queue_csvs": [
                        {"account_email": "me@example.com", "queue_csv": str(tmp / "queue-me.csv"), "people_csv": str(tmp / "people-me.csv")},
                        {"account_email": "work@example.com", "queue_csv": str(tmp / "queue-work.csv"), "people_csv": str(tmp / "people-work.csv")},
                    ]
                },
            }
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                if any("resolve_linkedin_queue.py" in part for part in cmd):
                    queue = Path(cmd[cmd.index("--input") + 1]).stem
                    return 0, {"output": str(tmp / f"resolutions-{queue}.csv")}, ""
                if any("gmail_network_import.py" in part for part in cmd):
                    run_id = cmd[cmd.index("--run-id") + 1]
                    return 0, {"people_csv": str(tmp / f"resolved-{run_id}.csv"), "resolved": 1}, ""
                if any("enrich_people.py" in part for part in cmd):
                    run_id = cmd[cmd.index("--run-id") + 1]
                    return 0, {"artifacts": {"people_csv": str(tmp / f"enriched-{run_id}.csv")}}, ""
                self.fail(f"unexpected command: {cmd}")
            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertTrue(import_network_pipeline.run_gmail_linkedin_resolution(ledger_path, ledger))
                self.assertTrue(import_network_pipeline.run_gmail_apply_and_enrich(ledger_path, ledger))
                self.assertTrue(import_network_pipeline.run_gmail_linkedin_resolution(ledger_path, ledger))
                self.assertTrue(import_network_pipeline.run_gmail_apply_and_enrich(ledger_path, ledger))
            self.assertEqual(len(ledger["artifacts"]["gmail_linkedin_resolutions_csvs"]), 2)
            self.assertEqual(len(ledger["artifacts"]["gmail_enrich_people_ledgers"]), 2)
            self.assertEqual(len(ledger["artifacts"]["gmail_final_people_csvs"]), 2)
            self.assertEqual(sum(1 for cmd in calls if any("resolve_linkedin_queue.py" in part for part in cmd)), 2)
            self.assertEqual(sum(1 for cmd in calls if any("gmail_network_import.py" in part for part in cmd)), 2)
            self.assertEqual(sum(1 for cmd in calls if any("enrich_people.py" in part for part in cmd)), 2)

    def test_explicit_gmail_resolutions_csv_rejects_multiple_gmail_people_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"gmail_resolutions_csv": str(tmp / "resolutions.csv")},
                "steps": {},
                "artifacts": {"gmail_people_csvs": [str(tmp / "gmail-a.csv"), str(tmp / "gmail-b.csv")], "gmail_people_csv": str(tmp / "gmail-b.csv")},
            }
            with mock.patch.object(import_network_pipeline, "run_cmd") as run_cmd:
                self.assertFalse(import_network_pipeline.run_gmail_apply_and_enrich(tmp / "ledger.json", ledger))
            run_cmd.assert_not_called()
            self.assertEqual(ledger["status"], "failed")
            self.assertIn("ambiguous", ledger["steps"]["gmail_apply_enrich"]["error"])
        self.assertEqual(
            import_network_pipeline.resolve_msgvault_db(argparse.Namespace(msgvault_db="", gmail_account_email="")),
            "",
        )
        self.assertEqual(
            import_network_pipeline.resolve_msgvault_db(argparse.Namespace(msgvault_db="/tmp/msgvault.db", gmail_account_email="")),
            "/tmp/msgvault.db",
        )

    def test_completed_ledger_dry_run_reports_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            run_dir = tmp / "run"
            people = run_dir / "merged/people.csv"
            people.parent.mkdir(parents=True)
            people.write_text("id\np1\n", encoding="utf-8")
            ledger_path.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "run_id": "network-test",
                        "run_dir": str(run_dir),
                        "steps": {
                            "linkedin": {"status": "completed"},
                            "gmail_msgvault": {"status": "skipped"},
                            "gmail_linkedin_resolution": {"status": "skipped"},
                            "gmail_apply_enrich": {"status": "skipped"},
                            "merge": {"status": "completed"},
                            "duckdb": {"status": "completed"},
                        },
                        "artifacts": {"merged_people_csv": str(people)},
                    }
                ),
                encoding="utf-8",
            )

            payload = import_network_pipeline.dry_run_plan(
                argparse.Namespace(
                    linkedin_csv="",
                    gmail_account_email="",
                    msgvault_db="",
                    gmail_linkedin_provider="off",
                    gmail_resolutions_csv="",
                ),
                ledger_path,
                "network-test",
                run_dir,
            )

            self.assertEqual(payload["existing_status"], "completed")
            self.assertEqual(payload["would_run_steps"], [])
            self.assertEqual(payload["estimated_paid_calls"], 0)
            self.assertEqual(payload["artifact_check"]["missing_count"], 0)

    def test_msgvault_to_merge_to_duckdb(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            write_msgvault_db(db)
            ledger = tmp / "ledger.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run",
                    "--ledger", str(ledger),
                    "--run-id", "network-test",
                    "--msgvault-db", str(db),
                    "--gmail-account-email", "me@example.com",
                    "--skip-msgvault-sync",
                    "--force",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "completed")
            artifacts = payload["artifacts"]
            self.assertTrue(Path(artifacts["network_contacts_csv"]).exists())
            self.assertTrue(Path(artifacts["network_contact_sources_csv"]).exists())
            self.assertTrue(Path(artifacts["duckdb"]).exists())
            with Path(artifacts["network_contact_sources_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["source_channel"], "gmail_msgvault")

    def test_msgvault_can_prepare_gmail_linkedin_harness(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            write_msgvault_db(db)
            ledger = tmp / "ledger.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run",
                    "--ledger", str(ledger),
                    "--run-id", "network-harness-test",
                    "--msgvault-db", str(db),
                    "--gmail-account-email", "me@example.com",
                    "--skip-msgvault-sync",
                    "--gmail-linkedin-provider", "harness",
                    "--force",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            artifacts = payload["artifacts"]
            self.assertTrue(Path(artifacts["gmail_linkedin_resolution_queue_csv"]).exists())
            self.assertTrue(Path(artifacts["gmail_linkedin_harness_prompts_jsonl"]).exists())
            self.assertIn("gmail_apply_enrich", json.loads(ledger.read_text())["steps"])


if __name__ == "__main__":
    unittest.main()
