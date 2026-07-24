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

from packs.ingestion.primitives.enrich import profile_cache, profile_transforms, rapidapi_client
from packs.ingestion.schemas.people_schema import generate_person_id
from packs.shared.csv_io import CsvIO

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/enrich/enrich_people.py"
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

    def write_people(self, path: Path, *, complete: bool = False, rapidapi_response=None, row_id="person-1"):
        fields = enrich_people.PEOPLE_SCHEMA_COLUMNS
        row = {col: "" for col in fields}
        row.update({
            "id": row_id,
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
        if rapidapi_response is not None:
            row["rapidapi_response"] = json.dumps(rapidapi_response)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)

    def profile(self, company_id="123"):
        return {
            "full_name": "Jane Example",
            "headline": "CEO at Acme",
            "experiences": [{
                "title": "CEO",
                "company": "Acme",
                "company_id": company_id,
                "company_linkedin_url": "https://linkedin.com/company/acme/",
                "starts_at": {"year": 2020, "month": 1},
                "ends_at": None,
            }],
            "education": [{"school": "Stanford"}],
        }

    def write_people_rows(self, path: Path, rows):
        fields = enrich_people.PEOPLE_SCHEMA_COLUMNS
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for raw in rows:
                row = {col: "" for col in fields}
                row.update(raw)
                writer.writerow(row)

    def cache_entry(self, profile=None):
        profile = profile or self.profile()
        return {
            "fetched_at": "2026-01-01T00:00:00Z",
            "public_identifier": "jane-example",
            "linkedin_url": "https://www.linkedin.com/in/jane-example",
            "raw_response": profile,
            "normalized_profile": {"success": True},
        }

    def test_run_with_approve_spend_fetches_cache_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}) as mocked:
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--profile-cache-dir", str(cache_dir), "--approve-spend"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(payload["counts"]["queue_count"], 1)
            self.assertEqual(payload["counts"]["paid_call_count"], 1)
            self.assertEqual(payload["counts"]["cache_hit_count"], 0)
            self.assertIsNone(payload.get("needs_approval"))
            self.assertTrue(Path(payload["manifest"]).exists())
            for artifact in ("linkedin_enrichment_queue_csv", "rapidapi_cache_misses_csv", "rapidapi_cache_hits_csv"):
                self.assertTrue(Path(payload["artifacts"][artifact]).exists())
            with Path(payload["artifacts"]["rapidapi_cache_misses_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["cache_status"], "miss")
            self.assertIn("cache_reason", rows[0])

    def test_run_without_approve_spend_stops_at_needs_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                # A cache miss must NOT fetch without approval: assert no network.
                with patch.object(rapidapi_client.RapidApiClient, "http_json", side_effect=AssertionError("network called")):
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--profile-cache-dir", str(cache_dir)])
            self.assertEqual(code, enrich_people.NEEDS_APPROVAL_CODE)
            self.assertEqual(payload["status"], "needs_approval")
            self.assertEqual(payload["counts"]["paid_call_count"], 1)
            self.assertEqual(payload["needs_approval"]["paid_call_count"], 1)
            self.assertEqual(payload["needs_approval"]["estimated_credits"], 1)
            # Gate stops before enrich/merge: no people.csv, no provider output.
            self.assertNotIn("people_csv", payload["artifacts"])
            self.assertNotIn("provider_enriched_csv", payload["artifacts"])

    def test_skips_complete_rows_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, complete=True, rapidapi_response=self.profile())
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            output = Path(payload["artifacts"]["people_csv"])
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["full_name"], "Jane Example")

    def test_errors_without_key_for_uncached_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            # Approved but no key: the spend gate is passed, so the run reaches the
            # hard missing-key failure rather than the needs_approval stop.
            with patch.dict(os.environ, {}, clear=True):
                code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--profile-cache-dir", str(cache_dir), "--approve-spend"])
            self.assertEqual(code, 1)
            self.assertIn("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY", payload["error"])

    def test_provider_merge_uses_rapidapi_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}):
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--profile-cache-dir", str(cache_dir), "--approve-spend"])
            self.assertEqual(code, 0)
            self.assertFalse(any(name.endswith("_enrich") and name != "rapidapi_profile" for name in dir(enrich_people)))
            output = Path(payload["artifacts"]["people_csv"])
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["enrichment_provider"], "rapidapi")
            self.assertEqual(rows[0]["current_company"], "Acme")
            experiences = json.loads(rows[0]["work_experiences"])
            self.assertEqual(experiences[0]["rapidapi_company_id"], "123")
            self.assertEqual(experiences[0]["company_public_identifier"], "acme")
            self.assertEqual(experiences[0]["company_key"], "linkedin_company:acme")
            self.assertEqual(rows[0]["current_company_urn"], "")

    def test_cache_hit_completes_without_key_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, rapidapi_response=self.profile())
            with patch.object(rapidapi_client.RapidApiClient, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["paid_call_count"], 0)
            self.assertEqual(payload["counts"]["cache_hit_count"], 1)
            with Path(payload["artifacts"]["provider_enriched_csv"]).open(newline="", encoding="utf-8") as handle:
                provider_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(provider_rows[0]["rapidapi_from_cache"], "true")
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                output_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(output_rows[0]["current_company"], "Acme")

    def test_bad_cache_does_not_bypass_rapidapi_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, rapidapi_response={"success": False, "message": "not found"})
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}) as mocked:
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--profile-cache-dir", str(cache_dir), "--approve-spend"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(payload["counts"]["paid_call_count"], 1)
            self.assertEqual(payload["counts"]["cache_hit_count"], 0)

    def test_rapidapi_error_preserves_base_row(self):
        base = {col: "" for col in enrich_people.PEOPLE_SCHEMA_COLUMNS}
        base.update({"id": "person-1", "public_identifier": "jane-example", "full_name": "Jane Base", "current_company": "BaseCo"})
        merged = enrich_people.merge_provider_profile(base, {}, {"success": False})
        self.assertEqual(merged["full_name"], "Jane Base")
        self.assertEqual(merged["current_company"], "BaseCo")

    def test_blank_id_generates_deterministic_id_and_existing_id_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, rapidapi_response=self.profile(), row_id="")
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["id"], generate_person_id("jane-example"))
            people2 = Path(tmp) / "people2.csv"
            self.write_people(people2, rapidapi_response=self.profile(), row_id="existing")
            code, payload = self.invoke(["run", "--input", str(people2), "--output-dir", str(Path(tmp) / "out2")])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["id"], "existing")


    def test_file_cache_hit_completes_without_key_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps(self.cache_entry()), encoding="utf-8")
            with patch.object(rapidapi_client.RapidApiClient, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                    "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["cache_hit_count"], 1)
            self.assertEqual(payload["counts"]["paid_call_count"], 0)
            with Path(payload["artifacts"]["provider_enriched_csv"]).open(newline="", encoding="utf-8") as handle:
                provider_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(provider_rows[0]["rapidapi_from_cache"], "true")
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                output_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(output_rows[0]["current_company"], "Acme")

    def test_refresh_cache_forces_cache_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps(self.cache_entry()), encoding="utf-8")
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}):
                    code, payload = self.invoke([
                        "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                        "--profile-cache-dir", str(cache_dir), "--refresh-cache", "--approve-spend",
                    ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["cache_hit_count"], 0)
            self.assertEqual(payload["counts"]["paid_call_count"], 1)

    def test_mixed_cache_and_miss_approval_counts_only_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people_rows(people, [
                {
                    "id": "jane",
                    "public_identifier": "jane-example",
                    "linkedin_url": "https://www.linkedin.com/in/jane-example",
                    "full_name": "Jane Example",
                    "rapidapi_response": json.dumps(self.profile()),
                },
                {
                    "id": "john",
                    "public_identifier": "john-example",
                    "linkedin_url": "https://www.linkedin.com/in/john-example",
                    "full_name": "John Example",
                },
            ])
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                john_profile = self.profile(company_id="456")
                john_profile["full_name"] = "John Example"
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value={"status_code": 200, "data": john_profile, "error": "", "from_cache": False}) as mocked:
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--approve-spend"])
                self.assertEqual(code, 0)
                self.assertEqual(payload["counts"]["queue_count"], 2)
                self.assertEqual(payload["counts"]["cache_hit_count"], 1)
                self.assertEqual(payload["counts"]["paid_call_count"], 1)
                self.assertEqual(profile_cache.count_rapidapi_cache_misses(Path(payload["artifacts"]["rapidapi_cache_misses_csv"])), 1)
            self.assertEqual(code, 0)
            self.assertEqual(mocked.call_count, 1)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                output_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(output_rows), 2)

    def test_company_corpus_metadata_and_current_company_urn_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people_rows(people, [{
                "id": "person-1",
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "full_name": "Jane Example",
                "current_company_urn": "legacy-company-id",
                "rapidapi_response": json.dumps(self.profile()),
            }])
            corpus = Path(tmp) / "companies.jsonl"
            corpus.write_text(
                json.dumps({
                    "rapidapi_company_id": "123",
                    "company_name": "Acme Metadata",
                    "company_linkedin_url": "https://linkedin.com/company/acme",
                }) + "\n",
                encoding="utf-8",
            )
            code, payload = self.invoke([
                "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                "--company-corpus-jsonl", str(corpus),
            ])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["current_company_urn"], "legacy-company-id")
            experiences = json.loads(rows[0]["work_experiences"])
            self.assertEqual(experiences[0]["company_name"], "Acme Metadata")
            self.assertEqual(experiences[0]["company_key"], "linkedin_company:acme")

    def test_rapidapi_linkedin_key_preferred(self):
        with patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "preferred", "RAPIDAPI_KEY": "fallback"}, clear=True):
            self.assertEqual(rapidapi_client.RapidApiClient.resolve_key(), "preferred")

    def test_cache_slug_is_sanitized(self):
        path = enrich_people.profile_cache_path(Path("cache"), "../bad/slug")
        self.assertEqual(path, Path("cache") / "bad_slug.json")

    def test_failed_profile_is_cached_with_last_checked_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(rapidapi_client.RapidApiClient, "http_json", return_value=(200, {"success": False, "message": "not found"}, "")):
                result = rapidapi_client.RapidApiClient("key").fetch_profile("jane-example", "https://www.linkedin.com/in/jane-example", cache_dir=Path(tmp))
            cached = json.loads((Path(tmp) / "jane-example.json").read_text(encoding="utf-8"))
            self.assertIn("last_checked_at", cached)
            self.assertEqual(cached["public_identifier"], "jane-example")
            self.assertFalse(cached["normalized_profile"]["success"])
            self.assertFalse(result["normalized_profile"]["success"])

    def test_recent_failed_cache_skips_retry_until_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps({
                "last_checked_at": enrich_people.now_iso(),
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "raw_response": {},
                "normalized_profile": {"success": False, "error": "not found"},
                "status_code": 404,
                "error": "not found",
            }), encoding="utf-8")
            with patch.object(rapidapi_client.RapidApiClient, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                    "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["paid_call_count"], 0)
            self.assertEqual(payload["counts"]["recent_failure_count"], 1)
            self.assertTrue(Path(payload["artifacts"]["rapidapi_recent_failures_csv"]).exists())
            self.assertEqual(payload["status"], "completed")
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(CsvIO.dict_reader(handle)), [])

    def test_old_failed_cache_retries_after_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            (cache_dir / "jane-example.json").write_text(json.dumps({
                "last_checked_at": "2020-01-01T00:00:00Z",
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "raw_response": {},
                "normalized_profile": {"success": False, "error": "not found"},
            }), encoding="utf-8")
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}):
                    code, payload = self.invoke([
                        "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                        "--profile-cache-dir", str(cache_dir), "--approve-spend",
                    ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["paid_call_count"], 1)
            self.assertEqual(payload["counts"]["recent_failure_count"], 0)

    def test_current_position_respects_explicit_false(self):
        title, company, legacy = profile_transforms.current_position([
            {"title": "Old", "company": "OldCo", "is_current_position": False},
            {"title": "Current", "company": "NewCo", "ends_at": None},
        ])
        self.assertEqual((title, company, legacy), ("Current", "NewCo", ""))

    def test_check_keys_is_rapidapi_only(self):
        with patch.dict(os.environ, {"UNRELATED_PROVIDER_KEY": "x", "RAPIDAPI_KEY": "r"}, clear=True):
            code, payload = self.invoke(["check-keys"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "rapidapi")
        self.assertEqual(set(payload["keys_present"]), {"RAPIDAPI_KEY", "RAPIDAPI_LINKEDIN_KEY"})
        self.assertTrue(payload["keys_present"]["RAPIDAPI_KEY"])


if __name__ == "__main__":
    unittest.main()
