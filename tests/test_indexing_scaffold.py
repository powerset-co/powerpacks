import json
import tempfile
import unittest
from pathlib import Path

from packs.indexing.lib.contracts import CANONICAL_PEOPLE_CSV, validate_people_csv
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.shared.csv_io import CsvIO
from packs.indexing.lib.identity import canonical_person_key
from packs.indexing.lib.ledger import load_ledger, mark_step, next_pending_step, save_ledger
from packs.indexing.lib.text import normalize_text, stable_text_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"


class IndexingScaffoldTests(unittest.TestCase):
    def test_indexing_skill_and_task_exist(self) -> None:
        skill = ROOT / "packs/indexing/skills/build-local-search-index/SKILL.md"
        task = ROOT / "packs/indexing/tasks/build-local-search-index.task.json"
        readme = ROOT / "packs/indexing/README.md"

        self.assertTrue(skill.exists())
        self.assertTrue(task.exists())
        self.assertTrue(readme.exists())
        self.assertIn(str(CANONICAL_PEOPLE_CSV), skill.read_text())
        self.assertIn(str(CANONICAL_PEOPLE_CSV), readme.read_text())

        task_json = json.loads(task.read_text())
        self.assertEqual(task_json["task"], "build_local_search_index")
        self.assertEqual(task_json["inputs"]["people_csv"], str(CANONICAL_PEOPLE_CSV))

    def test_contract_accepts_fixture_people_csv(self) -> None:
        # The fixture predates enrichment_status/enrichment_error, exactly like
        # every people.csv already on a user's disk. Newer schema columns are
        # OPTIONAL: their absence is a warning, never an error, and the file
        # still validates.
        result = validate_people_csv(FIXTURE_PEOPLE)
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.row_count, 4)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [
            "missing optional people schema columns: enrichment_error, enrichment_status",
            "rows with weak generated identity: 1",
        ])
        self.assertIn("id", result.columns)
        self.assertIn("needs_review", result.columns)

    def test_contract_reports_missing_required_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "people.csv"
            bad.write_text("id,full_name\n1,Ada Lovelace\n", encoding="utf-8")
            result = validate_people_csv(bad)
            self.assertFalse(result.ok)
            self.assertIn("missing required people schema columns", result.errors[0])

    def test_identity_and_text_helpers_are_deterministic(self) -> None:
        with FIXTURE_PEOPLE.open(newline="", encoding="utf-8") as handle:
            rows = list(CsvIO.dict_reader(handle))
        self.assertEqual(canonical_person_key(rows[0]), "linkedin:founder-example")
        self.assertEqual(canonical_person_key(rows[1]), "id:person-engineer")
        self.assertEqual(normalize_text(" Ada\n Lovelace "), "ada lovelace")
        self.assertEqual(stable_text_hash("Ada Lovelace"), stable_text_hash("Ada   Lovelace"))

    def test_ledger_json_helpers_track_steps_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            ledger = {
                "primitive": "build_processing_pipeline",
                "version": 1,
                "status": "pending",
                "run_id": "fixture",
                "run_dir": td,
                "input": "people.csv",
                "steps": [{"id": "flatten_people", "status": "pending"}],
                "artifacts": {},
            }
            save_ledger(path, ledger)
            loaded = load_ledger(path)
            self.assertEqual(loaded["run_id"], "fixture")
            self.assertEqual(next_pending_step(loaded, ["flatten_people"]), "flatten_people")
            updated = mark_step(path, loaded, "flatten_people", "completed", artifacts={"flattened_people": "out.jsonl"})
            self.assertEqual(updated["status"], "completed")
            self.assertEqual(updated["artifacts"]["flattened_people"], "out.jsonl")
            self.assertIsNone(next_pending_step(load_ledger(path), ["flatten_people"]))

    def test_merge_emits_only_people_csv_and_its_manifest(self) -> None:
        """The merge's whole output surface: `people.csv` + `manifest.json`."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            header = ("id,public_identifier,linkedin_url,first_name,last_name,full_name,"
                      "source_channels,rapidapi_response\n")
            linkedin_csv = base / "import/linkedin/people.csv"
            linkedin_csv.parent.mkdir(parents=True)
            linkedin_csv.write_text(
                header
                + "1,jordan-bravo,https://www.linkedin.com/in/jordan-bravo,Jordan,Bravo,Jordan Bravo,"
                  "linkedin_csv,\"{\"\"full_name\"\":\"\"Jordan Bravo\"\",\"\"experiences\"\":[{\"\"title\"\":\"\"Founder\"\"}]}\"\n",
                encoding="utf-8",
            )
            gmail_csv = base / "import/gmail/people.csv"
            gmail_csv.parent.mkdir(parents=True)
            gmail_csv.write_text(
                header
                + "2,casey-delta,https://www.linkedin.com/in/casey-delta,Casey,Delta,Casey Delta,"
                  "gmail_msgvault,\"{\"\"full_name\"\":\"\"Casey Delta\"\",\"\"experiences\"\":[{\"\"title\"\":\"\"Admiral\"\"}]}\"\n",
                encoding="utf-8",
            )
            out_dir = base / "merged"
            payload = PeopleMerge(
                inputs=[linkedin_csv, gmail_csv],
                output_dir=out_dir,
                directory_csv=base / "directory.csv",
            ).run().to_payload()
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["stats"]["input_rows_total"], 2)
            self.assertEqual(payload["stats"]["rows"], 2)
            self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["manifest.json", "people.csv"])
            self.assertTrue(validate_people_csv(out_dir / "people.csv").ok)


if __name__ == "__main__":
    unittest.main()
