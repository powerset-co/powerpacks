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

from packs.ingestion.schemas.people_schema import generate_person_id
from packs.shared.csv_io import CsvIO

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

    def test_prepare_queue_runs_rapidapi_without_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            ledger = Path(tmp) / "ledger.json"
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}) as mocked:
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger), "--profile-cache-dir", str(cache_dir)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(mocked.call_count, 1)
            state = json.loads(ledger.read_text())
            self.assertEqual(state["queue_count"], 1)
            self.assertEqual(state["paid_call_count"], 1)
            self.assertEqual(state["cache_hit_count"], 0)
            self.assertNotIn("blocked", state)
            for artifact in ("linkedin_enrichment_queue_csv", "rapidapi_cache_misses_csv", "rapidapi_cache_hits_csv"):
                self.assertTrue(Path(state["artifacts"][artifact]).exists())
            with Path(state["artifacts"]["rapidapi_cache_misses_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["cache_status"], "miss")
            self.assertIn("cache_reason", rows[0])

    def test_skips_complete_rows_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, complete=True, rapidapi_response=self.profile())
            ledger = Path(tmp) / "ledger.json"
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger)])
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
            ledger = Path(tmp) / "ledger.json"
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {}, clear=True):
                code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger), "--profile-cache-dir", str(cache_dir)])
            self.assertEqual(code, 1)
            self.assertIn("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY", payload["error"])

    def test_provider_merge_uses_rapidapi_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            ledger = Path(tmp) / "ledger.json"
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "r"}, clear=True):
                with patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}):
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger), "--profile-cache-dir", str(cache_dir)])
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
            ledger = Path(tmp) / "ledger.json"
            with patch.object(enrich_people, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger)])
            self.assertEqual(code, 0)
            state = json.loads(ledger.read_text())
            self.assertEqual(state["paid_call_count"], 0)
            self.assertEqual(state["cache_hit_count"], 1)
            with Path(state["artifacts"]["provider_enriched_csv"]).open(newline="", encoding="utf-8") as handle:
                provider_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(provider_rows[0]["rapidapi_from_cache"], "true")
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                output_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(output_rows[0]["current_company"], "Acme")

    def test_bad_cache_does_not_bypass_rapidapi_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, rapidapi_response={"success": False, "message": "not found"})
            ledger = Path(tmp) / "ledger.json"
            cache_dir = Path(tmp) / "cache"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}) as mocked:
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger), "--profile-cache-dir", str(cache_dir)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(mocked.call_count, 1)
            state = json.loads(ledger.read_text())
            self.assertEqual(state["paid_call_count"], 1)
            self.assertEqual(state["cache_hit_count"], 0)

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
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(Path(tmp) / "ledger.json")])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["id"], generate_person_id("jane-example"))
            people2 = Path(tmp) / "people2.csv"
            self.write_people(people2, rapidapi_response=self.profile(), row_id="existing")
            code, payload = self.invoke(["run", "--input", str(people2), "--output-dir", str(Path(tmp) / "out2"), "--ledger", str(Path(tmp) / "ledger2.json")])
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
            ledger = Path(tmp) / "ledger.json"
            with patch.object(enrich_people, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                    "--ledger", str(ledger), "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            state = json.loads(ledger.read_text())
            self.assertEqual(state["cache_hit_count"], 1)
            self.assertEqual(state["paid_call_count"], 0)
            with Path(state["artifacts"]["provider_enriched_csv"]).open(newline="", encoding="utf-8") as handle:
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
            ledger = Path(tmp) / "ledger.json"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}):
                    code, _payload = self.invoke([
                        "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                        "--ledger", str(ledger), "--profile-cache-dir", str(cache_dir), "--refresh-cache",
                    ])
            self.assertEqual(code, 0)
            state = json.loads(ledger.read_text())
            self.assertEqual(state["cache_hit_count"], 0)
            self.assertEqual(state["paid_call_count"], 1)

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
            ledger = Path(tmp) / "ledger.json"
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                john_profile = self.profile(company_id="456")
                john_profile["full_name"] = "John Example"
                with patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": john_profile, "error": "", "from_cache": False}) as mocked:
                    code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"), "--ledger", str(ledger)])
                self.assertEqual(code, 0)
                state = json.loads(ledger.read_text())
                self.assertEqual(state["queue_count"], 2)
                self.assertEqual(state["cache_hit_count"], 1)
                self.assertEqual(state["paid_call_count"], 1)
                self.assertEqual(enrich_people.count_rapidapi_cache_misses(Path(state["artifacts"]["rapidapi_cache_misses_csv"])), 1)
            self.assertEqual(code, 0)
            self.assertEqual(mocked.call_count, 1)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                output_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(output_rows), 2)

    def test_old_ledger_use_rapidapi_false_fails_only_for_paid_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            ledger = Path(tmp) / "ledger.json"
            run_dir = Path(tmp) / "out" / "enrichment" / "legacy"
            run_dir.mkdir(parents=True)
            ledger.write_text(json.dumps({
                "primitive": "enrich_people",
                "status": "running",
                "run_id": "legacy",
                "run_dir": str(run_dir),
                "input": {"input_csv": str(people), "use_rapidapi": False, "profile_cache_dir": str(Path(tmp) / "cache")},
                "steps": {"prepare_queue": {"status": "completed"}},
                "artifacts": {"rapidapi_cache_hits_csv": str(run_dir / "hits.csv"), "rapidapi_cache_misses_csv": str(run_dir / "misses.csv")},
                "paid_call_count": 1,
            }), encoding="utf-8")
            enrich_people.write_csv(run_dir / "hits.csv", enrich_people.CACHE_COLUMNS, [])
            enrich_people.write_csv(run_dir / "misses.csv", enrich_people.CACHE_COLUMNS, [{
                "id": "person-1",
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "full_name": "Jane Example",
                "cache_status": "miss",
            }])
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                code, payload = self.invoke(["continue", "--ledger", str(ledger)])
            self.assertEqual(code, 1)
            self.assertIn("RapidAPI provider is required", payload["error"])

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
            ledger = Path(tmp) / "ledger.json"
            code, payload = self.invoke([
                "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                "--ledger", str(ledger), "--company-corpus-jsonl", str(corpus),
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
            self.assertEqual(enrich_people.rapidapi_key(), "preferred")

    def test_cache_slug_is_sanitized(self):
        path = enrich_people.profile_cache_path(Path("cache"), "../bad/slug")
        self.assertEqual(path, Path("cache") / "bad_slug.json")

    def test_failed_profile_is_cached_with_last_checked_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(enrich_people, "http_json", return_value=(200, {"success": False, "message": "not found"}, "")):
                result = enrich_people.rapidapi_profile("jane-example", "https://www.linkedin.com/in/jane-example", "key", cache_dir=Path(tmp))
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
            with patch.object(enrich_people, "http_json", side_effect=AssertionError("network called")):
                code, payload = self.invoke([
                    "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                    "--ledger", str(Path(tmp) / "ledger.json"), "--profile-cache-dir", str(cache_dir),
                ])
            self.assertEqual(code, 0)
            state = json.loads(Path(tmp, "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(state["paid_call_count"], 0)
            self.assertEqual(state["recent_failure_count"], 1)
            self.assertTrue(Path(state["artifacts"]["rapidapi_recent_failures_csv"]).exists())
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
                with patch.object(enrich_people, "rapidapi_profile", return_value={"status_code": 200, "data": self.profile(), "error": "", "from_cache": False}):
                    code, payload = self.invoke([
                        "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                        "--ledger", str(Path(tmp) / "ledger.json"), "--profile-cache-dir", str(cache_dir),
                    ])
            self.assertEqual(code, 0)
            state = json.loads(Path(tmp, "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(state["paid_call_count"], 1)
            self.assertEqual(state["recent_failure_count"], 0)

    def test_current_position_respects_explicit_false(self):
        title, company, legacy = enrich_people.current_position([
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
