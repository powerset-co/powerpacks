import importlib.util
import json
import os
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

from packs.indexing.lib.artifact_io import write_parquet_rows

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
    "packs/ingestion/primitives/discover_contacts_pipeline/linkedin/discover.py",
)
import_common = load_module(
    "phase13_import_common",
    "packs/ingestion/primitives/import_contacts_pipeline/common.py",
)
index_contacts = load_module(
    "phase13_index_contacts",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
)
setup_mod = load_module(
    "phase13_setup",
    "packs/ingestion/primitives/setup/setup.py",
)
setup_linkedin_csv = load_module(
    "phase13_setup_linkedin_csv",
    "packs/ingestion/primitives/setup_linkedin_csv/setup_linkedin_csv.py",
)
setup_gmail = load_module(
    "phase13_setup_gmail",
    "packs/ingestion/primitives/setup_gmail/setup_gmail.py",
)
openai_usage_tiers = load_module(
    "phase13_openai_usage_tiers",
    "packs/indexing/lib/openai_usage_tiers.py",
)
build_processing = load_module(
    "phase13_build_processing",
    "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py",
)
enrich_people = load_module(
    "phase13_enrich_people",
    "packs/ingestion/primitives/enrich_people/enrich_people.py",
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
                    "gmail_account_email": "operator@example.com",
                    "msgvault_db": ".powerpacks/msgvault/operator-example-com/msgvault.metadata.db",
                    "skip_msgvault_sync": True,
                    "linkedin_csv": ".powerpacks/network-import/discover/linkedin/Connections.csv",
                    "linkedin_source_user": "arthur",
                },
                "steps": {},
                "artifacts": {},
            }

            def fake_gmail(**kwargs):
                self.assertEqual(kwargs["msgvault_db"], ".powerpacks/msgvault/operator-example-com/msgvault.metadata.db")
                self.assertEqual(kwargs["selected_accounts"], ["operator@example.com"])
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

    def test_openai_usage_tier_defaults_to_tier_5_profile(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            profile = openai_usage_tiers.openai_usage_tier_profile()
        self.assertEqual(profile["tier"], "tier_5")
        self.assertEqual(profile["powerpacks_tpm_budget"], 10_000_000)
        self.assertEqual(profile["openai_concurrency"], 256)
        self.assertEqual(profile["paid_checkpoint_every"], 512)
        self.assertEqual(profile["embedding_concurrency"], 8)
        self.assertEqual(openai_usage_tiers.openai_usage_tier_profile("tier-2")["paid_checkpoint_every"], 32)
        self.assertEqual(openai_usage_tiers.openai_usage_tier_profile("1")["openai_concurrency"], 16)

    def test_paid_checkpoint_every_uses_profile_and_env_override(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            ledger = {"checkpoint_every": 1000, "openai_usage_tier": openai_usage_tiers.openai_usage_tier_profile("tier_5")}
            self.assertEqual(build_processing.paid_checkpoint_every(ledger), 512)
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_USAGE_TIER": "tier_1"}, clear=True):
            self.assertEqual(openai_usage_tiers.profile_paid_checkpoint_every(1000), 16)
            self.assertEqual(build_processing.paid_checkpoint_every({"checkpoint_every": 1000}), 16)
            self.assertEqual(build_processing.openai_concurrency({}), 16)
        with mock.patch.dict(os.environ, {"POWERPACKS_PAID_CHECKPOINT_EVERY": "77"}, clear=True):
            self.assertEqual(openai_usage_tiers.profile_paid_checkpoint_every(1000, tier="tier_5"), 77)

    def test_index_processing_args_forwards_default_tier_5(self):
        args = SimpleNamespace(
            people_csv=".powerpacks/network-import/merged/people.csv",
            output_dir=".powerpacks/search-index",
            operator_id="arthur",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            cmd = index_contacts.processing_args(args, dry_run=True, allow_paid=False)
        self.assertIn("--openai-usage-tier", cmd)
        self.assertEqual(cmd[cmd.index("--openai-usage-tier") + 1], "tier_5")

    def test_openai_usage_tier_env_overrides_pipeline_default(self):
        args = SimpleNamespace(
            people_csv=".powerpacks/network-import/merged/people.csv",
            output_dir=".powerpacks/search-index",
            operator_id="arthur",
        )
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_USAGE_TIER": "tier_1"}, clear=True):
            cmd = index_contacts.processing_args(args, dry_run=True, allow_paid=False)
            setup_args = setup_linkedin_csv.args_for_index("arthur")
        self.assertEqual(cmd[cmd.index("--openai-usage-tier") + 1], "tier_1")
        self.assertEqual(setup_args.openai_usage_tier, "tier_1")

    def test_explicit_openai_usage_tier_wins_over_env(self):
        args = SimpleNamespace(
            people_csv=".powerpacks/network-import/merged/people.csv",
            output_dir=".powerpacks/search-index",
            operator_id="arthur",
            openai_usage_tier="tier-2",
        )
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_USAGE_TIER": "tier_1"}, clear=True):
            cmd = index_contacts.processing_args(args, dry_run=True, allow_paid=False)
        self.assertEqual(cmd[cmd.index("--openai-usage-tier") + 1], "tier_2")

    def test_openai_usage_tier_cli_accepts_aliases(self):
        build_args = build_processing.build_parser().parse_args([
            "run",
            "--input", "people.csv",
            "--output-dir", "out",
            "--openai-usage-tier", "tier-2",
        ])
        index_args = index_contacts.build_parser().parse_args(["plan", "--openai-usage-tier", "1"])
        self.assertEqual(build_processing.openai_usage_tier_profile(build_args.openai_usage_tier)["tier"], "tier_2")
        self.assertEqual(index_contacts.selected_openai_usage_tier(index_args)["tier"], "tier_1")

    def test_rapidapi_defaults_use_authorized_throughput(self):
        self.assertEqual(enrich_people.DEFAULT_RAPIDAPI_MAX_WORKERS, 64)
        self.assertEqual(enrich_people.DEFAULT_RAPIDAPI_MAX_RPM, 300)

    def test_rapidapi_profile_retries_429_then_succeeds(self):
        payload = {
            "public_identifier": "ada",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "experiences": [{"title": "Founder", "company_name": "Analytical Engines"}],
        }
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(enrich_people, "DEFAULT_RAPIDAPI_RETRY_ATTEMPTS", 3), \
            mock.patch.object(enrich_people, "DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS", 1.0), \
            mock.patch.object(enrich_people, "http_json", side_effect=[(429, {"message": "Too many requests"}, ""), (200, payload, "")]) as http_json, \
            mock.patch.object(enrich_people.time, "sleep") as sleep:
            wait = mock.Mock()
            result = enrich_people.rapidapi_profile("ada", "https://www.linkedin.com/in/ada", "key", cache_dir=tmp, refresh_cache=True, wait_for_attempt=wait)
            cached = json.loads((Path(tmp) / "ada.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(http_json.call_count, 2)
        self.assertEqual(wait.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(cached["attempts"], 2)

    def test_rapidapi_profile_records_final_retry_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(enrich_people, "DEFAULT_RAPIDAPI_RETRY_ATTEMPTS", 2), \
            mock.patch.object(enrich_people, "DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS", 0.5), \
            mock.patch.object(enrich_people, "http_json", side_effect=[(429, {"message": "Too many requests"}, ""), (429, {"message": "Too many requests"}, "")]) as http_json, \
            mock.patch.object(enrich_people.time, "sleep") as sleep:
            result = enrich_people.rapidapi_profile("ada", "https://www.linkedin.com/in/ada", "key", cache_dir=tmp, refresh_cache=True)
            cached = json.loads((Path(tmp) / "ada.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(http_json.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(cached["status_code"], 429)
        self.assertEqual(cached["attempts"], 2)

    def test_enrich_linkedin_reports_retry_summary_and_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / "enrichment"
            hits = artifact_dir / "rapidapi_cache_hits.csv"
            misses = artifact_dir / "rapidapi_cache_misses.csv"
            enrich_people.write_csv(hits, enrich_people.CACHE_COLUMNS, [])
            enrich_people.write_csv(misses, enrich_people.CACHE_COLUMNS, [{
                "id": "p1",
                "public_identifier": "ada",
                "linkedin_url": "https://www.linkedin.com/in/ada",
                "cache_status": "miss",
            }])
            ledger = {
                "artifact_dir": str(artifact_dir),
                "input": {"profile_cache_dir": str(root / "cache"), "refresh_cache": True, "max_workers": 1, "max_rpm": 0},
                "artifacts": {"rapidapi_cache_hits_csv": str(hits), "rapidapi_cache_misses_csv": str(misses)},
                "paid_call_count": 1,
            }
            rapid = {
                "status_code": 200,
                "data": {"public_identifier": "ada", "full_name": "Ada Lovelace"},
                "error": "",
                "from_cache": False,
                "normalized_profile": {"success": True},
                "attempts": 2,
            }
            with mock.patch.object(enrich_people, "rapidapi_key", return_value="key"), \
                mock.patch.object(enrich_people, "rapidapi_profile", return_value=rapid):
                summary = enrich_people.step_enrich_linkedin(ledger)
            rows = enrich_people.read_csv(Path(summary["output_file"]))
        self.assertEqual(summary["retried"], 1)
        self.assertEqual(summary["retry_successes"], 1)
        self.assertEqual(summary["retry_failures"], 0)
        self.assertEqual(rows[0]["rapidapi_attempts"], "2")
        self.assertEqual(rows[0]["rapidapi_retry_outcome"], "success")

    def test_recent_rapidapi_failure_is_not_classified_as_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cache_path = cache_dir / "ada.json"
            cache_path.write_text(json.dumps({
                "status_code": 429,
                "last_checked_at": enrich_people.now_iso(),
                "normalized_profile": {"success": False, "error": "rate limited"},
            }), encoding="utf-8")
            status, reason, path, failure = enrich_people.classify_rapidapi_cache_status(
                {"public_identifier": "ada", "linkedin_url": "https://www.linkedin.com/in/ada"},
                cache_dir,
                refresh_cache=False,
                retry_hours=24,
                cache_index={"ada"},
            )
        self.assertEqual(status, "recent_failure")
        self.assertEqual(reason, "recent provider failure")
        self.assertEqual(path, cache_path)
        self.assertIsNotNone(failure)

    def test_linkedin_csv_path_falls_back_to_repo_local_discovered_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "network-import"
            repo_local = base / "discover" / "linkedin" / "Connections.csv"
            repo_local.parent.mkdir(parents=True)
            repo_local.write_text("First Name,Last Name\nAda,Lovelace\n", encoding="utf-8")
            accounts = {
                "accounts": {
                    "linkedin_csv": {
                        "config": {"csv_path": "/path/to/Downloads/missing/Connections.csv"},
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

    def test_import_manifest_current_ignores_absolute_shared_directory_csv(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(import_common, "DEFAULT_IMPORT_DIR", Path(tmp) / "import"):
            artifact = Path(tmp) / "people.csv"
            directory = Path(tmp) / "directory.csv"
            artifact.write_text("id\n1\n", encoding="utf-8")
            directory.write_text("id\n1\n", encoding="utf-8")
            with mock.patch.object(import_common, "DEFAULT_DIRECTORY_CSV", directory):
                import_common.write_manifest("linkedin", {
                    "status": "completed",
                    "input": {"people_csv": str(artifact)},
                    "outputs": {"people_csv": str(artifact), "directory_csv": str(directory.resolve())},
                })
                directory.write_text("id\nchanged\n", encoding="utf-8")
                self.assertIsNotNone(import_common.import_manifest_current("linkedin"))

    def test_setup_linkedin_csv_dry_run_uses_stable_discovered_csv_for_currentness(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upload_csv = tmp_path / "uploads" / "Connections.csv"
            stable_csv = tmp_path / "network-import" / "discover" / "linkedin" / "Connections.csv"
            import_dir = tmp_path / "network-import" / "import"
            people_csv = tmp_path / "people.csv"
            accounts = tmp_path / "accounts.json"
            upload_csv.parent.mkdir(parents=True)
            stable_csv.parent.mkdir(parents=True)
            upload_csv.write_text("First Name,Last Name,URL\nAda,Lovelace,https://www.linkedin.com/in/ada\n", encoding="utf-8")
            stable_csv.write_text(upload_csv.read_text(encoding="utf-8"), encoding="utf-8")
            people_csv.write_text("id\n1\n", encoding="utf-8")
            accounts.write_text("{}", encoding="utf-8")
            import_common.write_manifest("linkedin", {
                "status": "completed",
                "input": {"connections_csv": str(stable_csv), "source_user": "arthur"},
                "outputs": {"people_csv": str(people_csv)},
                "stats": {"people": 1},
            }, import_dir=import_dir)

            args = SimpleNamespace(csv=str(upload_csv), source_user="arthur", accounts=str(accounts))
            with mock.patch.object(setup_linkedin_csv, "DEFAULT_IMPORT_DIR", import_dir), \
                mock.patch.object(setup_linkedin_csv, "DISCOVER_CONNECTIONS_CSV", stable_csv), \
                mock.patch.object(setup_linkedin_csv, "read_csv_stats", return_value={"status": "ok", "valid_contacts": 1}):
                payload = setup_linkedin_csv.dry_run(args)

            self.assertTrue(payload["current_import"])
            self.assertEqual(payload["manifest_csv"], str(stable_csv))

    def test_setup_linkedin_csv_run_noops_uploaded_csv_after_discovery_stable_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upload_csv = tmp_path / "uploads" / "Connections.csv"
            stable_csv = tmp_path / "network-import" / "discover" / "linkedin" / "Connections.csv"
            import_dir = tmp_path / "network-import" / "import"
            people_csv = tmp_path / "people.csv"
            duckdb = tmp_path / "local-search.duckdb"
            accounts = tmp_path / "accounts.json"
            run_root = tmp_path / "runs" / "setup-linkedin-csv"
            upload_csv.parent.mkdir(parents=True)
            stable_csv.parent.mkdir(parents=True)
            upload_csv.write_text("First Name,Last Name,URL\nAda,Lovelace,https://www.linkedin.com/in/ada\n", encoding="utf-8")
            stable_csv.write_text(upload_csv.read_text(encoding="utf-8"), encoding="utf-8")
            people_csv.write_text("id\n1\n", encoding="utf-8")
            duckdb.write_bytes(b"0" * 2048)
            accounts.write_text("{}", encoding="utf-8")
            import_common.write_manifest("linkedin", {
                "status": "completed",
                "input": {"connections_csv": str(stable_csv), "source_user": "arthur"},
                "outputs": {"people_csv": str(people_csv)},
                "stats": {"people": 1},
            }, import_dir=import_dir)
            args = SimpleNamespace(
                run_id="stable-noop",
                csv=str(upload_csv),
                source_user="arthur",
                accounts=str(accounts),
                force=False,
                operator_id="arthur",
            )

            with mock.patch.object(setup_linkedin_csv, "RUN_ROOT", run_root), \
                mock.patch.object(setup_linkedin_csv, "DEFAULT_IMPORT_DIR", import_dir), \
                mock.patch.object(setup_linkedin_csv, "DISCOVER_CONNECTIONS_CSV", stable_csv), \
                mock.patch.object(setup_linkedin_csv, "read_csv_stats", return_value={"status": "ok", "valid_contacts": 1}), \
                mock.patch.object(setup_linkedin_csv.linkedin_discovery, "discover", return_value={"status": "completed", "contacts": 1, "source_csv": str(stable_csv)}), \
                mock.patch.object(setup_linkedin_csv, "run_linkedin_import") as import_mock, \
                mock.patch.object(setup_linkedin_csv.index_contacts_pipeline, "run_pipeline", return_value=({"status": "ready", "step": "noop", "duckdb": str(duckdb)}, 0)):
                payload = setup_linkedin_csv.run(args)

            self.assertEqual(payload["status"], "completed")
            self.assertTrue(payload["import"]["noop"])
            import_mock.assert_not_called()
            self.assertTrue((run_root / "status.json").exists())

    def test_setup_linkedin_csv_rejects_invalid_run_id(self):
        with self.assertRaises(ValueError):
            setup_linkedin_csv.make_context("../bad")

    def test_setup_linkedin_csv_does_not_complete_when_index_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "Connections.csv"
            accounts = tmp_path / "accounts.json"
            run_root = tmp_path / "runs" / "setup-linkedin-csv"
            import_dir = tmp_path / "network-import" / "import"
            people_csv = tmp_path / "people.csv"
            csv_path.write_text("First Name,Last Name,URL\nAda,Lovelace,https://www.linkedin.com/in/ada\n", encoding="utf-8")
            accounts.write_text("{}", encoding="utf-8")
            people_csv.write_text("id\n1\n", encoding="utf-8")
            import_payload = {
                "status": "completed",
                "noop": True,
                "input": {"connections_csv": str(csv_path), "source_user": "arthur"},
                "outputs": {"people_csv": str(people_csv)},
                "stats": {"people": 1},
            }
            args = SimpleNamespace(
                run_id="not-ready-index",
                csv=str(csv_path),
                source_user="arthur",
                accounts=str(accounts),
                force=False,
                operator_id="arthur",
            )
            with mock.patch.object(setup_linkedin_csv, "RUN_ROOT", run_root), \
                mock.patch.object(setup_linkedin_csv, "DEFAULT_IMPORT_DIR", import_dir), \
                mock.patch.object(setup_linkedin_csv, "read_csv_stats", return_value={"status": "ok", "valid_contacts": 1}), \
                mock.patch.object(setup_linkedin_csv.linkedin_discovery, "discover", return_value={"status": "completed", "contacts": 1, "source_csv": str(csv_path)}), \
                mock.patch.object(setup_linkedin_csv, "import_manifest_current", return_value=import_payload), \
                mock.patch.object(setup_linkedin_csv.index_contacts_pipeline, "run_pipeline", return_value=({"status": "not_ready", "reason": "missing_people_csv"}, 0)):
                payload = setup_linkedin_csv.run(args)

            self.assertEqual(payload["status"], "failed")
            self.assertIn("missing_people_csv", payload["error"])

    def test_setup_linkedin_csv_status_payload_reads_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "runs" / "setup-linkedin-csv"
            run_root.mkdir(parents=True)
            with mock.patch.object(setup_linkedin_csv, "RUN_ROOT", run_root):
                self.assertEqual(setup_linkedin_csv.status_payload()["status"], "missing")
                (run_root / "status.json").write_text(json.dumps({"status": "completed", "run_id": "run-1"}), encoding="utf-8")
                # run_id arg is ignored; the single overwritten file is always read.
                self.assertEqual(setup_linkedin_csv.status_payload()["status"], "completed")
                self.assertEqual(setup_linkedin_csv.status_payload("ignored")["status"], "completed")

    def test_setup_linkedin_csv_second_run_overwrites_single_status_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "runs" / "setup-linkedin-csv"
            with mock.patch.object(setup_linkedin_csv, "RUN_ROOT", run_root):
                first = setup_linkedin_csv.make_context("run-first")
                first.event("inspect", "first run inspect", status="completed")
                status_path = run_root / "status.json"
                events_path = run_root / "events.jsonl"
                self.assertEqual(json.loads(status_path.read_text())["run_id"], "run-first")
                first_events = [line for line in events_path.read_text().splitlines() if line.strip()]
                self.assertTrue(first_events)

                # Clicking start again reuses the same files, overwriting the prior run.
                second = setup_linkedin_csv.make_context("run-second")
                self.assertEqual(second.state_path, status_path)
                self.assertEqual(second.events_path, events_path)
                self.assertEqual(json.loads(status_path.read_text())["run_id"], "run-second")
                # events.jsonl is truncated when the new run starts.
                self.assertEqual([line for line in events_path.read_text().splitlines() if line.strip()], [])
                self.assertFalse((run_root / "run-first").exists())
                self.assertFalse((run_root / "latest.json").exists())

    def test_setup_gmail_rejects_invalid_run_id(self):
        with self.assertRaises(ValueError):
            setup_gmail.make_context("../bad")

    def test_resumed_gmail_setup_drops_retired_contact_lookup_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "gmail"
            run_root.mkdir(parents=True)
            status_path = run_root / "status.json"
            status_path.write_text(json.dumps({
                "schema_version": 1,
                "vertical": "gmail",
                "run_id": "legacy-run",
                "status": "failed",
                "current_stage": "network_duckdb",
                "stage_order": [
                    {"id": "merge_network", "label": "Merge contact sources"},
                    {"id": "network_duckdb", "label": "Prepare contact lookup database"},
                    {"id": "search_duckdb", "label": "Update local search database"},
                ],
                "stages": {
                    "merge_network": {"status": "completed"},
                    "network_duckdb": {"status": "failed"},
                },
            }), encoding="utf-8")

            with mock.patch.object(setup_gmail, "RUN_ROOT", run_root):
                ctx = setup_gmail.make_context(resume=True)

            self.assertEqual(ctx.status["stage_order"], setup_gmail.STAGES)
            self.assertNotIn("network_duckdb", ctx.status["stages"])
            self.assertNotIn("current_stage", ctx.status)
            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["stage_order"], setup_gmail.STAGES)
            self.assertNotIn("network_duckdb", persisted["stages"])

    def test_setup_gmail_dry_run_lists_linked_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            accounts = tmp_path / "accounts.json"
            accounts.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(accounts=str(accounts), operator_id="arthur", run_id="")
            with mock.patch.object(setup_gmail, "linked_gmail_accounts", return_value=["ada@example.com"]), \
                mock.patch.object(setup_gmail, "import_manifest_current", return_value=None):
                payload = setup_gmail.dry_run(args)
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["linked_accounts"], ["ada@example.com"])
            self.assertFalse(payload["current_import"])

    def test_setup_gmail_dry_run_estimates_parallel_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            accounts = tmp_path / "accounts.json"
            accounts.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(accounts=str(accounts), operator_id="arthur", run_id="")
            with mock.patch.object(setup_gmail, "linked_gmail_accounts", return_value=["ada@example.com"]), \
                mock.patch.object(setup_gmail, "import_manifest_current", return_value=None), \
                mock.patch.object(setup_gmail, "_queue_emails", return_value=[f"ada-{idx}@example.com" for idx in range(12)]), \
                mock.patch.object(setup_gmail, "_directory_emails", return_value=set()):
                payload = setup_gmail.dry_run(args)
            estimate = payload["parallel_spend_estimate"]
            self.assertEqual(estimate["pending_contacts"], 12)
            self.assertEqual(estimate["cost_per_contact_usd"], 0.05)
            self.assertEqual(estimate["estimated_usd"], 0.6)
            self.assertEqual(estimate["processor"], "core2x")

    def test_setup_gmail_run_completes_and_auto_approves_parallel(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            accounts = tmp_path / "accounts.json"
            duckdb = tmp_path / "local-search.duckdb"
            people_csv = tmp_path / "people.csv"
            run_root = tmp_path / "runs" / "setup-gmail"
            accounts.write_text("{}", encoding="utf-8")
            duckdb.write_bytes(b"0" * 2048)
            people_csv.write_text("id\n1\n", encoding="utf-8")
            args = SimpleNamespace(accounts=str(accounts), operator_id="arthur", run_id="gmail-happy", approve_spend=True)
            import_payload = {"status": "completed", "outputs": {"people_csv": str(people_csv)}, "stats": {"people": 1}}
            with mock.patch.object(setup_gmail, "RUN_ROOT", run_root), \
                mock.patch.object(setup_gmail, "linked_gmail_accounts", return_value=["ada@example.com"]), \
                mock.patch.object(setup_gmail, "_check_gmail_tokens", return_value=[]), \
                mock.patch.object(setup_gmail, "estimate_parallel_spend", return_value={"pending_contacts": 12, "estimated_usd": 0.6}), \
                mock.patch.object(setup_gmail.gmail_discovery, "discover", return_value={"status": "completed", "contacts": 3, "selected_accounts": ["ada@example.com"]}), \
                mock.patch.object(setup_gmail.gmail_import, "run", return_value=import_payload) as import_mock, \
                mock.patch.object(setup_gmail.index_contacts_pipeline, "run_pipeline", return_value=({"status": "ready", "duckdb": str(duckdb)}, 0)):
                payload = setup_gmail.run(args)

            self.assertEqual(payload["status"], "completed")
            import_mock.assert_called_once()
            import_ns = import_mock.call_args.args[0]
            self.assertTrue(import_ns.approve_parallel_spend)
            self.assertEqual(payload["linked_accounts"], ["ada@example.com"])
            self.assertTrue((run_root / "status.json").exists())

    def test_setup_gmail_run_fails_when_no_linked_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            accounts = tmp_path / "accounts.json"
            run_root = tmp_path / "runs" / "setup-gmail"
            accounts.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(accounts=str(accounts), operator_id="arthur", run_id="gmail-empty")
            with mock.patch.object(setup_gmail, "RUN_ROOT", run_root), \
                mock.patch.object(setup_gmail, "linked_gmail_accounts", return_value=[]), \
                mock.patch.object(setup_gmail.gmail_discovery, "discover") as discover_mock:
                payload = setup_gmail.run(args)
            self.assertEqual(payload["status"], "failed")
            self.assertIn("Gmail", payload["error"])
            discover_mock.assert_not_called()

    def test_setup_gmail_run_does_not_complete_when_index_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            accounts = tmp_path / "accounts.json"
            people_csv = tmp_path / "people.csv"
            run_root = tmp_path / "runs" / "setup-gmail"
            accounts.write_text("{}", encoding="utf-8")
            people_csv.write_text("id\n1\n", encoding="utf-8")
            args = SimpleNamespace(accounts=str(accounts), operator_id="arthur", run_id="gmail-not-ready", approve_spend=True)
            import_payload = {"status": "completed", "outputs": {"people_csv": str(people_csv)}, "stats": {"people": 1}}
            with mock.patch.object(setup_gmail, "RUN_ROOT", run_root), \
                mock.patch.object(setup_gmail, "linked_gmail_accounts", return_value=["ada@example.com"]), \
                mock.patch.object(setup_gmail, "_check_gmail_tokens", return_value=[]), \
                mock.patch.object(setup_gmail, "estimate_parallel_spend", return_value={"pending_contacts": 0, "estimated_usd": 0}), \
                mock.patch.object(setup_gmail.gmail_discovery, "discover", return_value={"status": "completed", "contacts": 3}), \
                mock.patch.object(setup_gmail.gmail_import, "run", return_value=import_payload), \
                mock.patch.object(setup_gmail.index_contacts_pipeline, "run_pipeline", return_value=({"status": "not_ready", "reason": "missing_people_csv"}, 0)):
                payload = setup_gmail.run(args)
            self.assertEqual(payload["status"], "failed")
            self.assertIn("missing_people_csv", payload["error"])

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

    def test_index_run_does_not_mark_ready_for_partial_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            people = root / ".powerpacks/network-import/merged/people.csv"
            people.parent.mkdir(parents=True)
            people.write_text("id\n1\n", encoding="utf-8")
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
                "counts": {"pending_people": 1},
                "estimated_paid_calls": {"role_enrichment": 1},
                "estimated_cost_usd": 0.01,
            }
            partial = {"status": "partial", "next_step": "build_roles"}
            with mock.patch.object(index_contacts, "ROOT", root), \
                mock.patch.object(index_contacts, "run_fan_in", return_value=({"status": "completed", "step": "fan_in"}, 0)), \
                mock.patch.object(index_contacts, "maybe_materialize_existing_records", return_value={"status": "skipped", "reason": "missing_records"}), \
                mock.patch.object(index_contacts, "run_json_command", side_effect=[(0, estimate, ""), (0, partial, "")]) as run_json:
                payload, code = index_contacts.run_pipeline(args)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["reason"], "processing_incomplete")
        self.assertEqual(payload["processing"], partial)
        self.assertEqual(run_json.call_count, 2)

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
            records = root / ".powerpacks/search-index/records/people.records.parquet"
            records.parent.mkdir(parents=True)
            write_parquet_rows(records, [{"id": "newer-record"}])
            args = SimpleNamespace(
                people_csv=".powerpacks/network-import/merged/people.csv",
                output_dir=".powerpacks/search-index",
            )
            with mock.patch.object(index_contacts, "ROOT", root):
                self.assertFalse(index_contacts.duckdb_current_for_processing_hashes(args))
                freshness = index_contacts.duckdb_freshness_payload(args)
            self.assertEqual(freshness["reason"], "stale_duckdb_inputs")
            self.assertIn(".powerpacks/search-index/records/people.records.parquet", freshness["stale_inputs"])


if __name__ == "__main__":
    unittest.main()
