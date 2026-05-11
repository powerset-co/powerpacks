import csv
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/enrich_people/enrich_people.py"
spec = importlib.util.spec_from_file_location("enrich_people", MODULE_PATH)
enrich_people = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = enrich_people
spec.loader.exec_module(enrich_people)


class EnrichPeopleTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = enrich_people.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_people(self, path: Path, *, complete: bool = False):
        fields = enrich_people.PEOPLE_SCHEMA_COLUMNS
        row = {col: "" for col in fields}
        row.update({
            "id": "person-1",
            "public_identifier": "jane-example",
            "linkedin_url": "https://www.linkedin.com/in/jane-example",
            "first_name": "Jane",
            "last_name": "Example",
            "full_name": "Jane Example",
        })
        if complete:
            row.update({
                "headline": "CEO at Acme",
                "current_title": "CEO",
                "current_company": "Acme",
                "work_experiences": json.dumps([{"title": "CEO", "company": "Acme"}]),
            })
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)

    def test_prepare_queue_blocks_before_paid_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            ledger = Path(tmp) / "ledger.json"
            with patch.dict(os.environ, {"HARMONIC_API_KEY": "h", "RAPIDAPI_KEY": "r"}, clear=True):
                code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger)])
            self.assertEqual(code, 20)
            self.assertEqual(payload["step_id"], "enrich_linkedin")
            state = json.loads(ledger.read_text())
            queue = Path(state["artifacts"]["linkedin_enrichment_queue_csv"])
            self.assertTrue(queue.exists())
            with queue.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)

    def test_skips_complete_rows_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, complete=True)
            ledger = Path(tmp) / "ledger.json"
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            output = Path(payload["artifacts"]["people_enriched_csv"])
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["full_name"], "Jane Example")

    def test_errors_when_no_provider_enabled_for_queued_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            ledger = Path(tmp) / "ledger.json"
            code, payload = self.invoke([
                "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                "--ledger", str(ledger), "--no-harmonic", "--no-rapidapi",
            ])
            self.assertEqual(code, 1)
            self.assertIn("At least one", payload["error"])

    def test_provider_merge_prefers_richer_rapidapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            ledger = Path(tmp) / "ledger.json"
            rapid = {
                "full_name": "Jane Example",
                "headline": "CEO at Acme",
                "experiences": [{"title": "CEO", "company": "Acme"}],
                "education": [{"school": "Stanford"}],
            }
            with patch.dict(os.environ, {"HARMONIC_API_KEY": "h", "RAPIDAPI_KEY": "r"}, clear=True):
                code, _ = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger)])
                self.assertEqual(code, 20)
                self.assertEqual(self.invoke(["approve", "--ledger", str(ledger)])[0], 0)
                with patch.object(enrich_people, "harmonic_enrich", return_value={"status_code": 200, "data": {"full_name": "Jane Example", "experience": [], "education": []}, "error": ""}), \
                     patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": rapid, "error": ""}):
                    code, payload = self.invoke(["continue", "--ledger", str(ledger)])
            self.assertEqual(code, 0)
            self.assertNotIn("people_harmonic_all_csv", payload["artifacts"])
            output = Path(payload["artifacts"]["people_enriched_csv"])
            self.assertFalse((output.parent / "people_harmonic_all.csv").exists())
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("rapidapi", rows[0]["enrichment_provider"])
            self.assertEqual(rows[0]["current_company"], "Acme")


if __name__ == "__main__":
    unittest.main()
