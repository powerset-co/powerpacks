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
from packs.ingestion.schemas.people_schema import generate_person_id, normalize_people_row
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
            # The TTL-suppressed row SURVIVES, annotated. It used to be written
            # nowhere at all, which made a rate-limited fetch byte-identical to
            # "never attempted" and silently deleted the contact.
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "jane-example")
            self.assertEqual(rows[0]["enrichment_status"], "failed")
            self.assertIn("not found", rows[0]["enrichment_error"])
            self.assertEqual(payload["counts"]["failed_rows"], 1)
            self.assertEqual(payload["counts"]["enriched_rows"], 0)

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

    # ---- failure is annotated, never deleted -------------------------------

    def test_enriched_rows_are_stamped_enriched(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people, rapidapi_response=self.profile())
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["enrichment_status"], "enriched")
            self.assertEqual(rows[0]["enrichment_error"], "")
            self.assertEqual(payload["counts"]["enriched_rows"], 1)
            self.assertEqual(payload["counts"]["failed_rows"], 0)

    def test_failed_fetch_keeps_row_with_status_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            cache_dir = Path(tmp) / "cache"
            failed = {
                "status_code": 429,
                "data": None,
                "error": "rate limit exceeded",
                "from_cache": False,
                "normalized_profile": {"success": False, "error": "rate limit exceeded"},
                "attempts": 3,
            }
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "fetch_profile", return_value=failed):
                    code, payload = self.invoke([
                        "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                        "--profile-cache-dir", str(cache_dir), "--approve-spend",
                    ])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "jane-example")
            self.assertEqual(rows[0]["full_name"], "Jane Example")
            self.assertEqual(rows[0]["enrichment_status"], "failed")
            self.assertIn("rate limit exceeded", rows[0]["enrichment_error"])
            self.assertEqual(payload["counts"]["failed_rows"], 1)

    def test_row_without_linkedin_is_stamped_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people_rows(people, [
                {"id": "casey", "full_name": "Casey Delta", "primary_email": "casey@example.com"},
            ])
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["enrichment_status"], "skipped")
            self.assertEqual(rows[0]["enrichment_error"], "")
            self.assertEqual(payload["counts"]["skipped_rows"], 1)

    def test_reads_people_csv_written_before_the_new_columns(self):
        """An existing people.csv predating enrichment_status/-_error has neither
        column in its header. Reading it must not crash and must not invent a
        status: DictReader simply omits the keys, normalize_people_row fills "",
        and the run re-stamps the outcome from scratch."""
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            legacy_fields = [c for c in enrich_people.PEOPLE_SCHEMA_COLUMNS
                             if c not in {"enrichment_status", "enrichment_error"}]
            self.assertNotIn("enrichment_status", legacy_fields)
            row = {col: "" for col in legacy_fields}
            row.update({
                "id": "person-1",
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "full_name": "Jane Example",
                "rapidapi_response": json.dumps(self.profile()),
            })
            with people.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=legacy_fields)
                writer.writeheader()
                writer.writerow(row)
            # The legacy row really is missing the keys, not merely blank.
            raw_rows = CsvIO.read_dict_rows(people)
            self.assertNotIn("enrichment_status", raw_rows[0])
            self.assertNotIn("enrichment_error", raw_rows[0])
            # Missing column -> "" through the shared normalizer, no KeyError.
            self.assertEqual(normalize_people_row(raw_rows[0])["enrichment_status"], "")

            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["full_name"], "Jane Example")
            self.assertEqual(rows[0]["enrichment_status"], "enriched")

    def test_stale_status_from_a_previous_run_is_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people_rows(people, [{
                "id": "person-1",
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example",
                "full_name": "Jane Example",
                "rapidapi_response": json.dumps(self.profile()),
                "enrichment_status": "failed",
                "enrichment_error": "stale reason from a previous run",
            }])
            code, payload = self.invoke(["run", "--input", str(people), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["enrichment_status"], "enriched")
            self.assertEqual(rows[0]["enrichment_error"], "")

    # ---- only PERMANENT failures are cached --------------------------------

    def fetch_with_http(self, tmp: Path, response, *, refresh_cache=False):
        with patch.object(rapidapi_client.RapidApiClient, "http_json", return_value=response):
            return rapidapi_client.RapidApiClient("key", retry_attempts=1).fetch_profile(
                "jane-example", "https://www.linkedin.com/in/jane-example",
                cache_dir=tmp, refresh_cache=refresh_cache,
            )

    def test_permanent_http_failure_is_cached(self):
        for status in sorted(rapidapi_client.PERMANENT_FAILURE_STATUS_CODES):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                self.fetch_with_http(Path(tmp), (status, None, "profile not found"))
                cached_path = Path(tmp) / "jane-example.json"
                self.assertTrue(cached_path.exists(), f"status {status} should be cached")
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
                self.assertEqual(cached["status_code"], status)
                self.assertFalse(cached["normalized_profile"]["success"])

    def test_transient_failures_are_not_cached(self):
        # 0 covers a network error AND a 200 whose body fails to parse as JSON —
        # http_json funnels both into status 0.
        for status in (0, 429, 500, 502, 503, 504):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                self.fetch_with_http(Path(tmp), (status, None, "transient"))
                self.assertFalse(
                    (Path(tmp) / "jane-example.json").exists(),
                    f"status {status} is transient and must not be cached: caching it "
                    f"suppresses the retry for the whole failure TTL",
                )

    def test_transient_failure_leaves_the_row_retryable_next_run(self):
        """The end of the rate-limit-storm bug: a 429 must not classify as a
        recent_failure on the next run — the person is still a cache MISS."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            self.fetch_with_http(cache_dir, (429, None, "rate limited"))
            status, reason, _path, _failure = profile_cache.classify_rapidapi_cache_status(
                {"public_identifier": "jane-example", "linkedin_url": "https://www.linkedin.com/in/jane-example"},
                cache_dir, False, 24.0, None,
            )
            self.assertEqual(status, "miss")
            self.assertNotEqual(reason, "recent provider failure")

    def test_provider_success_false_is_a_permanent_failure(self):
        self.assertTrue(rapidapi_client.RapidApiClient.is_permanent_failure(200, {"success": False}))
        self.assertFalse(rapidapi_client.RapidApiClient.is_permanent_failure(200, {"success": True}))
        self.assertFalse(rapidapi_client.RapidApiClient.is_permanent_failure(429, {"success": False}))
        self.assertFalse(rapidapi_client.RapidApiClient.is_permanent_failure(0, {"success": False}))

    def test_failure_never_overwrites_an_existing_good_cache_entry(self):
        """--refresh-cache skips the cache READ, so without a guard a transient
        error during a refresh would destroy an already-paid-for profile."""
        for status in (0, 429, 503, 404):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                cache_path = Path(tmp) / "jane-example.json"
                cache_path.write_text(json.dumps(self.cache_entry()), encoding="utf-8")
                before = cache_path.read_text(encoding="utf-8")
                self.fetch_with_http(Path(tmp), (status, None, "boom"), refresh_cache=True)
                self.assertEqual(cache_path.read_text(encoding="utf-8"), before,
                                 f"status {status} clobbered a paid-for cache entry")
                self.assertIsNotNone(profile_cache.read_usable_cached_profile(cache_path))

    def test_successful_refresh_still_overwrites_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "jane-example.json"
            cache_path.write_text(json.dumps(self.cache_entry()), encoding="utf-8")
            fresh = self.profile()
            fresh["headline"] = "CTO at Acme"
            self.fetch_with_http(Path(tmp), (200, fresh, ""), refresh_cache=True)
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cached["raw_response"]["headline"], "CTO at Acme")

    # ---- spend gate --------------------------------------------------------

    def test_needs_approval_quotes_a_credit_range_not_just_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            self.write_people(people)
            with patch.dict(os.environ, {"RAPIDAPI_KEY": "r"}, clear=True):
                with patch.object(rapidapi_client.RapidApiClient, "http_json", side_effect=AssertionError("network called")):
                    code, payload = self.invoke([
                        "run", "--input", str(people), "--output-dir", str(Path(tmp) / "out"),
                        "--profile-cache-dir", str(Path(tmp) / "cache"),
                    ])
            self.assertEqual(code, enrich_people.NEEDS_APPROVAL_CODE)
            gate = payload["needs_approval"]
            attempts = rapidapi_client.DEFAULT_RAPIDAPI_RETRY_ATTEMPTS
            self.assertEqual(gate["estimated_credits"], 1)
            self.assertTrue(gate["estimated_credits_is_floor"])
            self.assertEqual(gate["estimated_credits_max"], attempts)
            self.assertEqual(gate["retry_attempts"], attempts)
            self.assertIn(str(attempts), gate["message"])

    def test_check_keys_is_rapidapi_only(self):
        with patch.dict(os.environ, {"UNRELATED_PROVIDER_KEY": "x", "RAPIDAPI_KEY": "r"}, clear=True):
            code, payload = self.invoke(["check-keys"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "rapidapi")
        self.assertEqual(set(payload["keys_present"]), {"RAPIDAPI_KEY", "RAPIDAPI_LINKEDIN_KEY"})
        self.assertTrue(payload["keys_present"]["RAPIDAPI_KEY"])


if __name__ == "__main__":
    unittest.main()
