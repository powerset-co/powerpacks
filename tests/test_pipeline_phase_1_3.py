import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


discover_pipeline = load_module(
    "phase13_discover_contacts_pipeline",
    "packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py",
)
discover_common = load_module(
    "phase13_discover_common",
    "packs/ingestion/primitives/discover_contacts_pipeline/common.py",
)
linkedin_discovery = load_module(
    "phase13_linkedin_discovery",
    "packs/ingestion/primitives/discover_contacts_pipeline/linkedin.py",
)
import_common = load_module(
    "phase13_import_common",
    "packs/ingestion/primitives/import_contacts_pipeline/common.py",
)
import_dispatcher = load_module(
    "phase13_import_dispatcher",
    "packs/ingestion/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
)
index_contacts = load_module(
    "phase13_index_contacts",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
)
setup_mod = load_module(
    "phase13_setup",
    "packs/ingestion/primitives/setup/setup.py",
)


class PipelinePhase13Tests(unittest.TestCase):
    def test_discover_contacts_direct_cli_emits_dry_run_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "Connections.csv"
            csv_path.write_text(
                "notes\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                "Ada,Lovelace,https://www.linkedin.com/in/ada,ada@example.com,Analytical Engines,Founder,2024-01-01\n",
                encoding="utf-8",
            )
            db_path = tmp_path / "msgvault.metadata.db"
            db_path.write_bytes(b"not-sqlite-needed-for-dry-run")
            ledger = tmp_path / "ledger.json"
            script = ROOT / "packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "run",
                    "--dry-run",
                    "--operator-id",
                    "test",
                    "--ledger",
                    str(ledger),
                    "--linkedin-csv",
                    str(csv_path),
                    "--linkedin-source-user",
                    "test-user",
                    "--msgvault-db",
                    str(db_path),
                    "--gmail-account-email",
                    "me@example.com",
                    "--skip-msgvault-sync",
                    "--gmail-linkedin-provider",
                    "off",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertIn("gmail_msgvault", payload["would_run_steps"])
        self.assertIn("linkedin", payload["would_run_steps"])
        gmail_job = next(job for job in payload["worker_groups"]["import"]["jobs"] if job["source"] == "gmail")
        self.assertTrue(gmail_job["skip_msgvault_sync"])

    def test_source_workers_receive_explicit_repo_local_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "discover-ledger.json"
            ledger = {
                "input": {
                    "operator_id": "arthur",
                    "from_accounts": str(Path(tmp) / "accounts.json"),
                    "gmail_account_email": "arthur@powerset.co",
                    "msgvault_db": ".powerpacks/msgvault/arthur-powerset.co/msgvault.metadata.db",
                    "skip_msgvault_sync": True,
                    "linkedin_csv": ".powerpacks/network-import/discover/linkedin/Connections.csv",
                    "linkedin_source_user": "arthur",
                },
                "steps": {},
                "artifacts": {},
            }

            def fake_gmail(**kwargs):
                self.assertEqual(kwargs["msgvault_db"], ".powerpacks/msgvault/arthur-powerset.co/msgvault.metadata.db")
                self.assertEqual(kwargs["selected_accounts"], ["arthur@powerset.co"])
                self.assertTrue(kwargs["skip_msgvault_sync"])
                return {"status": "completed", "contacts_csv": "gmail.csv", "linkedin_resolution_queue_csv": "queue.csv"}

            def fake_linkedin(**kwargs):
                self.assertEqual(kwargs["connections_csv"], ".powerpacks/network-import/discover/linkedin/Connections.csv")
                self.assertEqual(kwargs["source_user_label"], "arthur")
                return {"status": "completed", "artifacts": {}, "contacts_csv": "linkedin.csv"}

            with mock.patch.object(discover_pipeline.gmail, "discover", side_effect=fake_gmail), \
                mock.patch.object(discover_pipeline.linkedin, "discover", side_effect=fake_linkedin):
                ok = discover_pipeline.run_source_import_workers(ledger_path, ledger)
        self.assertTrue(ok)

    def test_csv_count_empty_path_is_zero_not_current_directory(self):
        self.assertEqual(import_common.csv_count(""), 0)

    def test_linkedin_csv_path_falls_back_to_repo_local_discovered_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "network-import"
            repo_local = base / "discover" / "linkedin" / "Connections.csv"
            repo_local.parent.mkdir(parents=True)
            repo_local.write_text("First Name,Last Name\nAda,Lovelace\n", encoding="utf-8")
            accounts = {
                "accounts": {
                    "linkedin_csv": {
                        "config": {"csv_path": "/Users/arthur/Downloads/missing/Connections.csv"},
                    }
                }
            }
            with mock.patch.object(import_common, "DEFAULT_BASE_DIR", base):
                self.assertEqual(import_common.linkedin_csv_path(accounts), str(repo_local))

    def test_setup_status_does_not_write_setup_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "setup-run.json"
            ledger.write_text(json.dumps({"status": "old"}), encoding="utf-8")
            before = ledger.read_text(encoding="utf-8")
            args = SimpleNamespace(setup_ledger=str(ledger))
            buf = StringIO()
            with mock.patch.object(setup_mod, "status_payload", return_value={"setup_ledger": {"status": "new"}}), redirect_stdout(buf):
                self.assertEqual(setup_mod.run_status(args), 0)
            self.assertEqual(ledger.read_text(encoding="utf-8"), before)
            self.assertEqual(json.loads(buf.getvalue()), {"setup_ledger": {"status": "new"}})

    def test_write_csv_rows_skips_unchanged_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "rows.csv"
            rows = [{"a": "1", "b": "2"}]
            discover_common.write_csv_rows(csv_path, ["a", "b"], rows)
            first_bytes = csv_path.read_bytes()
            first_mtime = csv_path.stat().st_mtime_ns
            time.sleep(0.01)
            discover_common.write_csv_rows(csv_path, ["a", "b"], rows)
            self.assertEqual(csv_path.read_bytes(), first_bytes)
            self.assertEqual(csv_path.stat().st_mtime_ns, first_mtime)
            self.assertIn(b"\n", first_bytes)
            self.assertNotIn(b"\r\n", first_bytes)

    def test_import_manifest_adopts_fingerprints_once_and_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(import_common, "DEFAULT_IMPORT_DIR", Path(tmp) / "import"):
            artifact = Path(tmp) / "people.csv"
            artifact.write_text("id\n1\n", encoding="utf-8")
            payload = {
                "status": "completed",
                "input": {"people_csv": str(artifact)},
                "outputs": {"people_csv": str(artifact)},
                "stats": {"people": 1},
            }
            first = import_common.write_manifest("linkedin", dict(payload))
            manifest = Path(tmp) / "import" / "linkedin" / "manifest.json"
            first_mtime = manifest.stat().st_mtime_ns
            time.sleep(0.01)
            second = import_common.write_manifest("linkedin", dict(payload))
            self.assertIn("fingerprints", first)
            self.assertEqual(second["updated_at"], first["updated_at"])
            self.assertEqual(manifest.stat().st_mtime_ns, first_mtime)

    def test_import_manifest_current_returns_noop_for_matching_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(import_common, "DEFAULT_IMPORT_DIR", Path(tmp) / "import"):
            artifact = Path(tmp) / "people.csv"
            directory = Path(tmp) / "directory.csv"
            artifact.write_text("id\n1\n", encoding="utf-8")
            directory.write_text("id\n1\n", encoding="utf-8")
            with mock.patch.object(import_common, "DEFAULT_DIRECTORY_CSV", directory):
                import_common.write_manifest("gmail", {
                    "status": "completed",
                    "input": {"people_csv": str(artifact)},
                    "outputs": {"people_csv": str(artifact), "directory_csv": str(directory)},
                    "stats": {"people": 1},
                })
                current = import_common.import_manifest_current("gmail")
                self.assertIsNotNone(current)
                self.assertTrue(current["noop"])
                self.assertEqual(current["reason"], "import_manifest_current")
                self.assertIsNotNone(import_common.import_manifest_current("gmail", {"people_csv": str(artifact)}))
                self.assertIsNone(import_common.import_manifest_current("gmail", {"people_csv": str(artifact.with_name("other.csv"))}))
                directory.write_text("id\n2\n", encoding="utf-8")
                self.assertIsNotNone(import_common.import_manifest_current("gmail"))
                artifact.write_text("id\n2\n", encoding="utf-8")
                self.assertIsNone(import_common.import_manifest_current("gmail"))

    def test_import_all_manifest_skips_unchanged_parent_write(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(import_dispatcher, "DEFAULT_IMPORT_DIR", Path(tmp) / "import"):
            payload = {"status": "completed", "sources": {"gmail": {"status": "completed"}}, "updated_at": "first"}
            first = import_dispatcher.write_aggregate_manifest(payload)
            manifest = Path(tmp) / "import" / "manifest.json"
            first_mtime = manifest.stat().st_mtime_ns
            time.sleep(0.01)
            second = import_dispatcher.write_aggregate_manifest({**payload, "updated_at": "second"})
            self.assertEqual(first, second)
            self.assertEqual(manifest.stat().st_mtime_ns, first_mtime)

    def test_fan_in_excludes_canonical_merged_only_when_all_sources_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            merged = root / ".powerpacks/network-import/merged/people.csv"
            gmail = root / ".powerpacks/network-import/import/gmail/people.csv"
            linkedin = root / ".powerpacks/network-import/import/linkedin/people.csv"
            messages = root / ".powerpacks/network-import/import/messages/people.csv"
            merged.parent.mkdir(parents=True)
            gmail.parent.mkdir(parents=True)
            linkedin.parent.mkdir(parents=True)
            messages.parent.mkdir(parents=True)
            merged.write_text("id\nmerged\n", encoding="utf-8")
            gmail.write_text("id\ngmail\n", encoding="utf-8")
            args = SimpleNamespace(include_existing_artifacts=True, input=[])
            with mock.patch.object(index_contacts, "ROOT", root):
                inputs = [str(path) for path in index_contacts.fan_in_input_paths(args)]
                self.assertIn(".powerpacks/network-import/import/gmail/people.csv", inputs)
                self.assertIn(".powerpacks/network-import/merged/people.csv", inputs)
                linkedin.write_text("id\nlinkedin\n", encoding="utf-8")
                messages.write_text("id\nmessages\n", encoding="utf-8")
                inputs = [str(path) for path in index_contacts.fan_in_input_paths(args)]
                self.assertNotIn(".powerpacks/network-import/merged/people.csv", inputs)
                gmail.unlink()
                linkedin.unlink()
                messages.unlink()
                inputs = [str(path) for path in index_contacts.fan_in_input_paths(args)]
                self.assertEqual(inputs, [".powerpacks/network-import/merged/people.csv"])

    def test_fan_in_currentness_accepts_legacy_canonical_merged_fingerprint(self):
        current = {
            ".powerpacks/network-import/import/gmail/people.csv": {"sha256": "a"},
        }
        legacy = {
            ".powerpacks/network-import/merged/people.csv": {"sha256": "bootstrap"},
            ".powerpacks/network-import/import/gmail/people.csv": {"sha256": "a"},
        }
        self.assertTrue(index_contacts.fan_in_fingerprints_match(legacy, current))
        changed = {**legacy, ".powerpacks/network-import/import/gmail/people.csv": {"sha256": "b"}}
        self.assertFalse(index_contacts.fan_in_fingerprints_match(changed, current))

    def test_discovery_stage_manifest_adopts_fingerprints_once_and_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "contacts.csv"
            artifact.write_text("id\n1\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.json"
            payload = {
                "status": "completed",
                "source": "linkedin_csv",
                "contacts_csv": str(artifact),
                "contacts": 1,
                "stats": {"parsed": 1},
            }
            first = discover_common.write_stage_manifest(manifest, dict(payload))
            first_mtime = manifest.stat().st_mtime_ns
            time.sleep(0.01)
            second = discover_common.write_stage_manifest(manifest, dict(payload))
            self.assertIn("fingerprints", first)
            self.assertEqual(second["updated_at"], first["updated_at"])
            self.assertEqual(manifest.stat().st_mtime_ns, first_mtime)

    def test_linkedin_discovery_accepts_repo_local_stable_connections_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_csv = tmp_path / "Connections.csv"
            source_csv.write_text(
                "notes\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                "Ada,Lovelace,https://www.linkedin.com/in/ada,ada@example.com,Analytical Engines,Founder,2024-01-01\n",
                encoding="utf-8",
            )
            contacts_csv = tmp_path / "contacts.csv"
            manifest_json = tmp_path / "manifest.json"

            def fake_output_path(_source, name):
                return {
                    "source_csv": source_csv,
                    "contacts_csv": contacts_csv,
                    "manifest_json": manifest_json,
                }[name]

            with mock.patch.object(linkedin_discovery, "output_path", side_effect=fake_output_path):
                payload = linkedin_discovery.discover(
                    accounts_file=tmp_path / "accounts.json",
                    connections_csv=source_csv,
                    source_user_label="arthur",
                )
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["source_csv"], str(source_csv))
            self.assertTrue(contacts_csv.exists())
            self.assertTrue(manifest_json.exists())

    def test_index_run_noops_when_processing_dry_run_reports_complete_restored_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            people = root / ".powerpacks/network-import/merged/people.csv"
            people.parent.mkdir(parents=True)
            people.write_text("id\n1\n", encoding="utf-8")
            duckdb = root / ".powerpacks/search-index/local-search.duckdb"
            duckdb.parent.mkdir(parents=True)
            duckdb.write_bytes(b"x" * 2048)
            manifest = root / ".powerpacks/network-import/index/contacts/manifest.json"
            manifest.parent.mkdir(parents=True)
            args = SimpleNamespace(
                people_csv=".powerpacks/network-import/merged/people.csv",
                output_dir=".powerpacks/search-index",
                manifest=".powerpacks/network-import/index/contacts/manifest.json",
                operator_id="arthur",
            )
            estimate = {
                "status": "dry_run",
                "counts": {"total_people": 1, "processed_people": 1, "pending_people": 0},
                "estimated_paid_calls": {"role_embeddings": 0},
                "estimated_cost_usd": 0,
            }
            with mock.patch.object(index_contacts, "ROOT", root), \
                mock.patch.object(index_contacts, "run_fan_in", return_value=({"status": "completed", "step": "fan_in"}, 0)), \
                mock.patch.object(index_contacts, "maybe_materialize_existing_records", return_value={"status": "skipped", "reason": "duckdb_exists"}), \
                mock.patch.object(index_contacts, "run_json_command", return_value=(0, estimate, "")) as run_json:
                payload, code = index_contacts.run_pipeline(args)
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "noop")
            self.assertEqual(payload["reason"], "processing_outputs_complete")
            run_json.assert_called_once()

    def test_index_run_refreshes_duckdb_when_processing_hashes_are_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            people = root / ".powerpacks/network-import/merged/people.csv"
            people.parent.mkdir(parents=True)
            people.write_text("id\n1\n", encoding="utf-8")
            duckdb = root / ".powerpacks/search-index/local-search.duckdb"
            duckdb.parent.mkdir(parents=True)
            duckdb.write_bytes(b"x" * 2048)
            time.sleep(0.01)
            hashes = root / ".powerpacks/search-index/unified/person_hashes.json"
            hashes.parent.mkdir(parents=True)
            hashes.write_text('{"id:1":"hash"}\n', encoding="utf-8")
            manifest = root / ".powerpacks/network-import/index/contacts/manifest.json"
            manifest.parent.mkdir(parents=True)
            args = SimpleNamespace(
                people_csv=".powerpacks/network-import/merged/people.csv",
                output_dir=".powerpacks/search-index",
                manifest=".powerpacks/network-import/index/contacts/manifest.json",
                operator_id="arthur",
            )
            estimate = {
                "status": "dry_run",
                "counts": {"total_people": 1, "processed_people": 1, "pending_people": 0},
                "estimated_paid_calls": {"role_embeddings": 0},
                "estimated_cost_usd": 0,
            }
            duckdb_payload = {"duckdb": ".powerpacks/search-index/local-search.duckdb"}
            with mock.patch.object(index_contacts, "ROOT", root), \
                mock.patch.object(index_contacts, "run_fan_in", return_value=({"status": "completed", "step": "fan_in"}, 0)), \
                mock.patch.object(index_contacts, "maybe_materialize_existing_records", return_value={"status": "skipped", "reason": "duckdb_exists"}), \
                mock.patch.object(index_contacts, "run_json_command", side_effect=[(0, estimate, ""), (0, duckdb_payload, "")]) as run_json:
                payload, code = index_contacts.run_pipeline(args)
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "local_duckdb_refresh")
            self.assertEqual(payload["reason"], "processing_outputs_complete_duckdb_refreshed")
            self.assertEqual(run_json.call_count, 2)
            duckdb_cmd = run_json.call_args_list[1].args[0]
            self.assertIn("--incremental", duckdb_cmd)
            self.assertNotIn("--force", duckdb_cmd)

    def test_duckdb_freshness_checks_record_inputs_not_only_person_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            people = root / ".powerpacks/network-import/merged/people.csv"
            people.parent.mkdir(parents=True)
            people.write_text("id\n1\n", encoding="utf-8")
            duckdb = root / ".powerpacks/search-index/local-search.duckdb"
            duckdb.parent.mkdir(parents=True)
            duckdb.write_bytes(b"x" * 2048)
            time.sleep(0.01)
            records = root / ".powerpacks/search-index/records/people.records.jsonl"
            records.parent.mkdir(parents=True)
            records.write_text('{"id":"newer-record"}\n', encoding="utf-8")
            args = SimpleNamespace(
                people_csv=".powerpacks/network-import/merged/people.csv",
                output_dir=".powerpacks/search-index",
            )
            with mock.patch.object(index_contacts, "ROOT", root):
                self.assertFalse(index_contacts.duckdb_current_for_processing_hashes(args))
                freshness = index_contacts.duckdb_freshness_payload(args)
            self.assertEqual(freshness["reason"], "stale_duckdb_inputs")
            self.assertIn(".powerpacks/search-index/records/people.records.jsonl", freshness["stale_inputs"])


if __name__ == "__main__":
    unittest.main()
