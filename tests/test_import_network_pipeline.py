import argparse
import csv
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
