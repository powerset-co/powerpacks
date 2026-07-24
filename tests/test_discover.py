import csv
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.discover import common as discover_common
from packs.ingestion.primitives.imports import directory as import_directory
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO

discover_gmail = importlib.import_module(
    "packs.ingestion.primitives.discover.gmail.discover"
)
discover_gmail_sync = importlib.import_module(
    "packs.ingestion.primitives.discover.gmail.msgvault.sync"
)
discover_gmail_util = importlib.import_module(
    "packs.ingestion.primitives.discover.gmail.util"
)
extract_gmail = importlib.import_module(
    "packs.ingestion.primitives.discover.gmail.extract_gmail"
)
common_proc = importlib.import_module(
    "packs.ingestion.primitives.common.proc"
)
common_jsonio = importlib.import_module(
    "packs.ingestion.primitives.common.jsonio"
)
import_messages = importlib.import_module(
    "packs.ingestion.primitives.imports.messages.importer"
)


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


class ParseLastJsonTests(unittest.TestCase):
    """The one `parse_last_json`, after the divergent whatsapp_wacli fork folded in."""

    def test_returns_last_top_level_object_after_progress_lines(self) -> None:
        stdout = 'syncing Jordan Bravo\n{"step": 1}\nstill syncing\n{"status": "ok"}\n'
        self.assertEqual(common_jsonio.parse_last_json(stdout), {"status": "ok"})

    def test_scans_forward_past_malformed_json_between_objects(self) -> None:
        # The promoted wacli behavior: a truncated/garbled object mid-stream no
        # longer ends the scan, so the real trailing payload still wins.
        stdout = '{"step": 1}\n{"truncated": \n{"status": "ok", "contacts": 2}\n'
        self.assertEqual(
            common_jsonio.parse_last_json(stdout),
            {"status": "ok", "contacts": 2},
        )

    def test_scans_forward_past_non_json_noise_between_objects(self) -> None:
        stdout = '{"step": 1}\n\x00\x01 binary noise {not json at all\n{"status": "ok"}\n'
        self.assertEqual(common_jsonio.parse_last_json(stdout), {"status": "ok"})

    def test_keeps_earlier_object_when_nothing_decodes_after_the_garbage(self) -> None:
        stdout = '{"status": "ok"}\ntrailing {garbage that never closes\n'
        self.assertEqual(common_jsonio.parse_last_json(stdout), {"status": "ok"})

    def test_empty_or_json_free_output_is_an_empty_dict(self) -> None:
        for stdout in ("", "   \n", "no json here at all\n"):
            with self.subTest(stdout=stdout):
                self.assertEqual(common_jsonio.parse_last_json(stdout), {})


class DiscoverContactsPipelineTests(unittest.TestCase):
    def test_child_commands_do_not_inherit_interactive_stdin(self) -> None:
        proc = mock.Mock()
        proc.stdout = io.StringIO('{"status": "ok"}\n')
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0

        with mock.patch.object(common_proc.subprocess, "Popen", return_value=proc) as popen:
            code, payload, stderr = common_proc.run_cmd(["fake-child"])

        self.assertEqual(code, 0)
        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(stderr, "")
        self.assertEqual(popen.call_args.kwargs["stdin"], common_proc.subprocess.DEVNULL)

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

            with mock.patch.object(discover_gmail_sync.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(discover_gmail_sync, "run_cmd", side_effect=fake_run_cmd):
                    payload = discover_gmail_sync.sync_msgvault_account("me@example.com", str(db), "-category:social")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["sync_after"], "2026-05-20")
            self.assertEqual(payload["sync_after_source"], "msgvault.sources.last_sync_at")
            self.assertEqual(calls, [["msgvault", "--home", str(tmp), "sync-full", "me@example.com", "--after", "2026-05-20", "--query", "-category:social"]])

    def test_gmail_sync_expired_token_returns_actionable_reauthorization_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "msgvault.db"
            calls: list[list[str]] = []
            expired_error = (
                "token for me@example.com is expired or revoked, but cannot re-authorize "
                "in a non-interactive session"
            )

            def fake_run_cmd(cmd, timeout=None):
                calls.append(cmd)
                return 1, {}, expired_error

            with mock.patch.object(discover_gmail_sync.shutil, "which", return_value="/usr/bin/msgvault"):
                with mock.patch.object(discover_gmail_sync, "run_cmd", side_effect=fake_run_cmd):
                    payload = discover_gmail_sync.sync_msgvault_account(
                        "me@example.com",
                        str(db),
                        "-category:social",
                        sync_after_override="2023-07-15",
                    )

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error_code"], "gmail_reauthorization_required")
            self.assertEqual(payload["account_email"], "me@example.com")
            self.assertIn("me@example.com", payload["error"])
            self.assertIn("--force-auth", payload["reauthorize_command"])
            self.assertEqual(payload["error_detail"], expired_error)
            self.assertEqual(len(calls), 1)
            self.assertIn("--after", calls[0])
            self.assertEqual(calls[0][calls[0].index("--after") + 1], "2023-07-15")
            self.assertIn("--noresume", calls[0])
            self.assertNotIn("add-account", calls[0])

    def test_gmail_cli_returns_nonzero_when_discovery_fails(self) -> None:
        fake_store = mock.Mock()
        fake_store.run.return_value = {"status": "failed"}
        with mock.patch.object(discover_gmail, "GmailDiscovery", return_value=fake_store):
            with mock.patch.object(discover_gmail, "emit"):
                with mock.patch.object(sys, "argv", ["gmail.py", "discover"]):
                    self.assertEqual(discover_gmail.main(), 1)

    def test_gmail_discovery_writes_only_stable_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
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

            def fake_run_msgvault(engine, *, db, account_email, output_dir):
                self.assertEqual(Path(output_dir), tmp)
                return {
                    "status": "completed",
                    "artifact_dir": str(account_dir),
                    "artifacts": {
                        "linkedin_resolution_queue_csv": str(account_queue),
                        "people_csv": str(account_people),
                    },
                    "counts": {"contacts_written": 1},
                }

            with mock.patch.object(discover_gmail, "output_path", side_effect=fake_output_path):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com", "messages_added": 4}):
                    with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault", fake_run_msgvault):
                        payload = discover_gmail.GmailDiscovery(account_emails=["me@example.com"]).run()

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["contacts"], 1)
            datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
            self.assertGreaterEqual(payload["duration_seconds"], 0)
            self.assertEqual(payload["accounts_timing"][0]["email"], "me@example.com")
            self.assertEqual(payload["accounts_timing"][0]["messages_added"], 4)
            self.assertGreaterEqual(payload["accounts_timing"][0]["duration_seconds"], 0)
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
            # First run: contacts.csv does not exist yet, so the empty-output
            # branch decides this before the calculation-version check does.
            self.assertEqual(manifest["calculation_reason"], "empty_output")

            with mock.patch.object(discover_gmail, "output_path", side_effect=fake_output_path):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault", fake_run_msgvault):
                        payload = discover_gmail.GmailDiscovery(account_emails=["me@example.com"]).run()

            self.assertEqual(payload["status"], "completed")
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                rerun_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in rerun_rows], ["jane@example.com"])
            self.assertEqual(rerun_rows[0]["total_messages"], "2")
            self.assertEqual(rerun_rows[0]["thread_count"], "1")

    def test_gmail_merge_plan_policy_order(self) -> None:
        """Every branch is a full rewrite — a child's rows restate its account's
        whole truth — so only the ordered diagnostic reason varies."""
        plan = discover_gmail_util.gmail_discovery_merge_plan
        version = discover_gmail_util.GMAIL_INTERACTION_CALCULATION_VERSION
        current = {"calculation_version": version, "account_emails": ["me@example.com"]}
        accounts = ["me@example.com"]

        # An empty/missing output wins over every other branch.
        for rows in (0, -1):
            self.assertEqual(
                plan(current, accounts, output_rows=rows),
                {"mode": "full_rewrite", "reason": "empty_output"},
            )
        self.assertEqual(
            plan(current, accounts, output_rows=5, full_rerun_requested=True),
            {"mode": "full_rewrite", "reason": "full_rerun_requested"},
        )
        self.assertEqual(
            plan({"calculation_version": "older-version"}, accounts, output_rows=5),
            {"mode": "full_rewrite", "reason": "calculation_version_changed"},
        )
        self.assertEqual(
            plan({"calculation_version": version, "account_emails": ["other@example.com"]},
                 accounts, output_rows=5),
            {"mode": "full_rewrite", "reason": "account_emails_changed"},
        )
        # The ordinary case: a populated, still-valid output is rebuilt anyway.
        self.assertEqual(
            plan(current, accounts, output_rows=5),
            {"mode": "full_rewrite", "reason": "children_returned_full_recounts"},
        )

    def test_gmail_extractor_declares_full_recount_calculation_mode(self) -> None:
        """The real producer contract: extract_gmail re-derives whole-store totals
        from the entire archive, so it declares full_recount in BOTH its manifest
        and its returned payload. Nothing in the pipeline emits incremental_delta."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "msgvault.db"
            write_msgvault_db(db)
            payload = extract_gmail.GmailExtractor().run_msgvault(
                db=str(db), account_email="me@example.com", output_dir=str(tmp),
            )
            self.assertEqual(
                payload["calculation_mode"],
                discover_gmail_util.GMAIL_CALCULATION_FULL_RECOUNT,
            )
            child_manifest = json.loads(
                (tmp / "discover/gmail/me-example.com/manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                child_manifest["calculation_mode"],
                discover_gmail_util.GMAIL_CALCULATION_FULL_RECOUNT,
            )

    def _run_gmail_discovery(self, tmp: Path, paths: dict, mode: str, *, total_messages: str,
                             thread_count: str, last_interaction: str, fresh: bool = False) -> dict:
        """Run GmailDiscovery once against a staged child queue at the fixed path."""
        scratch_queue = tmp / "discover" / "gmail" / "me-example.com" / "linkedin_resolution_queue.csv"
        write_csv(
            scratch_queue,
            discover_gmail.GMAIL_DISCOVERY_COLUMNS,
            [{
                "handle": "jordan@example.com",
                "id": "gmail:jordan@example.com",
                "account_emails": json.dumps(["me@example.com"]),
                "source_ids": json.dumps(["gmail:jordan@example.com"]),
                "display_name": "Jordan Bravo",
                "full_name": "Jordan Bravo",
                "primary_email": "jordan@example.com",
                "source": "gmail_msgvault",
                "source_channels": "gmail_msgvault",
                "total_messages": total_messages,
                "thread_count": thread_count,
                "last_interaction": last_interaction,
            }],
        )

        def fake_run_msgvault(engine, *, db, account_email, output_dir):
            return {
                "status": "completed",
                "calculation_mode": mode,
                "artifacts": {"linkedin_resolution_queue_csv": str(scratch_queue)},
                "counts": {"contacts_written": 1},
            }

        with mock.patch.object(discover_gmail, "output_path", side_effect=lambda s, k: paths[(s, k)]):
            with mock.patch.object(discover_gmail, "sync_msgvault_account",
                                   return_value={"status": "completed", "account_email": "me@example.com"}):
                with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault", fake_run_msgvault):
                    return discover_gmail.GmailDiscovery(
                        account_emails=["me@example.com"], fresh=fresh,
                    ).run()

    @staticmethod
    def _gmail_paths(tmp: Path) -> dict:
        return {
            ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
            ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
            ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
        }

    def test_gmail_empty_or_missing_output_forces_full_rewrite(self) -> None:
        """A surviving manifest is not enough to append to: with contacts.csv gone
        or header-only, the plan must rebuild rather than treat deltas as new."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paths = self._gmail_paths(tmp)
            # Seed a manifest whose calculation_version + account_emails match, so
            # only the new empty-output branch can explain the full rewrite.
            self._run_gmail_discovery(
                tmp, paths, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="10", thread_count="3", last_interaction="2026-01-02T00:00:00Z",
            )
            self.assertTrue(paths[("gmail", "contacts_csv")].exists())

            # contacts.csv deleted, manifest.json survives.
            paths[("gmail", "contacts_csv")].unlink()
            payload = self._run_gmail_discovery(
                tmp, paths, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="10", thread_count="3", last_interaction="2026-01-02T00:00:00Z",
            )
            self.assertEqual(payload["calculation_mode"], "full_rewrite")
            self.assertEqual(payload["calculation_reason"], "empty_output")

            # Header-only contacts.csv counts as empty too.
            write_csv(paths[("gmail", "contacts_csv")], discover_gmail.GMAIL_DISCOVERY_COLUMNS, [])
            payload = self._run_gmail_discovery(
                tmp, paths, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="10", thread_count="3", last_interaction="2026-01-02T00:00:00Z",
            )
            self.assertEqual(payload["calculation_mode"], "full_rewrite")
            self.assertEqual(payload["calculation_reason"], "empty_output")

    def test_gmail_fresh_requests_a_full_rerun(self) -> None:
        """--fresh is the explicit full-rerun door and is recorded as the reason."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paths = self._gmail_paths(tmp)
            self._run_gmail_discovery(
                tmp, paths, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="10", thread_count="3", last_interaction="2026-01-02T00:00:00Z",
            )
            # Populated output + matching manifest: without --fresh the reason is
            # the ordinary full-recount one.
            baseline = self._run_gmail_discovery(
                tmp, paths, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="10", thread_count="3", last_interaction="2026-01-02T00:00:00Z",
            )
            self.assertEqual(baseline["calculation_reason"], "children_returned_full_recounts")

            fresh_payload = self._run_gmail_discovery(
                tmp, paths, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                total_messages="10", thread_count="3", last_interaction="2026-01-02T00:00:00Z",
                fresh=True,
            )
            self.assertEqual(fresh_payload["calculation_mode"], "full_rewrite")
            self.assertEqual(fresh_payload["calculation_reason"], "full_rerun_requested")
            self.assertTrue(discover_gmail.GmailDiscovery(
                account_emails=["me@example.com"], fresh=True,
            ).full_rerun_requested)

    def test_gmail_discovery_ignores_missing_child_queue_instead_of_reading_dot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }

            with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault", return_value={"status": "completed", "artifacts": {}}):
                        payload = discover_gmail.GmailDiscovery(account_emails=["me@example.com"]).run()

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["contacts"], 0)
            self.assertTrue(paths[("gmail", "linkedin_resolution_queue_csv")].exists())

    def test_gmail_account_channel_records_contribution_from_child_queue(self) -> None:
        # Exercise GmailAccountChannel directly (the #320 channel/store split):
        # it syncs, spawns the engine child, and reads this account's rows back
        # from its FIXED queue_csv, recording what it contributed on self.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            channel = discover_gmail.GmailAccountChannel(
                account_email="me@example.com",
                output_base=tmp,
                msgvault_db=str(tmp / "msgvault.db"),
                sync_query="-category:social",
            )
            write_csv(
                channel.queue_csv,
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

            def fake_run_msgvault(engine, *, db, account_email, output_dir):
                self.assertEqual(Path(output_dir), tmp)
                self.assertEqual(account_email, "me@example.com")
                return {
                    "status": "completed",
                    "calculation_mode": discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                    "artifacts": {"linkedin_resolution_queue_csv": str(channel.queue_csv)},
                    "counts": {"contacts_written": 1},
                }

            with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}) as sync_mock:
                with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault", fake_run_msgvault):
                    result = channel.run()

            self.assertIsNone(result)
            sync_mock.assert_called_once()
            self.assertEqual(channel.mode, discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT)
            self.assertEqual([row["primary_email"] for row in channel.rows], ["jane@example.com"])
            self.assertEqual(channel.artifacts["linkedin_resolution_queue_csv"], str(channel.queue_csv))
            self.assertEqual(channel.artifacts["people_csv"], str(channel.people_csv))
            self.assertEqual(channel.record["account_email"], "me@example.com")
            self.assertEqual(channel.record["rows_read"], 1)
            self.assertEqual(channel.record["artifact_dir"], str(channel.discover_dir))
            self.assertEqual(channel.output, {
                "account_email": "me@example.com",
                "calculation_mode": discover_gmail.GMAIL_CALCULATION_FULL_RECOUNT,
                "rows": channel.rows,
            })

    def test_gmail_account_channel_failed_sync_short_circuits_before_child(self) -> None:
        # A failed msgvault sync stops the channel before the engine child spawns.
        channel = discover_gmail.GmailAccountChannel(
            account_email="me@example.com",
            output_base=Path("/nonexistent"),
            msgvault_db="/nonexistent/msgvault.db",
            sync_query="",
        )
        failed_sync = {"status": "failed", "account_email": "me@example.com", "error": "boom"}
        with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value=failed_sync):
            with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault") as run_msgvault_mock:
                result = channel.run()

        self.assertIsInstance(result, discover_gmail.GmailDiscoveryFailed)
        self.assertEqual(result.account_email, "me@example.com")
        self.assertEqual(result.error, failed_sync)
        run_msgvault_mock.assert_not_called()

    def test_gmail_discovery_store_loops_channels_and_writes_outputs(self) -> None:
        # Exercise GmailDiscovery directly: it builds one channel per selected
        # account, runs them, and writes contacts.csv + queue.csv + manifest.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            account_queue = tmp / "discover/gmail/me-example.com/linkedin_resolution_queue.csv"
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
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }

            def fake_run_msgvault(engine, *, db, account_email, output_dir):
                return {"status": "completed", "counts": {"contacts_written": 1}}

            with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value={"status": "completed", "account_email": "me@example.com"}):
                    with mock.patch.object(discover_gmail.GmailExtractor, "run_msgvault", fake_run_msgvault):
                        store = discover_gmail.GmailDiscovery(
                            account_emails=["me@example.com"], msgvault_db=str(tmp / "msgvault.db"), sync_query="")
                        payload = store.run()

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["contacts"], 1)
            self.assertEqual([channel.account_email for channel in store.channels], ["me@example.com"])
            self.assertEqual(payload["children"][0]["account_email"], "me@example.com")
            with paths[("gmail", "linkedin_resolution_queue_csv")].open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in rows], ["jane@example.com"])
            self.assertEqual(rows[0]["total_messages"], "2")

    def test_gmail_discovery_store_skips_without_account_emails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }
            with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                store = discover_gmail.GmailDiscovery(account_emails=[])
                payload = store.run()

            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["reason"], "no_account_emails")
            datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
            self.assertGreaterEqual(payload["duration_seconds"], 0)
            self.assertEqual(payload["accounts_timing"], [])
            self.assertEqual(store.channels, [])
            self.assertFalse(paths[("gmail", "linkedin_resolution_queue_csv")].exists())

    def test_gmail_discovery_failed_outcome_includes_account_timing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paths = {
                ("gmail", "contacts_csv"): tmp / "discover/gmail/contacts.csv",
                ("gmail", "linkedin_resolution_queue_csv"): tmp / "discover/gmail/linkedin_resolution_queue.csv",
                ("gmail", "manifest_json"): tmp / "discover/gmail/manifest.json",
            }
            failed_sync = {
                "status": "failed",
                "account_email": "me@example.com",
                "error": "boom",
            }

            with mock.patch.object(discover_gmail, "output_path", side_effect=lambda source, key: paths[(source, key)]):
                with mock.patch.object(discover_gmail, "sync_msgvault_account", return_value=failed_sync):
                    payload = discover_gmail.GmailDiscovery(
                        account_emails=["me@example.com"], msgvault_db=str(tmp / "msgvault.db"), sync_query="").run()

            self.assertEqual(payload["status"], "failed")
            datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
            self.assertGreaterEqual(payload["duration_seconds"], 0)
            self.assertEqual(payload["accounts_timing"][0]["email"], "me@example.com")
            self.assertGreaterEqual(payload["accounts_timing"][0]["duration_seconds"], 0)
            self.assertNotIn("messages_added", payload["accounts_timing"][0])

    def test_messages_contacts_direct_selects_matched_and_candidates(self) -> None:
        # The review-CSV materializer is gone: import is contacts-direct.
        # Matched rows become people rows; floor-passing unmatched rows become
        # research candidates for $deep-context.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            fields = [
                "phone", "name", "source", "is_in_group_chats", "group_names",
                "message_count", "imessage_message_count", "whatsapp_message_count",
                "last_message", "imessage_last_message", "whatsapp_last_message",
                "skip", "match_status", "matched_person_id", "matched_name",
                "matched_linkedin_url", "match_confidence", "match_method", "match_reason",
            ]
            base = {field: "" for field in fields}
            rows = [
                base | {"phone": "+14155550100", "name": "Network Person", "source": "imessage", "message_count": "5", "imessage_message_count": "5", "match_status": "matched", "matched_person_id": "p1", "matched_name": "Network Person", "matched_linkedin_url": "https://www.linkedin.com/in/network-person/"},
                base | {"phone": "+14155550106", "name": "Network Person", "source": "whatsapp", "message_count": "11", "whatsapp_message_count": "11", "match_status": "matched", "matched_person_id": "p1", "matched_name": "Network Person", "matched_linkedin_url": "https://www.linkedin.com/in/network-person/"},
                base | {"phone": "+14155550101", "name": "Research Person", "source": "whatsapp", "message_count": "6", "whatsapp_message_count": "6"},
                base | {"phone": "+14155550104", "name": "AAA", "source": "imessage", "message_count": "9", "imessage_message_count": "9"},
            ]
            write_csv(contacts, fields, rows)

            summary, people_rows, candidate_rows = import_messages.selected_contacts_people(contacts)

            self.assertEqual(summary["people_rows"], 1)
            self.assertEqual(summary["candidate_rows"], 1)
            self.assertEqual(summary["skipped"].get("bad_name"), 1)
            by_public = {row["public_identifier"]: row for row in people_rows}
            self.assertEqual(set(by_public), {"network-person"})
            self.assertEqual(
                json.loads(by_public["network-person"]["all_phones"]),
                ["+14155550100", "+14155550106"],
            )
            self.assertEqual(candidate_rows[0]["candidate_key"], "phone:+14155550101")

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
            checkpoint = import_directory.commit_people_csv_to_directory(
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


if __name__ == "__main__":
    unittest.main()
