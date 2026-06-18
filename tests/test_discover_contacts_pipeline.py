import argparse
import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline import common as discover_common
from packs.ingestion.primitives.discover_contacts_pipeline import directory as discover_directory
from packs.ingestion.primitives.discover_contacts_pipeline import discover_contacts_pipeline
from packs.ingestion.primitives.discover_contacts_pipeline import gmail as discover_gmail
from packs.ingestion.primitives.discover_contacts_pipeline import messages as discover_messages
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


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


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class DiscoverContactsPipelineTests(unittest.TestCase):
    def test_from_accounts_populates_linked_sources(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            linkedin = tmp / "Connections.csv"
            linkedin.write_text("First Name,Last Name,URL\n", encoding="utf-8")
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "version": 2,
                "accounts": {
                    "gmail": {
                        "linked": True,
                        "usernames": ["old@example.com"],
                        "config": {"msgvault_db": str(db), "selected_accounts": ["me@example.com", "work@example.com"]},
                    },
                    "linkedin_csv": {
                        "linked": True,
                        "usernames": ["me"],
                        "config": {"csv_path": str(linkedin), "source_label": "me"},
                    },
                    "twitter": {"skipped": True, "usernames": ["stale"]},
                    "messages": {"linked": True, "config": {"review_csv": str(tmp / "research_review.csv")}},
                },
            }), encoding="utf-8")

            args = discover_contacts_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = discover_contacts_pipeline.apply_account_sources(args)

            self.assertEqual(args.msgvault_db, str(db))
            self.assertEqual(args.gmail_account_emails, ["me@example.com", "work@example.com"])
            self.assertEqual(args.linkedin_csv, str(linkedin))
            self.assertEqual(args.linkedin_source_user, "me")
            self.assertEqual(args.twitter_handle, "")
            self.assertEqual(args.messages_review_csv, str(tmp / "research_review.csv"))

    def test_pending_gmail_accounts_are_not_discover_ready_until_linked(self) -> None:
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

            args = discover_contacts_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = discover_contacts_pipeline.apply_account_sources(args)
            self.assertEqual(args.gmail_account_emails, [])
            self.assertEqual(args.msgvault_db, "")

            data = json.loads(accounts.read_text(encoding="utf-8"))
            data["accounts"]["gmail"]["linked"] = True
            data["accounts"]["gmail"]["usernames"] = ["pending@example.com"]
            data["accounts"]["gmail"]["config"]["selected_accounts"] = ["pending@example.com"]
            data["accounts"]["gmail"]["config"]["pending_accounts"] = []
            accounts.write_text(json.dumps(data), encoding="utf-8")

            args = discover_contacts_pipeline.build_parser().parse_args(["run", "--from-accounts", str(accounts), "--dry-run"])
            args = discover_contacts_pipeline.apply_account_sources(args)
            self.assertEqual(args.gmail_account_emails, ["pending@example.com"])
            self.assertEqual(args.msgvault_db, str(tmp / "msgvault.db"))

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
            calls: list[list[str]] = []

            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                return 0, {"status": "completed", "messages_added": 0}, ""

            with mock.patch.object(discover_gmail.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_run_cmd):
                    payload = discover_gmail.sync_msgvault_account("me@example.com", str(db), "-category:social")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["sync_after"], "2026-05-20")
            self.assertEqual(payload["sync_after_source"], "msgvault.sources.last_sync_at")
            self.assertEqual(calls, [["msgvault", "--home", str(tmp), "sync-full", "me@example.com", "--after", "2026-05-20", "--query", "-category:social"]])

    def test_gmail_discovery_writes_only_stable_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "accounts": {
                    "gmail": {
                        "linked": True,
                        "config": {"selected_accounts": ["me@example.com"], "msgvault_db": str(tmp / "msgvault.db")},
                    }
                }
            }), encoding="utf-8")
            account_dir = tmp / "discover/gmail/me-example.com"
            account_queue = account_dir / "linkedin_resolution_queue.csv"
            account_people = account_dir / "people.csv"
            write_csv(
                account_queue,
                discover_gmail.GMAIL_DISCOVERY_COLUMNS,
                [{
                    "handle": "jane@example.com",
                    "id": "gmail:jane@example.com",
                    "account_emails": json.dumps(["me@example.com"]),
                    "source_ids": json.dumps(["gmail:jane@example.com"]),
                    "display_name": "Jane Example",
                    "full_name": "Jane Example",
                    "primary_email": "jane@example.com",
                    "source": "gmail_msgvault",
                    "source_channels": "gmail_msgvault",
                    "total_messages": "2",
                    "thread_count": "1",
                    "last_interaction": "2026-01-02T00:00:00Z",
                }],
            )
            write_csv(
                account_people,
                PEOPLE_SCHEMA_COLUMNS,
                [{
                    **{column: "" for column in PEOPLE_SCHEMA_COLUMNS},
                    "id": "gmail:me@example.com:email:jane@example.com",
                    "full_name": "Jane Example",
                    "primary_email": "jane@example.com",
                    "source_channels": "gmail_msgvault",
                    "interaction_counts": json.dumps({"gmail": 2}),
                    "last_interaction": "2026-01-02T00:00:00Z",
                }],
            )

            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }

            def fake_output_path(source: str, key: str) -> Path:
                return paths[(source, key)]

            def fake_run_cmd(cmd, timeout=None):
                self.assertIn("--output-dir", cmd)
                self.assertEqual(Path(cmd[cmd.index("--output-dir") + 1]), tmp)
                return 0, {
                    "status": "completed",
                    "artifact_dir": str(account_dir),
                    "artifacts": {
                        "linkedin_resolution_queue_csv": str(account_queue),
                        "people_csv": str(account_people),
                    },
                    "counts": {"contacts_written": 1},
                }, ""

            with mock.patch.object(discover_gmail, "output_path", side_effect=fake_output_path):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_run_cmd):
                        payload = discover_gmail.discover(accounts_file=accounts)

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["contacts"], 1)
            self.assertEqual(payload["contacts_csv"], str(paths[("gmail", "contacts_csv")]))
            self.assertEqual(payload["linkedin_resolution_queue_csv"], str(paths[("gmail", "linkedin_resolution_queue_csv")]))
            manifest = json.loads(paths[("gmail", "manifest_json")].read_text(encoding="utf-8"))
            self.assertNotIn("payload", manifest["children"][0])
            self.assertNotIn("/raw/", json.dumps(manifest))
            self.assertEqual(manifest["children"][0]["artifact_dir"], str(account_dir))
            self.assertEqual(manifest["children"][0]["people_csv"], str(account_people))
            self.assertEqual(manifest["children"][0]["linkedin_resolution_queue_csv"], str(account_queue))
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in rows], ["jane@example.com"])
            self.assertEqual(rows[0]["total_messages"], "2")
            self.assertEqual(manifest["calculation_version"], discover_gmail.GMAIL_INTERACTION_CALCULATION_VERSION)
            self.assertEqual(manifest["calculation_mode"], "full_rewrite")
            self.assertEqual(manifest["calculation_reason"], "calculation_version_changed")

            with mock.patch.object(discover_gmail, "output_path", side_effect=fake_output_path):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_run_cmd):
                        payload = discover_gmail.discover(accounts_file=accounts)

            self.assertEqual(payload["status"], "completed")
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                rerun_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in rerun_rows], ["jane@example.com"])
            self.assertEqual(rerun_rows[0]["total_messages"], "2")
            self.assertEqual(rerun_rows[0]["thread_count"], "1")

            scratch_queue = account_queue
            write_csv(
                scratch_queue,
                discover_gmail.GMAIL_DISCOVERY_COLUMNS,
                [{
                    "handle": "jane@example.com",
                    "id": "gmail:jane@example.com",
                    "account_emails": json.dumps(["me@example.com"]),
                    "source_ids": json.dumps(["gmail:jane@example.com"]),
                    "display_name": "Jane Example",
                    "full_name": "Jane Example",
                    "primary_email": "jane@example.com",
                    "source": "gmail_msgvault",
                    "source_channels": "gmail_msgvault",
                    "total_messages": "1",
                    "thread_count": "1",
                    "last_interaction": "2026-01-03T00:00:00Z",
                }],
            )

            def fake_incremental_run_cmd(cmd, timeout=None):
                return 0, {
                    "status": "completed",
                    "calculation_mode": discover_gmail.GMAIL_CALCULATION_INCREMENTAL_DELTA,
                    "artifacts": {
                        "linkedin_resolution_queue_csv": str(scratch_queue),
                        "people_csv": str(tmp / "scratch" / "people.csv"),
                    },
                    "counts": {"contacts_written": 1},
                }, ""

            with mock.patch.object(discover_gmail, "output_path", side_effect=fake_output_path):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_incremental_run_cmd):
                        payload = discover_gmail.discover(accounts_file=accounts)

            self.assertEqual(payload["status"], "completed")
            manifest = json.loads(paths[("gmail", "manifest_json")].read_text(encoding="utf-8"))
            self.assertEqual(manifest["calculation_mode"], "incremental_update")
            self.assertEqual(manifest["calculation_reason"], "children_returned_incremental_deltas")
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                incremental_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(incremental_rows[0]["total_messages"], "3")
            self.assertEqual(incremental_rows[0]["thread_count"], "2")
            self.assertEqual(incremental_rows[0]["last_interaction"], "2026-01-03T00:00:00Z")

            with mock.patch.object(discover_gmail, "output_path", side_effect=fake_output_path):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_incremental_run_cmd):
                        replay_payload = discover_gmail.discover(accounts_file=accounts)

            self.assertEqual(replay_payload["status"], "completed")
            replay_manifest = json.loads(paths[("gmail", "manifest_json")].read_text(encoding="utf-8"))
            self.assertEqual(replay_manifest["calculation_mode"], "incremental_update")
            self.assertEqual(len(replay_manifest["applied_incremental_inputs"]), 1)
            self.assertEqual(replay_manifest["skipped_incremental_inputs"], replay_manifest["applied_incremental_inputs"])
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                replay_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(replay_rows[0]["total_messages"], "3")
            self.assertEqual(replay_rows[0]["thread_count"], "2")
            self.assertEqual(replay_rows[0]["last_interaction"], "2026-01-03T00:00:00Z")

    def test_gmail_incremental_requires_existing_full_recount_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "accounts": {
                    "gmail": {
                        "linked": True,
                        "config": {"selected_accounts": ["me@example.com"], "msgvault_db": str(tmp / "msgvault.db")},
                    }
                }
            }), encoding="utf-8")
            scratch_queue = tmp / "scratch" / "queue-me@example.com.csv"
            write_csv(
                scratch_queue,
                discover_gmail.GMAIL_DISCOVERY_COLUMNS,
                [{
                    "handle": "jane@example.com",
                    "id": "gmail:jane@example.com",
                    "account_emails": json.dumps(["me@example.com"]),
                    "source_ids": json.dumps(["gmail:jane@example.com"]),
                    "display_name": "Jane Example",
                    "full_name": "Jane Example",
                    "primary_email": "jane@example.com",
                    "source": "gmail_msgvault",
                    "source_channels": "gmail_msgvault",
                    "total_messages": "1",
                    "thread_count": "1",
                    "last_interaction": "2026-01-03T00:00:00Z",
                }],
            )
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }

            def fake_incremental_run_cmd(cmd, timeout=None):
                return 0, {
                    "status": "completed",
                    "calculation_mode": discover_gmail.GMAIL_CALCULATION_INCREMENTAL_DELTA,
                    "artifacts": {"linkedin_resolution_queue_csv": str(scratch_queue)},
                    "counts": {"contacts_written": 1},
                }, ""

            with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_incremental_run_cmd):
                        payload = discover_gmail.discover(accounts_file=accounts)

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["calculation_mode"], "full_rewrite")
            self.assertEqual(payload["calculation_reason"], "full_rewrite_requires_full_recount_children")
            self.assertFalse(paths[("gmail", "linkedin_resolution_queue_csv")].exists())

    def test_gmail_incremental_replay_after_full_recount_boundary_does_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "accounts": {
                    "gmail": {
                        "linked": True,
                        "config": {"selected_accounts": ["me@example.com"], "msgvault_db": str(tmp / "msgvault.db")},
                    }
                }
            }), encoding="utf-8")
            scratch_queue = tmp / "scratch" / "queue-me@example.com.csv"
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }

            def run_with_child(mode: str, *, total_messages: str, thread_count: str, last_interaction: str) -> dict[str, object]:
                write_csv(
                    scratch_queue,
                    discover_gmail.GMAIL_DISCOVERY_COLUMNS,
                    [{
                        "handle": "jane@example.com",
                        "id": "gmail:jane@example.com",
                        "account_emails": json.dumps(["me@example.com"]),
                        "source_ids": json.dumps(["gmail:jane@example.com"]),
                        "display_name": "Jane Example",
                        "full_name": "Jane Example",
                        "primary_email": "jane@example.com",
                        "source": "gmail_msgvault",
                        "source_channels": "gmail_msgvault",
                        "total_messages": total_messages,
                        "thread_count": thread_count,
                        "last_interaction": last_interaction,
                    }],
                )

                def fake_run_cmd(cmd, timeout=None):
                    child = {
                        "status": "completed",
                        "calculation_mode": mode,
                        "artifacts": {"linkedin_resolution_queue_csv": str(scratch_queue)},
                        "counts": {"contacts_written": 1},
                    }
                    return 0, child, ""

                with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                    with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                        with mock.patch.object(discover_gmail, "run_cmd", side_effect=fake_run_cmd):
                            return discover_gmail.discover(accounts_file=accounts)

            full_payload = run_with_child(
                discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="2",
                thread_count="1",
                last_interaction="2026-01-02T00:00:00Z",
            )
            self.assertEqual(full_payload["status"], "completed")
            self.assertEqual(full_payload["calculation_mode"], "full_rewrite")
            self.assertEqual(full_payload["applied_incremental_inputs"], [])

            incremental_payload = run_with_child(
                discover_gmail.GMAIL_CALCULATION_INCREMENTAL_DELTA,
                total_messages="1",
                thread_count="1",
                last_interaction="2026-01-03T00:00:00Z",
            )
            self.assertEqual(incremental_payload["status"], "completed")
            self.assertEqual(incremental_payload["calculation_mode"], "incremental_update")
            self.assertEqual(len(incremental_payload["applied_incremental_inputs"]), 1)
            first_input = incremental_payload["applied_incremental_inputs"][0]
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                incremental_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(incremental_rows[0]["total_messages"], "3")
            self.assertEqual(incremental_rows[0]["thread_count"], "2")

            replay_payload = run_with_child(
                discover_gmail.GMAIL_CALCULATION_INCREMENTAL_DELTA,
                total_messages="1",
                thread_count="1",
                last_interaction="2026-01-03T00:00:00Z",
            )
            self.assertEqual(replay_payload["status"], "completed")
            self.assertEqual(replay_payload["skipped_incremental_inputs"], [first_input])
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                replay_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(replay_rows[0]["total_messages"], "3")
            self.assertEqual(replay_rows[0]["thread_count"], "2")

            full_recount_payload = run_with_child(
                discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="3",
                thread_count="2",
                last_interaction="2026-01-04T00:00:00Z",
            )
            self.assertEqual(full_recount_payload["status"], "completed")
            self.assertEqual(full_recount_payload["calculation_mode"], "full_rewrite")
            self.assertEqual(full_recount_payload["applied_incremental_inputs"], [first_input])

            stale_replay_payload = run_with_child(
                discover_gmail.GMAIL_CALCULATION_INCREMENTAL_DELTA,
                total_messages="1",
                thread_count="1",
                last_interaction="2026-01-03T00:00:00Z",
            )
            self.assertEqual(stale_replay_payload["status"], "completed")
            self.assertEqual(stale_replay_payload["skipped_incremental_inputs"], [first_input])
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                stale_replay_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(stale_replay_rows[0]["total_messages"], "3")
            self.assertEqual(stale_replay_rows[0]["thread_count"], "2")

    def test_gmail_incremental_input_ids_are_namespaced_by_account(self) -> None:
        rows: list[dict[str, str]] = []
        self.assertNotEqual(
            discover_gmail.gmail_incremental_input_id("one@example.com", rows),
            discover_gmail.gmail_incremental_input_id("two@example.com", rows),
        )

    def test_gmail_discovery_ignores_missing_child_queue_instead_of_reading_dot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "accounts": {
                    "gmail": {
                        "linked": True,
                        "config": {"selected_accounts": ["me@example.com"], "msgvault_db": str(tmp / "msgvault.db")},
                    }
                }
            }), encoding="utf-8")
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }

            with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail, "run_cmd", return_value=(0, {"status": "completed", "artifacts": {}}, "")):
                        payload = discover_gmail.discover(accounts_file=accounts)

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["contacts"], 0)
            self.assertTrue(paths[("gmail", "linkedin_resolution_queue_csv")].exists())

    def test_source_workers_discover_sources_only_without_fan_in_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ledger_path = tmp / "ledger.json"
            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({"accounts": {}}), encoding="utf-8")
            ledger = {
                "artifact_dir": str(tmp / "run"),
                "input": {
                    "from_accounts": str(accounts),
                    "gmail_account_emails": ["me@example.com"],
                    "linkedin_csv": str(tmp / "Connections.csv"),
                    "linkedin_source_user": "me",
                    "messages_review_csv": str(tmp / "review.csv"),
                },
                "steps": {},
                "artifacts": {},
            }

            with mock.patch.object(discover_contacts_pipeline.gmail, "discover", return_value={"status": "completed", "contacts_csv": "gmail_contacts.csv", "linkedin_resolution_queue_csv": "gmail_queue.csv"}):
                with mock.patch.object(discover_contacts_pipeline.linkedin, "discover", return_value={"status": "completed", "contacts_csv": "linkedin_contacts.csv"}):
                    with mock.patch.object(discover_contacts_pipeline.messages, "discover", return_value={"status": "completed", "contacts_csv": "messages_contacts.csv"}):
                        ok = discover_contacts_pipeline.run_source_import_workers(ledger_path, ledger)

            self.assertTrue(ok)
            self.assertEqual(ledger["steps"]["source_imports"]["status"], "completed")
            self.assertEqual(ledger["artifacts"]["gmail_contacts_csv"], "gmail_contacts.csv")
            self.assertEqual(ledger["artifacts"]["linkedin_contacts_csv"], "linkedin_contacts.csv")
            self.assertEqual(ledger["artifacts"]["messages_contacts_csv"], "messages_contacts.csv")
            self.assertNotIn("merged_people_csv", ledger["artifacts"])
            self.assertNotIn("duckdb", ledger["artifacts"])

    def test_materialize_approved_messages_review_uses_only_explicit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review = tmp / "research_review.csv"
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
            ]
            write_csv(review, fields, rows)

            scratch = tmp / "contacts.csv"
            summary = discover_messages.materialize_approved_messages_review(review, scratch)
            self.assertEqual(summary["contacts_csv"], str(scratch))
            with scratch.open(newline="", encoding="utf-8") as handle:
                materialized = list(CsvIO.dict_reader(handle))
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
                {"bucket": "yes", "full_name": "Rejected Person", "phone_e164": "+104", "total_messages": "9", "message_source": "imessage", "exclude": "yes", "linkedin_url": "https://www.linkedin.com/in/rejected-person/"},
                {"bucket": "maybe", "full_name": "Network Person Duplicate", "phone_e164": "+106", "total_messages": "11", "message_source": "whatsapp", "in_network": "true", "network_person_id": "p1", "network_linkedin_url": "https://www.linkedin.com/in/network-person/"},
            ]
            write_csv(review, fields, rows)

            summary = discover_messages.materialize_messages_review_people(review, output, manifest)

            self.assertEqual(summary["eligible_rows"], 4)
            self.assertEqual(summary["rows_written"], 3)
            with output.open(newline="", encoding="utf-8") as handle:
                materialized = list(CsvIO.dict_reader(handle))
            by_public = {row["public_identifier"]: row for row in materialized}
            self.assertEqual(set(by_public), {"approved-person", "enrich-person", "network-person"})
            self.assertEqual(json.loads(by_public["network-person"]["all_phones"]), ["+100", "+106"])

    def test_commit_people_csv_to_directory_records_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            directory = tmp / "directory.csv"
            people = tmp / "people.csv"
            discover_common.write_csv_rows(
                people,
                PEOPLE_SCHEMA_COLUMNS,
                [{
                    **{col: "" for col in PEOPLE_SCHEMA_COLUMNS},
                    "id": "linkedin:one",
                    "full_name": "Linked In",
                    "linkedin_url": "https://www.linkedin.com/in/linked-in",
                    "public_identifier": "linked-in",
                    "primary_email": "linked@example.com",
                    "source_channels": "gmail_msgvault",
                }],
            )
            artifacts: dict[str, object] = {}
            checkpoint = discover_directory.commit_people_csv_to_directory(
                {"linkedin_directory_csv": str(directory)},
                artifacts,
                str(people),
                source="gmail",
                source_account="me@example.com",
            )
            self.assertEqual(checkpoint["imported_rows"], 1)
            with directory.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["source"], "gmail")
            self.assertEqual(rows[0]["source_account"], "me@example.com")
            self.assertEqual(rows[0]["status"], "found")
            self.assertEqual(rows[0]["public_identifier"], "linked-in")

    def test_source_refresh_preserves_unselected_source_state_without_fan_in_reset(self) -> None:
        existing = {
            "steps": {
                "gmail_msgvault": {"status": "completed"},
                "linkedin": {"status": "completed"},
                "messages": {"status": "completed"},
            },
            "source_imports": {
                "gmail_msgvault:me-example.com": {"status": "completed"},
                "linkedin": {"status": "completed"},
            },
            "artifacts": {
                "gmail_contacts_csv": "gmail.csv",
                "linkedin_contacts_csv": "linkedin.csv",
                "messages_contacts_csv": "messages.csv",
                "merged_people_csv": "merged.csv",
            },
        }

        preserved = discover_contacts_pipeline.preserved_state_for_source_refresh(existing, {"gmail"})

        self.assertNotIn("gmail_msgvault", preserved["steps"])
        self.assertNotIn("gmail_contacts_csv", preserved["artifacts"])
        self.assertEqual(preserved["steps"]["linkedin"]["status"], "completed")
        self.assertEqual(preserved["steps"]["messages"]["status"], "completed")
        self.assertEqual(preserved["artifacts"]["linkedin_contacts_csv"], "linkedin.csv")
        self.assertEqual(preserved["artifacts"]["messages_contacts_csv"], "messages.csv")
        self.assertEqual(preserved["artifacts"]["merged_people_csv"], "merged.csv")


if __name__ == "__main__":
    unittest.main()
