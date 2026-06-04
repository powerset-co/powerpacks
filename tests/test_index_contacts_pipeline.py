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
    def test_run_promotes_fan_in_before_spend_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            final = tmp / ".powerpacks/network-import/final/merged"
            final.mkdir(parents=True)
            (final / "people.csv").write_text("id,linkedin_url,rapidapi_profile\np1,https://linkedin.com/in/a,{}\n", encoding="utf-8")
            (final / "merge_manifest.json").write_text("{}\n", encoding="utf-8")

            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            calls: list[list[str]] = []

            def fake_run_json_command(cmd: list[str], *, timeout: int):
                calls.append(cmd)
                joined = " ".join(cmd)
                if "merge_network_sources.py" in joined:
                    return 0, {
                        "status": "completed",
                        "people_csv": ".powerpacks/network-import/final/merged/people.csv",
                        "manifest": ".powerpacks/network-import/final/merged/merge_manifest.json",
                    }, ""
                if "build_network_duckdb.py" in joined:
                    duck = tmp / ".powerpacks/network-import/index/contacts/duckdb/network.local.duckdb"
                    duck.parent.mkdir(parents=True)
                    duck.write_text("duckdb", encoding="utf-8")
                    manifest = duck.parent / "manifest.json"
                    manifest.write_text("{}\n", encoding="utf-8")
                    return 0, {
                        "status": "completed",
                        "duckdb": ".powerpacks/network-import/index/contacts/duckdb/network.local.duckdb",
                        "manifest": ".powerpacks/network-import/index/contacts/duckdb/manifest.json",
                    }, ""
                if "build_processing_pipeline.py" in joined and "--dry-run" in cmd:
                    return 0, {
                        "status": "dry_run",
                        "estimated_cost_usd": 25.0,
                        "estimated_costs": {"known_pricing": True, "total_estimated_usd": 25.0},
                        "estimated_paid_calls": {"role_enrichment": 40},
                    }, ""
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
                limit=None,
                limit_mode="missing",
                auto_spend_limit_usd=10.0,
                approve_provider_spend=False,
            )

            try:
                with mock.patch.object(index_contacts_pipeline, "run_json_command", side_effect=fake_run_json_command):
                    payload, code = index_contacts_pipeline.run_pipeline(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(code, 20)
            self.assertEqual(payload["status"], "blocked_approval")
            self.assertTrue((tmp / ".powerpacks/network-import/merged/people.csv").exists())
            self.assertEqual(payload["people_sha256"], index_contacts_pipeline.sha256_file(tmp / ".powerpacks/network-import/merged/people.csv"))
            manifest = json.loads((tmp / ".powerpacks/network-import/index/contacts/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "blocked_approval")
            self.assertTrue(any("merge_network_sources.py" in " ".join(cmd) for cmd in calls))
            self.assertTrue(any("build_network_duckdb.py" in " ".join(cmd) for cmd in calls))
            self.assertFalse(any("build-local-duckdb-shim.py" in " ".join(cmd) for cmd in calls))


if __name__ == "__main__":
    unittest.main()
