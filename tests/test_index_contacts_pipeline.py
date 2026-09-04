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

PEOPLE_HEADER = "id,public_identifier,linkedin_url,full_name,source_channels\n"


def write_source_people(base: Path, source: str, rows: str) -> Path:
    path = base / ".powerpacks/network-import/import" / source / "people.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PEOPLE_HEADER + rows, encoding="utf-8")
    return path


class IndexContactsPipelineTest(unittest.TestCase):
    def test_job_description_records_use_processing_position_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            args = argparse.Namespace(
                jobs_db=None,
                jobs_jsonl=["jobs.jsonl"],
                job_description_embeddings=None,
                output_dir=".powerpacks/search-index",
                operator_id="operator-1",
            )
            try:
                with mock.patch.object(index_contacts_pipeline, "build_job_description_evidence", return_value={"matches": 2}) as build:
                    result = index_contacts_pipeline.build_job_description_records(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(result, {"status": "completed", "matches": 2})
            self.assertEqual(build.call_args.args[1], tmp / ".powerpacks/search-index/records/people.records.parquet")
            self.assertEqual(build.call_args.kwargs["jobs_jsonl"], [tmp / "jobs.jsonl"])

    def test_job_description_records_skip_unchanged_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "jobs.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            output = tmp / ".powerpacks/search-index"
            positions = output / "records/people.records.parquet"
            positions.parent.mkdir(parents=True)
            positions.write_text("positions", encoding="utf-8")
            job_records = output / "records/job_descriptions.records.parquet"
            matches = output / "records/job_description_positions.records.parquet"
            stats = output / "stats/build_job_description_evidence.json"
            stats.parent.mkdir(parents=True)
            for path in [job_records, matches]:
                path.write_text("records", encoding="utf-8")
            stats.write_text(json.dumps({"job_descriptions": 3, "matches": 2}), encoding="utf-8")
            args = argparse.Namespace(
                jobs_db=None,
                jobs_jsonl=[str(source)],
                job_description_embeddings=None,
                output_dir=".powerpacks/search-index",
                operator_id="operator-1",
            )
            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            try:
                with mock.patch.object(index_contacts_pipeline, "build_job_description_evidence") as build:
                    result = index_contacts_pipeline.build_job_description_records(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(result["reason"], "inputs_unchanged")
            self.assertEqual(result["matches"], 2)
            build.assert_not_called()

    def test_run_promotes_fan_in_then_runs_processing_after_cost_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            write_source_people(
                tmp, "linkedin",
                ",jordan-bravo,https://www.linkedin.com/in/jordan-bravo,Jordan Bravo,linkedin_csv\n",
            )

            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            calls: list[list[str]] = []

            def fake_run_json_command(cmd: list[str], *, timeout: int, stream_stderr: bool = False):
                calls.append(cmd)
                joined = " ".join(cmd)
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
                people_csv=".powerpacks/network-import/merged/people.csv",
                output_dir=".powerpacks/search-index",
                artifact_dir=".powerpacks/network-import/index/contacts",
                manifest=".powerpacks/network-import/index/contacts/manifest.json",
                input=[],
            )

            try:
                with mock.patch.object(index_contacts_pipeline, "run_json_command", side_effect=fake_run_json_command):
                    payload, code = index_contacts_pipeline.run_pipeline(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ready")
            promoted = tmp / ".powerpacks/network-import/merged/people.csv"
            self.assertTrue(promoted.exists())
            self.assertEqual(payload["people_sha256"], index_contacts_pipeline.sha256_file(promoted))
            self.assertNotIn("network_duckdb", payload["fan_in"])
            manifest = json.loads((tmp / ".powerpacks/network-import/index/contacts/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ready")
            # The merge runs IN-PROCESS: no child python process for our own merge .py.
            self.assertFalse(any("merge_people.py" in " ".join(cmd) for cmd in calls))
            self.assertFalse(any("build_network_duckdb.py" in " ".join(cmd) for cmd in calls))
            self.assertTrue(any("build-local-duckdb-shim.py" in " ".join(cmd) for cmd in calls))

    def test_fan_in_cache_only_requires_merged_people_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            write_source_people(tmp, "linkedin", ",jordan-bravo,,Jordan Bravo,linkedin_csv\n")
            merged = tmp / ".powerpacks/network-import/merged/people.csv"
            merged.parent.mkdir(parents=True)
            merged.write_text("id\np1\n", encoding="utf-8")
            manifest = tmp / ".powerpacks/network-import/index/contacts/manifest.json"
            manifest.parent.mkdir(parents=True)

            args = argparse.Namespace(
                manifest=".powerpacks/network-import/index/contacts/manifest.json",
                input=[],
                openai_usage_tier=None,
            )
            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            try:
                inputs = index_contacts_pipeline.fan_in_input_paths(args)
                manifest.write_text(json.dumps({
                    "status": "completed",
                    "step": "fan_in",
                    "input_fingerprints": index_contacts_pipeline.input_fingerprints(inputs),
                    "artifacts": {
                        "merged_people_csv": ".powerpacks/network-import/merged/people.csv",
                        "duckdb": ".powerpacks/network-import/duckdb/network.duckdb",
                    },
                    "promoted": {
                        "network_duckdb": ".powerpacks/network-import/duckdb/network.duckdb",
                    },
                    "network_duckdb": {"status": "completed"},
                }), encoding="utf-8")

                with mock.patch.object(index_contacts_pipeline, "run_merge") as run_merge:
                    payload, code = index_contacts_pipeline.run_fan_in(args)
            finally:
                index_contacts_pipeline.ROOT = old_root

            self.assertEqual(code, 0)
            self.assertTrue(payload["noop"])
            self.assertEqual(payload["reason"], "fan_in_inputs_unchanged")
            self.assertNotIn("network_duckdb", payload)
            self.assertNotIn("duckdb", payload["artifacts"])
            self.assertNotIn("network_duckdb", payload["promoted"])
            run_merge.assert_not_called()


class FanInInputSelectionTest(unittest.TestCase):
    """The fan-in feeds the merge SOURCE artifacts only."""

    def test_merged_people_csv_is_never_a_fan_in_input(self) -> None:
        # The retired --include-existing-artifacts self-feed let a re-run merge its
        # own previous output back in, self-joining every person with themselves.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            write_source_people(tmp, "linkedin", ",jordan-bravo,,Jordan Bravo,linkedin_csv\n")
            merged = tmp / ".powerpacks/network-import/merged/people.csv"
            merged.parent.mkdir(parents=True)
            merged.write_text(PEOPLE_HEADER + ",casey-delta,,Casey Delta,gmail_msgvault\n", encoding="utf-8")

            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            try:
                inputs = index_contacts_pipeline.fan_in_input_paths(argparse.Namespace(input=[]))
            finally:
                index_contacts_pipeline.ROOT = old_root
            self.assertEqual([str(path) for path in inputs],
                             [".powerpacks/network-import/import/linkedin/people.csv"])

    def test_inputs_follow_merge_source_precedence_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for source in ("messages", "gmail", "linkedin"):
                write_source_people(tmp, source, f",jordan-bravo,,Jordan Bravo,{source}\n")
            old_root = index_contacts_pipeline.ROOT
            index_contacts_pipeline.ROOT = tmp
            try:
                inputs = index_contacts_pipeline.fan_in_input_paths(argparse.Namespace(input=[]))
            finally:
                index_contacts_pipeline.ROOT = old_root
            self.assertEqual([path.parent.name for path in inputs], list(index_contacts_pipeline.MERGE_SOURCES))


if __name__ == "__main__":
    unittest.main()
