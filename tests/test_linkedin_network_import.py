import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

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

    def test_run_converts_and_blocks_before_external_apis(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Connections.csv"
            self.write_connections(csv_path)
            ledger = Path(tmp) / "ledger.json"
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
            self.assertEqual(payload["step_id"], "enrich_providers")
            state = json.loads(ledger.read_text(encoding="utf-8"))
            out = Path(state["artifacts"]["connections_csv"])
            self.assertTrue(out.exists())
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "jane-example")

    def test_merge_prefers_richer_rapidapi_experience_education(self):
        base = {
            "public_identifier": "jane-example",
            "linkedin_url": "https://www.linkedin.com/in/jane-example",
            "first_name": "Jane",
            "last_name": "Example",
        }
        harmonic = {"full_name": "Jane Example", "work_experiences": [], "education": []}
        rapid = {
            "full_name": "Jane Example",
            "headline": "CEO at Acme",
            "work_experiences": [{"title": "CEO", "company": "Acme"}],
            "education": [{"school": "Stanford"}],
        }
        row = linkedin_network_import.merge_provider_profile(base, harmonic, rapid, {"h": 1}, {"r": 1})
        self.assertIn("rapidapi", row["enrichment_provider"])
        self.assertEqual(len(json.loads(row["work_experiences"])), 1)
        self.assertEqual(len(json.loads(row["education"])), 1)

    def test_check_keys_does_not_print_secret_values(self):
        code, payload = self.invoke(["check-keys"])
        self.assertEqual(code, 0)
        self.assertEqual(set(payload["keys_present"].keys()), {"HARMONIC_API_KEY", "RAPIDAPI_KEY", "RAPIDAPI_LINKEDIN_KEY"})
        self.assertTrue(all(isinstance(v, bool) for v in payload["keys_present"].values()))


if __name__ == "__main__":
    unittest.main()
