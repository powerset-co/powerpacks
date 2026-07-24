import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.indexing.lib.artifact_io import write_parquet_rows
from packs.ingestion.primitives.enrich import profile_cache, rapidapi_client
from packs.shared.csv_io import CsvIO

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


discover_common = load_module(
    "phase13_discover_common",
    "packs/ingestion/primitives/discover/common.py",
)
import_common = load_module(
    "phase13_import_common",
    "packs/ingestion/primitives/imports/common.py",
)
index_contacts = load_module(
    "phase13_index_contacts",
    "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
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
    "packs/ingestion/primitives/enrich/enrich_people.py",
)


class PipelinePhase13Tests(unittest.TestCase):
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
        self.assertEqual(cmd[cmd.index("--openai-usage-tier") + 1], "tier_1")

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
        self.assertEqual(rapidapi_client.DEFAULT_RAPIDAPI_MAX_WORKERS, 64)
        self.assertEqual(rapidapi_client.DEFAULT_RAPIDAPI_MAX_RPM, 300)

    def test_rapidapi_profile_retries_429_then_succeeds(self):
        payload = {
            "public_identifier": "ada",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "experiences": [{"title": "Founder", "company_name": "Analytical Engines"}],
        }
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(rapidapi_client, "DEFAULT_RAPIDAPI_RETRY_ATTEMPTS", 3), \
            mock.patch.object(rapidapi_client, "DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS", 1.0), \
            mock.patch.object(rapidapi_client, "http_json", side_effect=[(429, {"message": "Too many requests"}, ""), (200, payload, "")]) as http_json, \
            mock.patch.object(rapidapi_client.time, "sleep") as sleep:
            wait = mock.Mock()
            result = rapidapi_client.rapidapi_profile("ada", "https://www.linkedin.com/in/ada", "key", cache_dir=tmp, refresh_cache=True, wait_for_attempt=wait)
            cached = json.loads((Path(tmp) / "ada.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(http_json.call_count, 2)
        self.assertEqual(wait.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(cached["attempts"], 2)

    def test_rapidapi_profile_records_final_retry_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(rapidapi_client, "DEFAULT_RAPIDAPI_RETRY_ATTEMPTS", 2), \
            mock.patch.object(rapidapi_client, "DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS", 0.5), \
            mock.patch.object(rapidapi_client, "http_json", side_effect=[(429, {"message": "Too many requests"}, ""), (429, {"message": "Too many requests"}, "")]) as http_json, \
            mock.patch.object(rapidapi_client.time, "sleep") as sleep:
            result = rapidapi_client.rapidapi_profile("ada", "https://www.linkedin.com/in/ada", "key", cache_dir=tmp, refresh_cache=True)
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
            cfg = enrich_people.build_config(
                input_csv=root / "people.csv",
                artifact_dir=artifact_dir,
                profile_cache_dir=root / "cache",
                refresh_cache=True,
                max_workers=1,
                max_rpm=0,
            )
            orchestrator = enrich_people.EnrichPeople(cfg)
            hits = artifact_dir / "rapidapi_cache_hits.csv"
            misses = artifact_dir / "rapidapi_cache_misses.csv"
            CsvIO.write_dict_rows(hits, enrich_people.CACHE_COLUMNS, [])
            CsvIO.write_dict_rows(misses, enrich_people.CACHE_COLUMNS, [{
                "id": "p1",
                "public_identifier": "ada",
                "linkedin_url": "https://www.linkedin.com/in/ada",
                "cache_status": "miss",
            }])
            orchestrator.artifacts.update({"rapidapi_cache_hits_csv": str(hits), "rapidapi_cache_misses_csv": str(misses)})
            orchestrator.counts["paid_call_count"] = 1
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
                summary = orchestrator.enrich_linkedin()
            rows = CsvIO.read_dict_rows(Path(summary["output_file"]))
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
            status, reason, path, failure = profile_cache.classify_rapidapi_cache_status(
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
