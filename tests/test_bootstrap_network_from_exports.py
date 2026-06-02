import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/bootstrap_network_from_exports/bootstrap_network_from_exports.py"
spec = importlib.util.spec_from_file_location("bootstrap_network_from_exports", MODULE_PATH)
bootstrap_network_from_exports = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = bootstrap_network_from_exports
spec.loader.exec_module(bootstrap_network_from_exports)


class BootstrapNetworkFromExportsTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = bootstrap_network_from_exports.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def test_generate_operator_bundle_uses_full_operator_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source_dir = tmp / "exports"
            source_dir.mkdir()
            mapping = tmp / "operator_mapping.json"
            mapping.write_text(
                json.dumps({
                    "_users": {"17d602f7-f073-40b4-97a1-dba00c574442": "patrick"},
                    "17d602f7-f073-40b4-97a1-dba00c574442": ["f48f06f0-db0c-4743-b588-da475a62e49b"],
                }),
                encoding="utf-8",
            )
            (source_dir / "parallel_enriched_f48f06f0_17d602f7_test.csv").write_text(
                "full_name,company,email,linkedin_url,status,basis.linkedin_url.confidence,basis.linkedin_url.reasoning\n"
                "Pat Example,Acme,pat@example.com,https://www.linkedin.com/in/pat-example,completed,high,matched\n",
                encoding="utf-8",
            )
            (source_dir / "linkedin_candidates_merged_17d602f7.csv").write_text(
                "operator_id,primary_email,display_name,confirmed_linkedin_url\n"
                "17d602f7-f073-40b4-97a1-dba00c574442,pat@example.com,Pat Example,https://www.linkedin.com/in/pat-example\n",
                encoding="utf-8",
            )
            with (source_dir / "harmonic_enriched_17d602f7.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["linkedin_url", "public_identifier", "harmonic_response", "harmonic_location"])
                writer.writeheader()
                writer.writerow({
                    "linkedin_url": "https://www.linkedin.com/in/pat-example",
                    "public_identifier": "pat-example",
                    "harmonic_response": json.dumps({"full_name": "Pat Example", "experience": []}),
                    "harmonic_location": "{}",
                })
            linkedin_csv = tmp / "Connections.csv"
            linkedin_csv.write_text(
                "Notes:\n\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                "Pat,Example,https://www.linkedin.com/in/pat-example,,Acme,CEO,01 Jan 2024\n",
                encoding="utf-8",
            )
            output_root = tmp / "bootstrap"
            code, payload = self.invoke([
                "generate",
                "--operator-mapping", str(mapping),
                "--source-dir", str(source_dir),
                "--operators", "patrick",
                "--linkedin-csv", str(linkedin_csv),
                "--output-root", str(output_root),
                "--seed-profile-cache",
                "--profile-cache-dir", str(tmp / "cache"),
                "--force",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["operators"][0]["operator_id"], "17d602f7-f073-40b4-97a1-dba00c574442")
            manifest = json.loads((output_root / "operators/patrick/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["linkedin_resolution_rows"], 1)
            self.assertEqual(manifest["counts"]["linkedin_connections_cached_rows"], 1)
            self.assertTrue((output_root / "operators/patrick/resolution/linkedin_resolutions_cached.csv").exists())
            self.assertTrue((output_root / "operators/patrick/inputs/linkedin_candidates/linkedin_candidates_merged_17d602f7.csv").exists())
            self.assertTrue((output_root / "operators/patrick/inputs/linkedin_candidates_manifest.csv").exists())
            with (output_root / "operators/patrick/resolution/linkedin_resolutions_cached.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["handle"], "pat@example.com")
            self.assertTrue((tmp / "cache/pat-example.json").exists())


if __name__ == "__main__":
    unittest.main()
