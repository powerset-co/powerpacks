import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from packs.indexing.lib.contracts import load_search_contract, validate_record
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import _company_corpus_to_record
from packs.indexing.primitives.enrich_companies_checkpointed import enrich_companies_checkpointed as stage


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

    def test_provider_output_missing_schema_fields_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "input.jsonl"
            artifact_path = root / "companies_corpus_v3.jsonl"
            self._write_jsonl(input_path, [{"company_urn": "urn:li:company:1", "company_name": "Acme AI"}])
            self._write_jsonl(artifact_path, [{"company_name": "Acme AI", "entity_types": ["venture_backed_startup"]}])

            with self.assertRaises(SystemExit) as ctx:
                stage.run(Namespace(
                    input=str(input_path),
                    output=str(root / "out.jsonl"),
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
            self.assertIn("missing required fields", str(ctx.exception))

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

            with mock.patch.object(stage, "call_openai_company_classifier", side_effect=fake_classifier) as mocked:
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


if __name__ == "__main__":
    unittest.main()
