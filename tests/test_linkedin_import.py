import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from packs.shared.csv_io import CsvIO

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/imports/linkedin/network_import.py"
spec = importlib.util.spec_from_file_location("linkedin_import", MODULE_PATH)
linkedin_import = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = linkedin_import
spec.loader.exec_module(linkedin_import)


class LinkedInNetworkImportTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = linkedin_import.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_connections(self, path: Path, rows=None):
        if rows is None:
            rows = [
                ("Jane", "Example", "jane-example", "jane@example.com", "Acme", "CEO", "01 Jan 2024"),
                ("Jane", "Example", "jane-example", "jane@example.com", "Acme", "CEO", "01 Jan 2024"),
            ]
        body = "\n".join(
            f"{first},{last},https://www.linkedin.com/in/{public_id},{email},{company},{position},{connected_on}"
            for first, last, public_id, email, company, position, connected_on in rows
        )
        path.write_text(
            "Notes:\nExported from LinkedIn\n\n"
            "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            f"{body}\n",
            encoding="utf-8",
        )

    def cache_entry(self, public_identifier="jane-example", full_name="Jane Example", company="Acme", title="CEO"):
        raw = {
            "full_name": full_name,
            "headline": f"{title} at {company}",
            "geo": {"city": "San Francisco, California", "country": "United States", "full": "San Francisco, California, United States"},
            "experiences": [{
                "title": title,
                "companyName": company,
                "company_id": "123",
                "company_linkedin_url": f"https://linkedin.com/company/{company.lower()}",
                "starts_at": {"year": 2020},
                "ends_at": None,
            }],
            "education": [{"school": "Stanford"}],
        }
        return {
            "fetched_at": "2026-01-01T00:00:00Z",
            "public_identifier": public_identifier,
            "linkedin_url": f"https://www.linkedin.com/in/{public_identifier}",
            "raw_response": raw,
            "normalized_profile": {"success": True},
        }

    def test_run_converts_and_enriches_uncached_rapidapi_call_without_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Connections.csv"
            self.write_connections(csv_path)
            ledger = Path(tmp) / "ledger.json"
            cache_dir = Path(tmp) / "profile_cache"
            with patch.dict("os.environ", {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(linkedin_import.people_enrichment, "rapidapi_profile", return_value={"status_code": 200, "data": self.cache_entry()["raw_response"], "error": "", "from_cache": False}):
                    code, payload = self.invoke([
                        "run",
                        "--csv", str(csv_path),
                        "--source-user", "arthur",
                        "--operator-id", "operator-12345678",
                        "--output-dir", str(Path(tmp) / "out"),
                        "--ledger", str(ledger),
                        "--profile-cache-dir", str(cache_dir),
                        "--force",
                    ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            state = json.loads(ledger.read_text(encoding="utf-8"))
            out = Path(state["artifacts"]["source_people_csv"])
            self.assertTrue(out.exists())
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "jane-example")
            self.assertEqual(rows[0]["source_channels"], "linkedin_csv")
            self.assertNotIn("blocked", state)
            self.assertEqual(state["steps"]["enrich_people"]["summary"]["paid_call_count"], 1)
            discover_dir = Path(tmp) / "out" / "discover" / "linkedin"
            self.assertEqual(Path(state["artifact_dir"]), discover_dir)
            self.assertEqual(Path(state["artifacts"]["people_csv"]), discover_dir / "people.csv")
            self.assertFalse((Path(tmp) / "out" / "linkedin" / "run-test").exists())
            self.assertTrue(Path(state["artifacts"]["people_csv"]).exists())

    def test_seeded_cache_end_to_end_writes_people_csv_without_network_or_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Connections.csv"
            self.write_connections(csv_path)
            cache_dir = Path(tmp) / "profile_cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps(self.cache_entry()), encoding="utf-8")
            ledger = Path(tmp) / "ledger.json"
            with patch.object(linkedin_import.people_enrichment, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run",
                    "--csv", str(csv_path),
                    "--source-user", "arthur",
                    "--output-dir", str(Path(tmp) / "out"),
                    "--ledger", str(ledger),
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
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["full_name"], "Jane Example")
            self.assertEqual(rows[0]["location_raw"], "San Francisco, California, United States")
            self.assertEqual(rows[0]["city"], "San Francisco")
            self.assertEqual(rows[0]["state"], "California")
            self.assertEqual(rows[0]["country"], "United States")
            self.assertEqual(rows[0]["current_company"], "Acme")
            self.assertEqual(rows[0]["enrichment_provider"], "rapidapi")
            experiences = json.loads(rows[0]["work_experiences"])
            self.assertEqual(experiences[0]["company_key"], "linkedin_company:acme")
            self.assertTrue(Path(state["artifacts"]["people_csv"]).exists())

    def test_rerun_updates_same_stable_discover_artifacts_without_run_id_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "Connections.csv"
            cache_dir = tmp_path / "profile_cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps(self.cache_entry()), encoding="utf-8")
            (cache_dir / "sam-example.json").write_text(
                json.dumps(self.cache_entry("sam-example", "Sam Example", "BetaCo", "Founder")),
                encoding="utf-8",
            )
            ledger = tmp_path / "ledger.json"
            out_dir = tmp_path / "out"
            discover_dir = out_dir / "discover" / "linkedin"

            self.write_connections(csv_path, [("Jane", "Example", "jane-example", "jane@example.com", "Acme", "CEO", "01 Jan 2024")])
            with patch.object(linkedin_import.people_enrichment, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run",
                    "--csv", str(csv_path),
                    "--source-user", "arthur",
                    "--output-dir", str(out_dir),
                    "--ledger", str(ledger),
                    "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            first_state = json.loads(ledger.read_text(encoding="utf-8"))
            artifact_keys = [
                "connections_csv",
                "source_people_csv",
                "people_csv",
                "provider_enriched_csv",
                "linkedin_enrichment_queue_csv",
                "rapidapi_cache_hits_csv",
                "rapidapi_cache_misses_csv",
                "rapidapi_recent_failures_csv",
                "needs_resolution_queue_csv",
                "skipped_enrichment_csv",
                "raw_provider_responses_dir",
                "enrich_people_ledger",
            ]
            first_paths = {key: first_state["artifacts"][key] for key in artifact_keys}
            self.assertEqual(Path(first_state["artifact_dir"]), discover_dir)

            self.write_connections(csv_path, [
                ("Jane", "Example", "jane-example", "jane@example.com", "Acme", "CEO", "01 Jan 2024"),
                ("Sam", "Example", "sam-example", "sam@example.com", "BetaCo", "Founder", "02 Jan 2024"),
            ])
            with patch.object(linkedin_import.people_enrichment, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run",
                    "--csv", str(csv_path),
                    "--source-user", "arthur",
                    "--output-dir", str(out_dir),
                    "--ledger", str(ledger),
                    "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            second_state = json.loads(ledger.read_text(encoding="utf-8"))
            second_paths = {key: second_state["artifacts"][key] for key in artifact_keys}
            self.assertEqual(second_paths, first_paths)
            self.assertEqual(Path(second_state["artifacts"]["people_csv"]), discover_dir / "people.csv")
            self.assertEqual(Path(second_state["artifacts"]["enrich_people_ledger"]), discover_dir / "enrich_people.ledger.json")
            self.assertFalse((out_dir / "linkedin" / "first-run").exists())
            self.assertFalse((out_dir / "linkedin" / "second-run").exists())

            with (discover_dir / "people.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["public_identifier"] for row in rows], ["jane-example", "sam-example"])
            self.assertEqual(second_state["steps"]["enrich_people"]["summary"]["cache_hit_count"], 2)

    def test_check_keys_delegates_to_rapidapi_only_enrichment(self):
        code, payload = self.invoke(["check-keys"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "rapidapi")
        self.assertEqual(set(payload["keys_present"].keys()), {"RAPIDAPI_KEY", "RAPIDAPI_LINKEDIN_KEY"})
        self.assertTrue(all(isinstance(v, bool) for v in payload["keys_present"].values()))


if __name__ == "__main__":
    unittest.main()
