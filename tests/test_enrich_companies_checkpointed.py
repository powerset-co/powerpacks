import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from packs.indexing.lib.contracts import load_search_contract, validate_record
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import _company_corpus_to_record
from packs.indexing.primitives.enrich_companies_checkpointed import enrich_companies_checkpointed as stage
from packs.indexing.primitives.enrich_companies_checkpointed import rapidapi_company


class EnrichCompaniesCheckpointedTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")

    def test_artifact_provider_preserves_classification_fields_and_validates_tpuf_record(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            artifact_path = root / "companies_corpus_v3.jsonl"
            output_path = root / "out.jsonl"
            self._write_jsonl(input_path, [{"company_urn": "urn:li:company:1", "company_name": "Acme AI", "description": "AI infrastructure", "allowed_operator_ids": ["operator:test"]}])
            self._write_jsonl(artifact_path, [{
                "company_urn": "seed-urn",
                "company_name": "Acme AI",
                "entity_types": ["venture_backed_startup"],
                "sector_types": ["ai_ml", "infra_devtools"],
                "technology_types": ["ai_ml", "developer_tools"],
                "customer_type": "Business (B2B)",
                "funding_stage": "SEED",
                "company_type": "STARTUP",
                "ownership_status": "PRIVATE",
                "stage": "Seed",
                "accelerators": ["YC"],
                "yc_batches": ["W24"],
                "doc2query": ["ai infrastructure company"],
                "d2q_text": "ai infrastructure company",
                "word_text": "venture backed startup ai infrastructure",
                "semantic_text": "Acme AI builds AI infrastructure for developers.",
                "confidence_score": 0.92,
                "logo_url": "https://example.com/logo.png",
            }])

            manifest = stage.run(Namespace(
                input=str(input_path),
                output=str(output_path),
                output_dir=str(root / "checkpoint"),
                checkpoint_every=100,
                provider="artifact",
                artifact_path=str(artifact_path),
                artifact_missing_policy="error",
                dry_run=False,
                estimate=False,
                allow_paid=False,
                model=None,
                api_key=None,
                base_url=None,
                force=False,
                stop_after_chunks=None,
            ))

            self.assertEqual(manifest["provider"], "artifact")
            row = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(row["company_urn"], "urn:li:company:1")
            self.assertEqual(row["sector_types"], ["ai_ml", "infra_devtools"])
            self.assertEqual(row["funding_stage"], "SEED")
            self.assertEqual(row["accelerators"], ["YC"])
            self.assertEqual(row["confidence_score"], 0.92)

            record = _company_corpus_to_record(row)
            record["vector"] = [0.01] * 1536
            result = validate_record(record, load_search_contract("turbopuffer/companies.namespace.json"))
            self.assertTrue(result["ok"], result)

    def test_provider_output_missing_schema_fields_uses_local_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            artifact_path = root / "companies_corpus_v3.jsonl"
            self._write_jsonl(input_path, [{"company_urn": "urn:li:company:1", "company_name": "Acme AI"}])
            self._write_jsonl(artifact_path, [{"company_name": "Acme AI", "entity_types": ["venture_backed_startup"]}])

            output_path = root / "out.jsonl"
            manifest = stage.run(Namespace(
                input=str(input_path),
                output=str(output_path),
                output_dir=str(root / "checkpoint"),
                checkpoint_every=100,
                provider="artifact",
                artifact_path=str(artifact_path),
                artifact_missing_policy="error",
                dry_run=False,
                estimate=False,
                allow_paid=False,
                model=None,
                api_key=None,
                base_url=None,
                force=False,
                stop_after_chunks=None,
            ))
            self.assertEqual(manifest["status"], "completed")
            row = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(row["entity_types"], ["venture_backed_startup"])
            self.assertEqual(row["technology_types"], [])
            self.assertEqual(row["accelerators"], [])

    def test_openai_dry_run_does_not_write_or_require_paid_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            output_path = root / "out.jsonl"
            self._write_jsonl(input_path, [{"company_urn": "urn:li:company:1", "company_name": "Acme AI"}])

            result = stage.run(Namespace(
                input=str(input_path),
                output=str(output_path),
                output_dir=str(root / "checkpoint"),
                checkpoint_every=100,
                provider="openai",
                artifact_path=None,
                artifact_missing_policy="error",
                dry_run=True,
                estimate=False,
                allow_paid=False,
                model=None,
                api_key=None,
                base_url=None,
                force=False,
                stop_after_chunks=None,
            ))
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse(output_path.exists())
            self.assertFalse((root / "checkpoint").exists())

    def test_artifact_provider_can_pay_for_missing_companies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            artifact_path = root / "companies_corpus_v3.jsonl"
            output_path = root / "out.jsonl"
            self._write_jsonl(input_path, [
                {"company_urn": "urn:li:company:1", "company_name": "Acme AI"},
                {"company_urn": "urn:li:company:2", "company_name": "Missing Co"},
            ])
            self._write_jsonl(artifact_path, [{
                "company_urn": "seed-urn",
                "company_name": "Acme AI",
                "entity_types": ["venture_backed_startup"],
                "sector_types": ["ai_ml"],
                "technology_types": ["ai_ml"],
                "customer_type": "Business (B2B)",
                "funding_stage": "SEED",
                "company_type": "STARTUP",
                "ownership_status": "PRIVATE",
                "stage": "Seed",
                "accelerators": [],
                "yc_batches": [],
                "doc2query": ["ai company"],
                "d2q_text": "ai company",
                "word_text": "ai",
                "semantic_text": "Acme AI",
                "confidence_score": 0.9,
            }])

            def fake_classifier(_row, **_kwargs):
                return {
                    "entity_types": ["venture_backed_startup"],
                    "sector_types": ["saas"],
                    "technology_types": [],
                    "customer_type": "Business (B2B)",
                    "funding_stage": "SEED",
                    "company_type": "STARTUP",
                    "ownership_status": "PRIVATE",
                    "stage": "Seed",
                    "accelerators": [],
                    "yc_batches": [],
                    "doc2query": ["missing company"],
                    "d2q_text": "missing company",
                    "word_text": "missing",
                    "semantic_text": "Missing Co",
                    "confidence_score": 0.8,
                }

            def fake_classifier_batch(rows, **_kwargs):
                return [fake_classifier(row) for row in rows]

            with mock.patch.object(stage, "call_openai_company_classifiers", side_effect=fake_classifier_batch) as mocked:
                manifest = stage.run(Namespace(
                    input=str(input_path),
                    output=str(output_path),
                    output_dir=str(root / "checkpoint"),
                    checkpoint_every=100,
                    provider="artifact",
                    artifact_path=str(artifact_path),
                    artifact_missing_policy="error",
                    dry_run=False,
                    estimate=False,
                    allow_paid=True,
                    model=None,
                    api_key="test",
                    base_url=None,
                    force=False,
                    stop_after_chunks=None,
                ))
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(manifest["counts"]["artifact_hits"], 1)
            self.assertEqual(manifest["counts"]["artifact_misses"], 1)
            self.assertEqual(manifest["counts"]["paid_calls"], 1)

    def test_normalize_website_domain_strips_scheme_www_and_path(self) -> None:
        self.assertEqual(stage.normalize_website_domain("https://www.Acme.com/about?utm=1"), "acme.com")
        self.assertEqual(stage.normalize_website_domain("http://acme.io"), "acme.io")
        self.assertEqual(stage.normalize_website_domain("www.acme.co.uk/team"), "acme.co.uk")
        self.assertEqual(stage.normalize_website_domain("acme.dev"), "acme.dev")
        self.assertEqual(stage.normalize_website_domain("https://acme.com:8443/#top"), "acme.com")
        self.assertEqual(stage.normalize_website_domain(""), "")
        self.assertEqual(stage.normalize_website_domain(None), "")

    def test_apply_rapidapi_context_backfills_without_overwriting(self) -> None:
        record = {"headcount": None, "founded_year": "", "website_domain": ""}
        context = {"headcount": 42, "founded_year": 2019, "website": "https://www.acme.ai/about"}
        stage.apply_rapidapi_context(record, context)
        self.assertEqual(record["headcount"], 42)
        self.assertEqual(record["founded_year"], 2019)
        self.assertEqual(record["website_domain"], "acme.ai")

        existing = {"headcount": 100, "founded_year": 1999, "website_domain": "existing.com"}
        stage.apply_rapidapi_context(existing, context)
        self.assertEqual(existing["headcount"], 100)
        self.assertEqual(existing["founded_year"], 1999)
        self.assertEqual(existing["website_domain"], "existing.com")

        untouched = {"headcount": 0, "founded_year": 0, "website_domain": ""}
        stage.apply_rapidapi_context(untouched, {})
        self.assertEqual(untouched, {"headcount": 0, "founded_year": 0, "website_domain": ""})

    def test_artifact_path_backfills_rapidapi_context_from_disk_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            artifact_path = root / "companies_corpus_v3.jsonl"
            output_path = root / "out.jsonl"
            cache_dir = root / "rapidapi-cache"
            cache_dir.mkdir()
            self._write_jsonl(input_path, [
                {"company_urn": "urn:li:company:1", "company_name": "Acme AI", "rapidapi_company_id": "12345"},
                {"company_urn": "urn:li:company:2", "company_name": "Uncached Co", "rapidapi_company_id": "99999"},
            ])
            base_artifact = {
                "entity_types": ["venture_backed_startup"],
                "sector_types": ["ai_ml"],
                "doc2query": ["ai company"],
                "d2q_text": "ai company",
                "word_text": "ai",
                "confidence_score": 0.9,
            }
            self._write_jsonl(artifact_path, [
                {**base_artifact, "company_name": "Acme AI", "semantic_text": "Acme AI"},
                {**base_artifact, "company_name": "Uncached Co", "semantic_text": "Uncached Co"},
            ])
            # Warm disk cache for one company only; the other is a miss and must be skipped.
            (cache_dir / "12345.json").write_text(json.dumps({
                "data": {
                    "staffCount": 42,
                    "founded": {"year": 2019},
                    "website": "https://www.acme.ai/about?utm=1",
                    "description": "AI infrastructure",
                }
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "test-key", "POWERPACKS_RAPIDAPI_COMPANY_CACHE": str(cache_dir)}), \
                    mock.patch.object(rapidapi_company, "fetch_company_details", side_effect=AssertionError("network fetch attempted")), \
                    mock.patch.object(rapidapi_company, "fetch_company_details_batch", side_effect=AssertionError("network batch fetch attempted")):
                manifest = stage.run(Namespace(
                    input=str(input_path),
                    output=str(output_path),
                    output_dir=str(root / "checkpoint"),
                    checkpoint_every=100,
                    provider="artifact",
                    artifact_path=str(artifact_path),
                    artifact_missing_policy="error",
                    dry_run=False,
                    estimate=False,
                    allow_paid=False,
                    model=None,
                    api_key=None,
                    base_url=None,
                    force=False,
                    stop_after_chunks=None,
                ))
            self.assertEqual(manifest["status"], "completed")
            rows = {row["company_name"]: row for line in output_path.read_text(encoding="utf-8").splitlines() for row in [json.loads(line)]}
            cached_row = rows["Acme AI"]
            self.assertEqual(cached_row["headcount"], 42)
            self.assertEqual(cached_row["founded_year"], 2019)
            self.assertEqual(cached_row["website_domain"], "acme.ai")
            uncached_row = rows["Uncached Co"]
            self.assertFalse(uncached_row.get("headcount"))
            self.assertFalse(uncached_row.get("founded_year"))

    def test_openai_paid_path_persists_rapidapi_context_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            output_path = root / "out.jsonl"
            self._write_jsonl(input_path, [
                {"company_urn": "urn:li:company:1", "company_name": "Acme AI", "rapidapi_company_id": "12345"},
            ])

            def fake_classifier_batch(rows, **_kwargs):
                return [{
                    "entity_types": ["venture_backed_startup"],
                    "sector_types": ["ai_ml"],
                    "technology_types": [],
                    "customer_type": "Business (B2B)",
                    "funding_stage": "SEED",
                    "company_type": "STARTUP",
                    "ownership_status": "PRIVATE",
                    "stage": "Seed",
                    "accelerators": [],
                    "yc_batches": [],
                    "doc2query": ["ai company"],
                    "d2q_text": "ai company",
                    "word_text": "ai",
                    "semantic_text": "Acme AI",
                    "confidence_score": 0.9,
                } for _ in rows]

            fake_responses = {
                "12345": {
                    "data": {
                        "staffCount": 42,
                        "founded": {"year": 2019},
                        "website": "https://www.acme.ai/about?utm=1",
                        "description": "AI infrastructure",
                    }
                }
            }

            with mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "test-key"}), \
                    mock.patch.object(rapidapi_company, "fetch_company_details_batch", return_value=fake_responses) as fetched, \
                    mock.patch.object(stage, "call_openai_company_classifiers", side_effect=fake_classifier_batch):
                manifest = stage.run(Namespace(
                    input=str(input_path),
                    output=str(output_path),
                    output_dir=str(root / "checkpoint"),
                    checkpoint_every=100,
                    provider="openai",
                    artifact_path=None,
                    artifact_missing_policy="error",
                    dry_run=False,
                    estimate=False,
                    allow_paid=True,
                    model=None,
                    api_key="test",
                    base_url=None,
                    force=False,
                    stop_after_chunks=None,
                ))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(fetched.call_count, 1)
            row = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(row["headcount"], 42)
            self.assertEqual(row["founded_year"], 2019)
            self.assertEqual(row["website_domain"], "acme.ai")


if __name__ == "__main__":
    unittest.main()
