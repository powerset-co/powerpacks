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
        INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES
            (1, 1, 10, 'email', '2026-01-01T00:00:00Z'),
            (2, 1, 11, 'email', '2026-01-02T00:00:00Z');
        INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
            (1, 1, 'from', 'Jane Example'),
            (2, 1, 'to', 'Jane Example');
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
                    "messages": {"linked": True, "usernames": [], "artifacts": [str(contacts)], "config": {"contacts_csv": str(contacts), "review_csv": str(tmp / "research_review.csv")}},
                },
            }), encoding="utf-8")
            args = import_network_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = import_network_pipeline.apply_account_sources(args)
            self.assertEqual(args.msgvault_db, str(db))
            self.assertEqual(args.gmail_account_emails, ["me@example.com", "work@example.com"])
            self.assertEqual(args.linkedin_csv, str(linkedin))
            self.assertEqual(args.linkedin_source_user, "me")
            self.assertEqual(args.twitter_handle, "arthur")
            self.assertEqual(args.messages_review_csv, str(tmp / "research_review.csv"))
            self.assertEqual(args.messages_contacts_csv, "")
            payload = import_network_pipeline.dry_run_plan(args, tmp / "ledger.json", "network-test", tmp / "run")
            jobs = payload["worker_groups"]["import"]["jobs"]
            self.assertEqual({job["source"] for job in jobs}, {"gmail", "linkedin_csv", "twitter"})
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
            self.assertEqual(args.messages_contacts_csv, "")
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

    def test_gmail_sync_after_is_passed_to_msgvault_sync_full(self) -> None:
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
                    "gmail_sync_after": "2026-05-15",
                },
                "steps": {},
                "artifacts": {},
            }
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                if cmd[0] == "msgvault" and "sync-full" in cmd:
                    return 0, {"status": "completed"}, ""
                return 0, {"status": "completed", "artifacts": {"people_csv": str(tmp / "gmail.csv")}, "counts": {"contacts_seen": 1, "contacts_written": 1}}, ""
            with mock.patch.object(import_network_pipeline.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                    self.assertTrue(import_network_pipeline.run_source_import_workers(ledger_path, ledger))
            default_query = "-category:social -category:promotions -category:forums -category:updates"
            self.assertIn(["msgvault", "--home", str(tmp), "sync-full", "me@example.com", "--after", "2026-05-15", "--query", default_query], calls)
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["sync_after"], "2026-05-15")
            self.assertEqual(ledger["source_imports"]["gmail_msgvault:me-example.com"]["sync_after"], "2026-05-15")

    def test_gmail_sync_after_is_inferred_from_msgvault_last_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            write_msgvault_db(db)
            con = sqlite3.connect(db)
            con.execute("ALTER TABLE sources ADD COLUMN last_sync_at TEXT")
            con.execute("UPDATE sources SET last_sync_at = '2026-05-20 12:34:56' WHERE identifier = 'me@example.com'")
            con.commit()
            con.close()
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {
                    "operator_id": "local",
                    "msgvault_db": str(db),
                    "gmail_account_emails": ["me@example.com"],
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
                return 0, {"status": "completed", "artifacts": {"people_csv": str(tmp / "gmail.csv")}, "counts": {"contacts_seen": 1, "contacts_written": 1}}, ""
            with mock.patch.object(import_network_pipeline.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                    self.assertTrue(import_network_pipeline.run_source_import_workers(ledger_path, ledger))
            default_query = "-category:social -category:promotions -category:forums -category:updates"
            self.assertIn(["msgvault", "--home", str(tmp), "sync-full", "me@example.com", "--after", "2026-05-20", "--query", default_query], calls)
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["sync_after"], "2026-05-20")
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["sync_after_source"], "msgvault.sources.last_sync_at")

    def test_gmail_sync_after_falls_back_to_latest_msgvault_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            write_msgvault_db(db)
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {
                    "operator_id": "local",
                    "msgvault_db": str(db),
                    "gmail_account_emails": ["me@example.com"],
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
                return 0, {"status": "completed", "artifacts": {"people_csv": str(tmp / "gmail.csv")}, "counts": {"contacts_seen": 1, "contacts_written": 1}}, ""
            with mock.patch.object(import_network_pipeline.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                    self.assertTrue(import_network_pipeline.run_source_import_workers(ledger_path, ledger))
            default_query = "-category:social -category:promotions -category:forums -category:updates"
            self.assertIn(["msgvault", "--home", str(tmp), "sync-full", "me@example.com", "--after", "2026-01-02", "--query", default_query], calls)
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["sync_after"], "2026-01-02")
            self.assertEqual(ledger["steps"]["gmail_msgvault:me-example.com"]["sync_after_source"], "msgvault.messages.sent_at")

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

    def test_linkedin_failure_prefers_structured_child_error_over_progress_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            ledger = {"run_id": "network-test", "run_dir": str(tmp / "network-test"), "input": {"linkedin_csv": str(tmp / "Connections.csv"), "linkedin_source_user": "me"}, "steps": {}, "artifacts": {}}
            stderr = "[enrich-people] Prepared LinkedIn enrichment queue: 289 total, 0 cached, 289 RapidAPI fetches, 0 recent failures.\n"
            payload = {"status": "failed", "step_id": "enrich_people", "error": "RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set"}
            with mock.patch.object(import_network_pipeline, "run_cmd", return_value=(1, payload, stderr)):
                ok = import_network_pipeline.run_source_import_workers(ledger_path, ledger)
            self.assertFalse(ok)
            self.assertEqual(ledger["steps"]["linkedin"]["error"], "RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")
            self.assertEqual(ledger["steps"]["source_imports"]["status"], "failed")

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

    def test_include_existing_artifacts_skips_unreviewed_messages_contacts(self) -> None:
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
            self.assertEqual(message_inputs, [])

    def test_include_existing_artifacts_uses_canonical_people_as_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            canonical = base / "merged/people.csv"
            current_linkedin = tmp / "current-linkedin.csv"
            for path in (canonical, current_linkedin):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("id\nx\n", encoding="utf-8")
            ledger = {
                "input": {"include_existing_artifacts": True},
                "artifacts": {"linkedin_people_csv": str(current_linkedin)},
            }

            with mock.patch.object(import_network_pipeline, "DEFAULT_BASE_DIR", base):
                paths = import_network_pipeline.merge_input_paths(ledger, tmp / "run/merged")

            self.assertEqual(paths[:2], [str(canonical), str(current_linkedin)])

    def test_include_existing_artifacts_uses_only_explicitly_approved_messages_review_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            review = tmp / ".powerpacks/messages/research_review.csv"
            review.parent.mkdir(parents=True, exist_ok=True)
            fields = [
                "bucket", "full_name", "phone_e164", "total_messages", "message_source", "last_message",
                "exclude", "approved", "upload_decision", "enrich_decision", "network_name",
                "network_linkedin_url", "network_person_id", "network_match_method", "review_source",
            ]
            rows = [
                {"bucket": "yes", "full_name": "Bucket Only", "phone_e164": "+100", "total_messages": "1", "message_source": "imessage", "exclude": "", "approved": "", "upload_decision": "", "enrich_decision": ""},
                {"bucket": "maybe", "full_name": "Exclude No", "phone_e164": "+101", "total_messages": "2", "message_source": "whatsapp", "exclude": "no", "approved": "", "upload_decision": "", "enrich_decision": ""},
                {"bucket": "maybe", "full_name": "Approved True", "phone_e164": "+102", "total_messages": "3", "message_source": "imessage", "exclude": "", "approved": "true", "upload_decision": "", "enrich_decision": ""},
                {"bucket": "maybe", "full_name": "Upload Include", "phone_e164": "+103", "total_messages": "4", "message_source": "imessage", "exclude": "", "approved": "", "upload_decision": "include", "enrich_decision": ""},
                {"bucket": "maybe", "full_name": "Rejected", "phone_e164": "+104", "total_messages": "5", "message_source": "imessage", "exclude": "yes", "approved": "true", "upload_decision": "", "enrich_decision": ""},
                {"bucket": "maybe", "full_name": "Enrich Only", "phone_e164": "+105", "total_messages": "6", "message_source": "imessage", "exclude": "", "approved": "", "upload_decision": "", "enrich_decision": "yes"},
            ]
            with review.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

            ledger = {"input": {"include_existing_artifacts": True}, "artifacts": {}}
            merge_dir = tmp / "run/merged"
            with mock.patch.object(import_network_pipeline, "DEFAULT_BASE_DIR", base):
                paths = import_network_pipeline.merge_input_paths(ledger, merge_dir)

            message_inputs = [Path(path) for path in paths if path.endswith("source-inputs/messages/contacts.csv")]
            self.assertEqual(len(message_inputs), 1)
            with message_inputs[0].open(newline="", encoding="utf-8") as handle:
                materialized = list(csv.DictReader(handle))
            self.assertEqual([row["name"] for row in materialized], ["Exclude No", "Approved True", "Upload Include"])
            self.assertEqual([row["phone"] for row in materialized], ["+101", "+102", "+103"])

    def test_messages_review_people_materializer_uses_reviewed_linkedin_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review = tmp / "research_review.csv"
            output = tmp / "people.csv"
            manifest = tmp / "manifest.json"
            fields = [
                "bucket", "full_name", "phone_e164", "total_messages", "message_source",
                "imessage_message_count", "whatsapp_message_count", "exclude", "approved",
                "upload_decision", "enrich_decision", "in_network", "network_person_id",
                "network_name", "network_linkedin_url", "linkedin_url", "retarget_linkedin_url",
                "review_source", "top_title_company_pairs", "short_reason",
            ]
            rows = [
                {"bucket": "maybe", "full_name": "Network Person", "phone_e164": "+100", "total_messages": "5", "message_source": "imessage", "in_network": "true", "network_person_id": "p1", "network_name": "Network Person", "network_linkedin_url": "https://www.linkedin.com/in/network-person/"},
                {"bucket": "maybe", "full_name": "Approved Person", "phone_e164": "+101", "total_messages": "6", "message_source": "whatsapp", "approved": "true", "linkedin_url": "https://www.linkedin.com/in/approved-person/"},
                {"bucket": "maybe", "full_name": "Enrich Person", "phone_e164": "+102", "total_messages": "7", "message_source": "whatsapp", "enrich_decision": "yes", "linkedin_url": "https://www.linkedin.com/in/enrich-person/"},
                {"bucket": "maybe", "full_name": "Researched Person", "phone_e164": "+103", "total_messages": "8", "message_source": "imessage", "review_source": "llm_network_review", "linkedin_url": "https://www.linkedin.com/in/researched-person/", "top_title_company_pairs": "Founder at Example"},
                {"bucket": "yes", "full_name": "Rejected Person", "phone_e164": "+104", "total_messages": "9", "message_source": "imessage", "exclude": "yes", "linkedin_url": "https://www.linkedin.com/in/rejected-person/"},
                {"bucket": "maybe", "full_name": "Synthetic Person", "phone_e164": "+105", "total_messages": "10", "message_source": "imessage", "in_network": "true", "network_linkedin_url": "SYNTHETIC"},
                {"bucket": "maybe", "full_name": "Network Person Duplicate", "phone_e164": "+106", "total_messages": "11", "message_source": "whatsapp", "in_network": "true", "network_person_id": "p1", "network_linkedin_url": "https://www.linkedin.com/in/network-person/"},
            ]
            with review.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

            summary = import_network_pipeline.materialize_messages_review_people(review, output, manifest)

            self.assertEqual(summary["eligible_rows"], 5)
            self.assertEqual(summary["rows_written"], 4)
            self.assertEqual(summary["selection_counts"], {"in_network": 2, "approved": 1, "enrich_decision": 1, "researched": 1})
            self.assertTrue(manifest.exists())
            with output.open(newline="", encoding="utf-8") as handle:
                materialized = list(csv.DictReader(handle))
            by_public = {row["public_identifier"]: row for row in materialized}
            self.assertEqual(set(by_public), {"approved-person", "enrich-person", "network-person", "researched-person"})
            self.assertEqual(by_public["network-person"]["id"], "p1")
            self.assertEqual(json.loads(by_public["network-person"]["all_phones"]), ["+100", "+106"])
            self.assertEqual(by_public["researched-person"]["current_title"], "Founder")
            self.assertEqual(by_public["researched-person"]["current_company"], "Example")

    def test_messages_enrichment_delegates_to_enrich_people_and_records_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review = tmp / "research_review.csv"
            review.write_text(
                "bucket,full_name,phone_e164,total_messages,message_source,in_network,network_person_id,network_linkedin_url\n"
                "maybe,Network Person,+100,5,imessage,true,p1,https://www.linkedin.com/in/network-person/\n",
                encoding="utf-8",
            )
            ledger_path = tmp / "ledger.json"
            enriched = tmp / "enriched-messages.csv"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"messages_review_csv": str(review)},
                "steps": {},
                "artifacts": {},
            }
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                self.assertTrue(any("enrich_people.py" in part for part in cmd))
                input_csv = Path(cmd[cmd.index("--input") + 1])
                self.assertTrue(input_csv.exists())
                with input_csv.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[0]["public_identifier"], "network-person")
                return 0, {"status": "completed", "artifacts": {"people_csv": str(enriched)}}, ""

            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertTrue(import_network_pipeline.run_messages_enrichment(ledger_path, ledger))

            self.assertEqual(len(calls), 1)
            self.assertEqual(ledger["steps"]["messages_enrich_people"]["status"], "completed")
            self.assertEqual(ledger["artifacts"]["messages_people_csv"], str(enriched))
            self.assertTrue(Path(ledger["artifacts"]["messages_people_input_csv"]).exists())
            self.assertTrue(Path(ledger["artifacts"]["messages_people_input_manifest"]).exists())

            paths = import_network_pipeline.merge_input_paths(ledger, tmp / "merge")
            self.assertIn(str(enriched), paths)

    def test_messages_enrichment_blocks_for_child_rapidapi_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review = tmp / "research_review.csv"
            review.write_text(
                "bucket,full_name,phone_e164,total_messages,message_source,in_network,network_person_id,network_linkedin_url\n"
                "maybe,Network Person,+100,5,imessage,true,p1,https://www.linkedin.com/in/network-person/\n",
                encoding="utf-8",
            )
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"messages_review_csv": str(review)},
                "steps": {},
                "artifacts": {},
            }
            child_payload = {"status": "blocked_approval", "step_id": "enrich_linkedin", "paid_call_count": 1}
            with mock.patch.object(import_network_pipeline, "run_cmd", return_value=(20, child_payload, "")):
                self.assertFalse(import_network_pipeline.run_messages_enrichment(ledger_path, ledger))

            self.assertEqual(ledger["blocked"]["step_id"], "messages_enrich_people")
            self.assertEqual(ledger["steps"]["messages_enrich_people"]["status"], "blocked")
            self.assertEqual(ledger["blocked"]["child"], child_payload)

    def test_messages_approval_delegates_to_enrich_people_child(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            child = tmp / "child.ledger.json"
            ledger_path.write_text(json.dumps({
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "blocked": {"step_id": "messages_enrich_people", "child_ledger": str(child)},
                "steps": {},
                "artifacts": {},
            }), encoding="utf-8")
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                return 0, {"status": "approved"}, ""

            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertEqual(import_network_pipeline.cmd_approve(argparse.Namespace(ledger=str(ledger_path))), 0)

            self.assertTrue(any("enrich_people.py" in part for part in calls[0]))
            self.assertIn("approve", calls[0])
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertNotIn("blocked", saved)

    def test_merge_skips_explicit_messages_contacts_without_review_override(self) -> None:
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
            self.assertEqual(input_values, [])

    def test_merge_allows_explicit_messages_contacts_only_with_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            contacts.write_text("name,phone,source,message_count,last_message\nJane,+15551234567,imessage,3,2026-01-01\n", encoding="utf-8")
            ledger_path = tmp / "ledger.json"
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"include_existing_artifacts": False, "messages_contacts_csv": str(contacts), "allow_unreviewed_messages": True},
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
                "input": {"gmail_linkedin_provider": "harness", "linkedin_directory_csv": str(tmp / "directory.csv"), "linkedin_directory_use_defaults": False},
                "steps": {},
                "artifacts": {
                    "gmail_linkedin_resolution_queue_csvs": [
                        {"account_email": "me@example.com", "queue_csv": str(tmp / "queue-me.csv"), "people_csv": str(tmp / "people-me.csv")},
                        {"account_email": "work@example.com", "queue_csv": str(tmp / "queue-work.csv"), "people_csv": str(tmp / "people-work.csv")},
                    ]
                },
            }
            for queue_name, email in [("queue-me.csv", "me-person@example.com"), ("queue-work.csv", "work-person@example.com")]:
                import_network_pipeline.write_csv_rows(tmp / queue_name, [
                    "handle", "id", "display_name", "full_name", "primary_email", "company_guess", "primary_email_type",
                    "total_messages", "thread_count", "last_interaction", "source", "source_channels",
                ], [{"handle": email, "id": f"gmail:{email}", "display_name": "Person Example", "full_name": "Person Example", "primary_email": email}])
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                if any("resolve_linkedin_queue.py" in part for part in cmd):
                    queue = Path(cmd[cmd.index("--input") + 1]).stem
                    output = tmp / f"resolutions-{queue}.csv"
                    with output.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.DictWriter(handle, fieldnames=import_network_pipeline.LINKEDIN_RESOLUTION_COLUMNS)
                        writer.writeheader()
                        writer.writerow({
                            "handle": "person@example.com",
                            "status": "found",
                            "linkedin_url": "https://www.linkedin.com/in/person-example",
                            "confidence": "0.95",
                            "matched_name": "Person Example",
                            "matched_headline": "",
                            "evidence": "[]",
                            "reasoning": "fixture",
                        })
                    return 0, {"output": str(output)}, ""
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
            self.assertEqual(len(ledger["artifacts"]["gmail_linkedin_resolutions_csvs"]), 2)
            self.assertEqual(
                len({record["resolutions_csv"] for record in ledger["artifacts"]["gmail_linkedin_resolutions_csvs"]}),
                1,
            )
            self.assertTrue(Path(ledger["artifacts"]["gmail_linkedin_combined_queue_csv"]).exists())
            self.assertEqual(len(ledger["artifacts"]["gmail_enrich_people_ledgers"]), 2)
            self.assertEqual(len(ledger["artifacts"]["gmail_final_people_csvs"]), 2)
            resolve_cmds = [cmd for cmd in calls if any("resolve_linkedin_queue.py" in part for part in cmd)]
            self.assertEqual(len(resolve_cmds), 1)
            self.assertTrue(resolve_cmds[0][resolve_cmds[0].index("--input") + 1].endswith("gmail-combined-unresolved-queue.csv"))
            self.assertEqual(sum(1 for cmd in calls if any("gmail_network_import.py" in part for part in cmd)), 2)
            self.assertEqual(sum(1 for cmd in calls if any("enrich_people.py" in part for part in cmd)), 2)

    def test_build_directory_checkpoint_bootstraps_from_candidates_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            candidates = tmp / "linkedin_candidates_merged_test.csv"
            with candidates.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["primary_email", "display_name", "confirmed_linkedin_url"])
                writer.writeheader()
                writer.writerow({
                    "primary_email": "jane@example.com",
                    "display_name": "Jane Example",
                    "confirmed_linkedin_url": "linkedin.com/in/jane-example?trk=old",
                })
            input_cfg = {
                "linkedin_directory_csv": str(tmp / "directory.csv"),
                "linkedin_directory_source_csvs": [str(candidates)],
            }
            with mock.patch.object(import_network_pipeline, "default_directory_source_paths", return_value=[]):
                first = import_network_pipeline.build_directory_checkpoint(input_cfg, {})
                second = import_network_pipeline.build_directory_checkpoint(input_cfg, {})
            self.assertEqual(first["rows"], 1)
            self.assertEqual(second["rows"], 1)
            with (tmp / "directory.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["source_key"] for row in rows], ["email:jane@example.com"])
            self.assertEqual(rows[0]["linkedin_url"], "https://www.linkedin.com/in/jane-example")

    def test_gmail_directory_filters_provider_queue_to_unresolved_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            directory = tmp / "directory.csv"
            import_network_pipeline.write_csv_rows(directory, import_network_pipeline.DIRECTORY_COLUMNS, [{
                "source": "fixture",
                "source_key": "email:jane@example.com",
                "email": "jane@example.com",
                "phone": "",
                "name": "Jane Example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "public_identifier": "jane-example",
                "confidence": "1.00",
                "matched_name": "Jane Example",
                "matched_headline": "",
                "evidence": "",
                "reasoning": "",
                "source_artifact": "",
                "updated_at": "",
            }])
            queue = tmp / "queue.csv"
            import_network_pipeline.write_csv_rows(queue, [
                "handle", "id", "display_name", "full_name", "primary_email", "company_guess", "primary_email_type",
                "total_messages", "thread_count", "last_interaction", "source", "source_channels",
            ], [
                {"handle": "jane@example.com", "id": "gmail:jane", "display_name": "Jane Example", "full_name": "Jane Example", "primary_email": "jane@example.com"},
                {"handle": "alex@example.com", "id": "gmail:alex", "display_name": "Alex Example", "full_name": "Alex Example", "primary_email": "alex@example.com"},
            ])
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"gmail_linkedin_provider": "harness", "linkedin_directory_csv": str(directory)},
                "steps": {},
                "artifacts": {"gmail_linkedin_resolution_queue_csvs": [{"account_email": "me@example.com", "queue_csv": str(queue), "people_csv": str(tmp / "people.csv")}]},
            }
            with mock.patch.object(import_network_pipeline, "default_directory_source_paths", return_value=[]):
                self.assertTrue(import_network_pipeline.run_gmail_directory(tmp / "ledger.json", ledger))
            directory_record = ledger["artifacts"]["gmail_directory_resolution_records"][0]
            unresolved_record = ledger["artifacts"]["gmail_unresolved_linkedin_resolution_queue_csvs"][0]
            with Path(unresolved_record["queue_csv"]).open(newline="", encoding="utf-8") as handle:
                unresolved = list(csv.DictReader(handle))
            self.assertEqual(directory_record["resolved"], 1)
            self.assertEqual([row["handle"] for row in unresolved], ["alex@example.com"])
            with directory.open(newline="", encoding="utf-8") as handle:
                directory_rows = list(csv.DictReader(handle))
            observed = {row["source_key"]: row for row in directory_rows if row["status"] == "observed"}
            self.assertEqual(
                sorted(observed),
                ["gmail:me@example.com:email:alex@example.com", "gmail:me@example.com:email:jane@example.com"],
            )
            self.assertEqual({row["source_account"] for row in observed.values()}, {"me@example.com"})

            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                return 0, {"status": "prepared_harness", "prompts_jsonl": str(tmp / "prompts.jsonl"), "instructions": str(tmp / "instructions.md")}, ""
            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertTrue(import_network_pipeline.run_gmail_linkedin_resolution(tmp / "ledger.json", ledger))
            self.assertEqual(len(calls), 1)
            combined_queue = Path(calls[0][calls[0].index("--input") + 1])
            self.assertEqual(combined_queue.name, "gmail-combined-unresolved-queue.csv")
            with combined_queue.open(newline="", encoding="utf-8") as handle:
                combined_rows = list(csv.DictReader(handle))
            self.assertEqual([row["handle"] for row in combined_rows], ["alex@example.com"])

    def test_gmail_apply_combines_directory_and_provider_resolutions_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            directory_resolutions = tmp / "directory_resolutions.csv"
            provider_resolutions = tmp / "provider_resolutions.csv"
            for path, handle, slug in [
                (directory_resolutions, "jane@example.com", "jane-example"),
                (provider_resolutions, "alex@example.com", "alex-example"),
            ]:
                with path.open("w", newline="", encoding="utf-8") as handle_obj:
                    writer = csv.DictWriter(handle_obj, fieldnames=import_network_pipeline.LINKEDIN_RESOLUTION_COLUMNS)
                    writer.writeheader()
                    writer.writerow({
                        "handle": handle,
                        "status": "found",
                        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
                        "confidence": "0.95",
                        "matched_name": handle.split("@", 1)[0].title(),
                        "matched_headline": "",
                        "evidence": "[]",
                        "reasoning": "fixture",
                    })
            people = tmp / "people.csv"
            people.write_text("id,primary_email\n", encoding="utf-8")
            ledger = {
                "run_id": "network-test",
                "run_dir": str(tmp / "network-test"),
                "input": {"linkedin_directory_csv": str(tmp / "directory.csv"), "linkedin_directory_use_defaults": False},
                "steps": {},
                "artifacts": {
                    "gmail_directory_resolution_records": [{"account_email": "me@example.com", "resolutions_csv": str(directory_resolutions), "people_csv": str(people), "slug": "me"}],
                    "gmail_linkedin_resolutions_csvs": [{"account_email": "me@example.com", "resolutions_csv": str(provider_resolutions), "people_csv": str(people), "slug": "me"}],
                },
            }
            calls = []
            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                if any("gmail_network_import.py" in part for part in cmd):
                    combined = Path(cmd[cmd.index("--resolutions-csv") + 1])
                    with combined.open(newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual([row["handle"] for row in rows], ["alex@example.com", "jane@example.com"])
                    return 0, {"people_csv": str(tmp / "resolved.csv"), "resolved": 2}, ""
                if any("enrich_people.py" in part for part in cmd):
                    return 0, {"artifacts": {"people_csv": str(tmp / "enriched.csv")}}, ""
                self.fail(f"unexpected command: {cmd}")
            with mock.patch.object(import_network_pipeline, "run_cmd", side_effect=fake_run_cmd):
                self.assertTrue(import_network_pipeline.run_gmail_apply_and_enrich(tmp / "ledger.json", ledger))
            self.assertEqual(sum(1 for cmd in calls if any("gmail_network_import.py" in part for part in cmd)), 1)
            self.assertEqual(sum(1 for cmd in calls if any("enrich_people.py" in part for part in cmd)), 1)
            self.assertEqual(ledger["artifacts"]["gmail_final_people_csvs"], [str(tmp / "enriched.csv")])
            with (tmp / "directory.csv").open(newline="", encoding="utf-8") as handle:
                directory_rows = list(csv.DictReader(handle))
            self.assertEqual([row["source_key"] for row in directory_rows], ["gmail:me@example.com:email:alex@example.com", "gmail:me@example.com:email:jane@example.com"])
            self.assertEqual({row["source_account"] for row in directory_rows}, {"me@example.com"})
            self.assertEqual({row["status"] for row in directory_rows}, {"found"})
        self.assertEqual(
            import_network_pipeline.resolve_msgvault_db(argparse.Namespace(msgvault_db="", gmail_account_email="")),
            "",
        )
        self.assertEqual(
            import_network_pipeline.resolve_msgvault_db(argparse.Namespace(msgvault_db="/tmp/msgvault.db", gmail_account_email="")),
            "/tmp/msgvault.db",
        )

    def test_confirmed_people_artifacts_commit_to_directory_by_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            directory = tmp / "directory.csv"
            linkedin_people = tmp / "linkedin_people.csv"
            messages_people = tmp / "messages_people.csv"
            for path, row in [
                (linkedin_people, {
                    "id": "linkedin:one",
                    "full_name": "Linked In",
                    "linkedin_url": "https://www.linkedin.com/in/linked-in",
                    "public_identifier": "linked-in",
                    "source_channels": "linkedin_csv",
                }),
                (messages_people, {
                    "id": "message:one",
                    "full_name": "Message Person",
                    "linkedin_url": "https://www.linkedin.com/in/message-person",
                    "public_identifier": "message-person",
                    "primary_phone": "+15555550100",
                    "source_channels": "imessage,whatsapp",
                }),
            ]:
                import_network_pipeline.write_csv_rows(
                    path,
                    import_network_pipeline.PEOPLE_SCHEMA_COLUMNS,
                    [{col: row.get(col, "") for col in import_network_pipeline.PEOPLE_SCHEMA_COLUMNS}],
                )
            artifacts: dict[str, object] = {}
            input_cfg = {"linkedin_directory_csv": str(directory)}
            import_network_pipeline.commit_people_csv_to_directory(
                input_cfg,
                artifacts,
                str(linkedin_people),
                source="linkedin_csv",
                source_account="arthur",
            )
            import_network_pipeline.commit_people_csv_to_directory(
                input_cfg,
                artifacts,
                str(messages_people),
                source="messages",
            )
            with directory.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [row["source_key"] for row in rows],
                ["linkedin_csv:arthur:linkedin:linked-in", "messages:phone:+15555550100"],
            )
            self.assertEqual({row["status"] for row in rows}, {"found"})
            self.assertEqual([row["public_identifier"] for row in rows], ["linked-in", "message-person"])

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
                            "gmail_directory": {"status": "skipped"},
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

    def test_source_refresh_preserves_unselected_source_artifacts(self) -> None:
        existing = {
            "steps": {
                "gmail_msgvault": {"status": "completed"},
                "gmail_msgvault:me-example.com": {"status": "completed"},
                "linkedin": {"status": "completed"},
                "merge": {"status": "completed"},
                "duckdb": {"status": "completed"},
            },
            "source_imports": {
                "gmail_msgvault:me-example.com": {"status": "completed"},
                "linkedin": {"status": "completed"},
            },
            "artifacts": {
                "gmail_people_csvs": ["gmail.csv"],
                "linkedin_people_csv": "linkedin.csv",
                "messages_review_csv": "review.csv",
                "merged_people_csv": "merged.csv",
                "duckdb": "network.duckdb",
            },
        }

        preserved = import_network_pipeline.preserved_state_for_source_refresh(existing, {"gmail"})

        self.assertNotIn("gmail_msgvault", preserved["steps"])
        self.assertNotIn("gmail_msgvault:me-example.com", preserved["source_imports"])
        self.assertNotIn("gmail_people_csvs", preserved["artifacts"])
        self.assertEqual(preserved["steps"]["linkedin"]["status"], "completed")
        self.assertEqual(preserved["source_imports"]["linkedin"]["status"], "completed")
        self.assertEqual(preserved["artifacts"]["linkedin_people_csv"], "linkedin.csv")
        self.assertEqual(preserved["artifacts"]["messages_review_csv"], "review.csv")
        self.assertNotIn("merged_people_csv", preserved["artifacts"])
        self.assertNotIn("duckdb", preserved["artifacts"])

    def test_fan_in_preserves_all_source_artifacts(self) -> None:
        existing = {
            "steps": {
                "gmail_msgvault": {"status": "completed"},
                "linkedin": {"status": "completed"},
                "merge": {"status": "completed"},
                "duckdb": {"status": "completed"},
            },
            "source_imports": {
                "gmail_msgvault:me-example.com": {"status": "completed"},
                "linkedin": {"status": "completed"},
            },
            "artifacts": {
                "gmail_people_csvs": ["gmail.csv"],
                "linkedin_people_csv": "linkedin.csv",
                "messages_review_csv": "review.csv",
                "merged_people_csv": "merged.csv",
                "network_contacts_csv": "contacts.csv",
                "duckdb": "network.duckdb",
            },
        }

        preserved = import_network_pipeline.preserved_state_for_source_refresh(existing, set())

        self.assertEqual(preserved["steps"]["gmail_msgvault"]["status"], "completed")
        self.assertEqual(preserved["steps"]["linkedin"]["status"], "completed")
        self.assertEqual(preserved["source_imports"]["gmail_msgvault:me-example.com"]["status"], "completed")
        self.assertEqual(preserved["source_imports"]["linkedin"]["status"], "completed")
        self.assertEqual(preserved["artifacts"]["gmail_people_csvs"], ["gmail.csv"])
        self.assertEqual(preserved["artifacts"]["linkedin_people_csv"], "linkedin.csv")
        self.assertEqual(preserved["artifacts"]["messages_review_csv"], "review.csv")
        self.assertNotIn("merged_people_csv", preserved["artifacts"])
        self.assertNotIn("network_contacts_csv", preserved["artifacts"])
        self.assertNotIn("duckdb", preserved["artifacts"])

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
                    "--linkedin-directory-csv", str(tmp / "directory.csv"),
                    "--no-default-linkedin-directory-sources",
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
            with Path(artifacts["network_contacts_csv"]).open(newline="", encoding="utf-8") as handle:
                contacts = list(csv.DictReader(handle))
            self.assertEqual(contacts, [])
            with Path(artifacts["network_contact_sources_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])
            manifest = json.loads(Path(artifacts["merge_manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["filtered_without_linkedin"], 1)
            self.assertEqual(manifest["merged_rows"], 0)

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
                    "--linkedin-directory-csv", str(tmp / "directory.csv"),
                    "--no-default-linkedin-directory-sources",
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
