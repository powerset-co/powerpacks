import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = ROOT / "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py"
spec = importlib.util.spec_from_file_location("index_contacts_pipeline", PIPELINE_PATH)
index_contacts_pipeline = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(index_contacts_pipeline)


class IndexContactsPipelineTest(unittest.TestCase):
    def test_run_promotes_fan_in_then_runs_processing_after_cost_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            final = tmp / ".powerpacks/network-import/final/merged"
            final.mkdir(parents=True)
            (final / "people.csv").write_text("id,linkedin_url,rapidapi_profile\np1,https://linkedin.com/in/a,{}\n", encoding="utf-8")
            (final / "merge_manifest.json").write_text("{}\n", encoding="utf-8")

            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            calls: list[list[str]] = []

            def fake_run_json_command(cmd: list[str], *, timeout: int, stream_stderr: bool = False):
                calls.append(cmd)
                joined = " ".join(cmd)
                if "merge_network_sources.py" in joined:
                    return 0, {
                        "status": "completed",
                        "people_csv": ".powerpacks/network-import/final/merged/people.csv",
                        "manifest": ".powerpacks/network-import/final/merged/merge_manifest.json",
                    }, ""
                if "build_processing_pipeline.py" in joined and "--dry-run" in cmd:
                    return 0, {
                        "status": "dry_run",
                        "estimated_cost_usd": 25.0,
                        "estimated_costs": {"known_pricing": True, "total_estimated_usd": 25.0},
                        "estimated_paid_calls": {"role_enrichment": 40},
                    }, ""
                if "build_processing_pipeline.py" in joined:
                    self.assertIn("--allow-paid-role-provider", cmd)
                    self.assertIn("--allow-paid-embeddings", cmd)
                    self.assertIn("--allow-paid-company-provider", cmd)
                    return 0, {"status": "completed", "counts": {}}, ""
                if "build-local-duckdb-shim.py" in joined:
                    duck = tmp / ".powerpacks/search-index/local-search.duckdb"
                    duck.parent.mkdir(parents=True)
                    duck.write_text("duckdb", encoding="utf-8")
                    return 0, {"status": "completed", "duckdb": ".powerpacks/search-index/local-search.duckdb"}, ""
                return 1, {"status": "unexpected"}, joined

            args = argparse.Namespace(
                operator_id="operator-1",
                accounts=".powerpacks/ingestion/accounts.json",
                people_csv=".powerpacks/network-import/merged/people.csv",
                output_dir=".powerpacks/search-index",
                artifact_dir=".powerpacks/network-import/index/contacts",
                manifest=".powerpacks/network-import/index/contacts/manifest.json",
                input=[".powerpacks/network-import/final/merged/people.csv"],
                include_existing_artifacts=False,
            )

            try:
                with mock.patch.object(index_contacts_pipeline, "run_json_command", side_effect=fake_run_json_command):
                    payload, code = index_contacts_pipeline.run_pipeline(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ready")
            self.assertTrue((tmp / ".powerpacks/network-import/merged/people.csv").exists())
            self.assertEqual(payload["people_sha256"], index_contacts_pipeline.sha256_file(tmp / ".powerpacks/network-import/merged/people.csv"))
            self.assertNotIn("network_duckdb", payload["fan_in"])
            manifest = json.loads((tmp / ".powerpacks/network-import/index/contacts/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ready")
            self.assertTrue(any("merge_network_sources.py" in " ".join(cmd) for cmd in calls))
            self.assertFalse(any("build_network_duckdb.py" in " ".join(cmd) for cmd in calls))
            self.assertTrue(any("build-local-duckdb-shim.py" in " ".join(cmd) for cmd in calls))

    def test_fan_in_cache_only_requires_merged_people_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / ".powerpacks/network-import/import/linkedin/people.csv"
            source.parent.mkdir(parents=True)
            source.write_text("id\np1\n", encoding="utf-8")
            merged = tmp / ".powerpacks/network-import/merged/people.csv"
            merged.parent.mkdir(parents=True)
            merged.write_text("id\np1\n", encoding="utf-8")
            manifest = tmp / ".powerpacks/network-import/index/contacts/manifest.json"
            manifest.parent.mkdir(parents=True)

            args = argparse.Namespace(
                manifest=".powerpacks/network-import/index/contacts/manifest.json",
                input=[],
                include_existing_artifacts=False,
                openai_usage_tier=None,
            )
            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            try:
                inputs = index_contacts_pipeline.fan_in_input_paths(args)
                manifest.write_text(json.dumps({
                    "status": "completed",
                    "step": "fan_in",
                    "input_fingerprints": index_contacts_pipeline.input_fingerprints(
                        inputs + index_contacts_pipeline.FAN_IN_OVERRIDE_FILES
                    ),
                    "artifacts": {
                        "merged_people_csv": ".powerpacks/network-import/merged/people.csv",
                        "duckdb": ".powerpacks/network-import/duckdb/network.duckdb",
                    },
                    "promoted": {
                        "network_duckdb": ".powerpacks/network-import/duckdb/network.duckdb",
                    },
                    "network_duckdb": {"status": "completed"},
                }), encoding="utf-8")

                with mock.patch.object(index_contacts_pipeline, "run_json_command") as run_command:
                    payload, code = index_contacts_pipeline.run_fan_in(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(code, 0)
            self.assertTrue(payload["noop"])
            self.assertEqual(payload["reason"], "fan_in_inputs_unchanged")
            self.assertNotIn("network_duckdb", payload)
            self.assertNotIn("duckdb", payload["artifacts"])
            self.assertNotIn("network_duckdb", payload["promoted"])
            run_command.assert_not_called()


class FanInOverrideFingerprintTest(unittest.TestCase):
    def test_override_file_change_invalidates_fan_in_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            imports = tmp / ".powerpacks/network-import/import/linkedin"
            imports.mkdir(parents=True)
            (imports / "people.csv").write_text("id\np1\n", encoding="utf-8")

            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            try:
                args = argparse.Namespace(input=[], include_existing_artifacts=False)
                inputs = index_contacts_pipeline.fan_in_input_paths(args)
                fingerprint_paths = inputs + index_contacts_pipeline.FAN_IN_OVERRIDE_FILES
                existing = index_contacts_pipeline.input_fingerprints(fingerprint_paths)

                # unchanged inputs + absent overrides -> cache hit
                self.assertTrue(index_contacts_pipeline.fan_in_fingerprints_match(
                    existing, index_contacts_pipeline.input_fingerprints(fingerprint_paths)))

                # a newly approved retarget override must invalidate the no-op cache
                overrides = tmp / ".powerpacks/network-import/overrides"
                overrides.mkdir(parents=True)
                (overrides / "retarget-people.csv").write_text("id\np2\n", encoding="utf-8")
                current = index_contacts_pipeline.input_fingerprints(
                    index_contacts_pipeline.fan_in_input_paths(args) + index_contacts_pipeline.FAN_IN_OVERRIDE_FILES)
                self.assertFalse(index_contacts_pipeline.fan_in_fingerprints_match(existing, current))

                # ... and a content edit to an existing override must too
                stale = current
                (overrides / "retarget-people.csv").write_text("id\np2\np3\n", encoding="utf-8")
                edited = index_contacts_pipeline.input_fingerprints(
                    index_contacts_pipeline.fan_in_input_paths(args) + index_contacts_pipeline.FAN_IN_OVERRIDE_FILES)
                self.assertFalse(index_contacts_pipeline.fan_in_fingerprints_match(stale, edited))

                # override files are fingerprint inputs only — never merge --input sources
                self.assertFalse(set(map(str, index_contacts_pipeline.fan_in_input_paths(args)))
                                 & set(map(str, index_contacts_pipeline.FAN_IN_OVERRIDE_FILES)))
            finally:
                index_contacts_pipeline.ROOT = old_root


if __name__ == "__main__":
    unittest.main()
