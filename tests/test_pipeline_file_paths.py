import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.pipeline_paths import (
    ENRICHED_PEOPLE_CSV,
    ENRICHMENT_LEDGER_JSON,
    INDEX_LEDGER_JSON,
    INDEX_PEOPLE_RECORDS_JSONL,
    INDEX_RUN_DIR,
    MERGE_MANIFEST_JSON,
    MERGE_REVIEW_CSV,
    MERGED_PEOPLE_CSV,
    PIPELINE_DAG,
    pipeline_dag_as_dict,
)
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
MERGE = ROOT / "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py"
ENRICH = ROOT / "packs/ingestion/primitives/enrich_people/enrich_people.py"
INDEX = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"


class PipelineFilePathTests(unittest.TestCase):
    def test_pipeline_dag_contains_canonical_no_path_stage_commands(self):
        stages = {row["id"]: row for row in pipeline_dag_as_dict()}
        self.assertEqual(stages["merge"]["outs"], [str(MERGED_PEOPLE_CSV), str(MERGE_REVIEW_CSV), str(MERGE_MANIFEST_JSON)])
        self.assertEqual(stages["enrich"]["deps"], [str(MERGED_PEOPLE_CSV)])
        self.assertIn(str(ENRICHED_PEOPLE_CSV), stages["enrich"]["outs"])
        self.assertIn(str(ENRICHMENT_LEDGER_JSON), stages["enrich"]["outs"])
        self.assertIn(str(INDEX_LEDGER_JSON), stages["index"]["outs"])
        self.assertIn(str(INDEX_PEOPLE_RECORDS_JSONL), stages["index"]["outs"])
        self.assertNotIn("--input", stages["merge"]["command"])
        self.assertNotIn("--input", stages["enrich"]["command"])
        self.assertNotIn("--input", stages["index"]["command"])
        self.assertEqual([stage.id for stage in PIPELINE_DAG], [row["id"] for row in pipeline_dag_as_dict()])

    def test_merge_writes_canonical_people_review_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source_dir = work / ".powerpacks/network-import/linkedin/run-1"
            source_dir.mkdir(parents=True)
            source = source_dir / "people.csv"
            row = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
            row.update({
                "id": "person-1",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "public_identifier": "jane-example",
                "first_name": "Jane",
                "last_name": "Example",
                "full_name": "Jane Example",
                "source_channels": "linkedin",
            })
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            proc = subprocess.run([sys.executable, str(MERGE), "run"], cwd=work, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["output"], str(MERGED_PEOPLE_CSV))
            self.assertEqual(payload["review_output"], str(MERGE_REVIEW_CSV))
            self.assertEqual(payload["manifest"], str(MERGE_MANIFEST_JSON))
            self.assertTrue((work / MERGED_PEOPLE_CSV).exists())
            self.assertTrue((work / MERGE_REVIEW_CSV).exists())
            manifest = json.loads((work / MERGE_MANIFEST_JSON).read_text(encoding="utf-8"))
            self.assertEqual(manifest["merged_rows"], 1)
            self.assertEqual(manifest["inputs"], {str(Path(".powerpacks/network-import/linkedin/run-1/people.csv")): 1})

    def test_enrich_and_index_plan_are_pathless_by_default(self):
        enrich_help = subprocess.run([sys.executable, str(ENRICH), "run", "--help"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        index_help = subprocess.run([sys.executable, str(INDEX), "run", "--help"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertNotIn("--input", enrich_help)
        self.assertNotIn("--output-dir", enrich_help)
        self.assertNotIn("--ledger", enrich_help)
        self.assertNotIn("--input", index_help)
        self.assertNotIn("--output-dir", index_help)
        self.assertNotIn("--run-id", index_help)

        plan = subprocess.run([sys.executable, str(INDEX), "plan"], cwd=ROOT, capture_output=True, text=True, check=True)
        payload = json.loads(plan.stdout)
        self.assertEqual(payload["run_dir"], str(INDEX_RUN_DIR))
        self.assertEqual(payload["artifacts"]["ledger"], str(INDEX_LEDGER_JSON))
        self.assertIn(payload["input"], [str(ENRICHED_PEOPLE_CSV), str(MERGED_PEOPLE_CSV)])


if __name__ == "__main__":
    unittest.main()
