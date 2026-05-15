import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py"
spec = importlib.util.spec_from_file_location("linkedin_network_import", MODULE_PATH)
linkedin_network_import = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = linkedin_network_import
spec.loader.exec_module(linkedin_network_import)


class LinkedInNetworkImportTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = linkedin_network_import.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_connections(self, path: Path):
        path.write_text(
            "Notes:\nExported from LinkedIn\n\n"
            "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            "Jane,Example,https://www.linkedin.com/in/jane-example,jane@example.com,Acme,CEO,01 Jan 2024\n"
            "Jane,Example,https://www.linkedin.com/in/jane-example,jane@example.com,Acme,CEO,01 Jan 2024\n",
            encoding="utf-8",
        )

    def cache_entry(self):
        raw = {
            "full_name": "Jane Example",
            "headline": "CEO at Acme",
            "experiences": [{
                "title": "CEO",
                "companyName": "Acme",
                "company_id": "123",
                "company_linkedin_url": "https://linkedin.com/company/acme",
                "starts_at": {"year": 2020},
                "ends_at": None,
            }],
            "education": [{"school": "Stanford"}],
        }
        return {
            "fetched_at": "2026-01-01T00:00:00Z",
            "public_identifier": "jane-example",
            "linkedin_url": "https://www.linkedin.com/in/jane-example",
            "raw_response": raw,
            "normalized_profile": {"success": True},
        }

    def test_run_converts_and_blocks_before_uncached_rapidapi_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Connections.csv"
            self.write_connections(csv_path)
            ledger = Path(tmp) / "ledger.json"
            with patch.dict("os.environ", {"RAPIDAPI_KEY": "r"}, clear=True):
                code, payload = self.invoke([
                    "run",
                    "--csv", str(csv_path),
                    "--source-user", "arthur",
                    "--operator-id", "operator-12345678",
                    "--output-dir", str(Path(tmp) / "out"),
                    "--ledger", str(ledger),
                    "--run-id", "run-test",
                    "--force",
                ])
            self.assertEqual(code, 20)
            self.assertEqual(payload["step_id"], "enrich_people")
            state = json.loads(ledger.read_text(encoding="utf-8"))
            out = Path(state["artifacts"]["source_people_csv"])
            self.assertTrue(out.exists())
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "jane-example")
            self.assertEqual(rows[0]["source_channels"], "linkedin_csv")
            self.assertIn("delegate_ledger", state["blocked"])

    def test_seeded_cache_end_to_end_writes_people_csv_without_network_or_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Connections.csv"
            self.write_connections(csv_path)
            cache_dir = Path(tmp) / "profile_cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps(self.cache_entry()), encoding="utf-8")
            ledger = Path(tmp) / "ledger.json"
            with patch.object(linkedin_network_import.people_enrichment, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run",
                    "--csv", str(csv_path),
                    "--source-user", "arthur",
                    "--output-dir", str(Path(tmp) / "out"),
                    "--ledger", str(ledger),
                    "--run-id", "run-cache",
                    "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            state = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(state["steps"]["enrich_people"]["summary"]["cache_hit_count"], 1)
            self.assertEqual(state["steps"]["enrich_people"]["summary"]["paid_call_count"], 0)
            people_path = Path(state["artifacts"]["people_csv"])
            self.assertEqual(people_path.name, "people.csv")
            self.assertTrue(people_path.exists())
            with people_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["full_name"], "Jane Example")
            self.assertEqual(rows[0]["current_company"], "Acme")
            self.assertEqual(rows[0]["enrichment_provider"], "rapidapi")
            experiences = json.loads(rows[0]["work_experiences"])
            self.assertEqual(experiences[0]["company_key"], "rapidapi:123")
            self.assertTrue(Path(state["artifacts"]["people_enriched_csv"]).exists())

    def test_check_keys_delegates_to_rapidapi_only_enrichment(self):
        code, payload = self.invoke(["check-keys"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "rapidapi")
        self.assertEqual(set(payload["keys_present"].keys()), {"RAPIDAPI_KEY", "RAPIDAPI_LINKEDIN_KEY"})
        self.assertTrue(all(isinstance(v, bool) for v in payload["keys_present"].values()))


if __name__ == "__main__":
    unittest.main()
