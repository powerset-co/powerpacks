import tempfile
import unittest
from pathlib import Path

from packs.indexing.lib.contracts import load_search_contract, normalize_record_for_contract
from packs.indexing.lib.io import read_jsonl, write_jsonl
from packs.indexing.lib.people import build_roles
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import (
    _company_corpus_to_record,
    _denormalize_company_onto_position,
    _funding_stage_label,
    paths,
    step_people,
)

CORPUS_ROW = {
    "company_urn": "11111111-1111-5111-8111-111111111111",
    "company_name": "Acme AI",
    "name_aliases": ["Acme AI", "Acme AI Inc."],
    "description": "AI infrastructure for developers.",
    "website_domain": "acme.ai",
    "linkedin_url": "https://www.linkedin.com/company/acme-ai",
    "headcount": 42,
    "funding_total": 1000000.0,
    "funding_stage": "SEED",
    "stage": "",
    "entity_types": ["venture_backed_startup"],
    "sector_types": ["ai_ml", "infra_devtools"],
    "word_text": "venture backed startup ai ml infra devtools",
    "d2q_text": "ai infrastructure company",
    "doc2query": ["ai infrastructure company", "acme ai developer tools"],
    "semantic_text": "Acme AI builds AI infrastructure for developers.",
    "confidence_score": 0.9,
}


class CompanyCorpusToRecordTests(unittest.TestCase):
    def test_record_emits_bootstrap_keys(self) -> None:
        record = _company_corpus_to_record(CORPUS_ROW)
        self.assertEqual(record["id"], CORPUS_ROW["company_urn"])
        self.assertEqual(record["company_urn"], CORPUS_ROW["company_urn"])
        self.assertEqual(record["aliases"], ["Acme AI", "Acme AI Inc."])
        self.assertEqual(record["doc2query"], ["ai infrastructure company", "acme ai developer tools"])
        self.assertEqual(record["word_text"], "venture backed startup ai ml infra devtools")
        self.assertEqual(record["word_text"], record["entity_sector_text"])

    def test_contract_normalization_keeps_bootstrap_keys(self) -> None:
        contract = load_search_contract("turbopuffer/companies.namespace.json")
        normalized = normalize_record_for_contract(_company_corpus_to_record(CORPUS_ROW), contract)
        self.assertEqual(normalized["company_urn"], CORPUS_ROW["company_urn"])
        self.assertEqual(normalized["aliases"], ["Acme AI", "Acme AI Inc."])
        self.assertEqual(normalized["doc2query"], ["ai infrastructure company", "acme ai developer tools"])
        self.assertEqual(normalized["word_text"], "venture backed startup ai ml infra devtools")

    def test_missing_corpus_fields_default_to_empty(self) -> None:
        record = _company_corpus_to_record({"company_urn": "urn-1", "company_name": "Bare Co"})
        self.assertEqual(record["company_urn"], "urn-1")
        self.assertEqual(record["aliases"], [])
        self.assertEqual(record["doc2query"], [])
        self.assertEqual(record["word_text"], "")


class DenormalizeCompanyOntoPositionTests(unittest.TestCase):
    def _company_record(self) -> dict:
        return {
            "id": "11111111-1111-5111-8111-111111111111",
            "description": "AI infrastructure for developers.",
            "website_domain": "acme.ai",
            "headcount": 42.0,
            "funding_total": 1000000.0,
            "funding_stage": 99,
            "stage": "",
            "sector_types": ["ai_ml"],
            "entity_types": ["venture_backed_startup"],
        }

    def test_fills_empty_company_fields(self) -> None:
        record = {
            "company_description": "",
            "company_domain": "",
            "company_headcount": 0,
            "company_funding_total": 0.0,
            "company_stage": "",
            "company_sector_types": [],
            "company_entity_types": [],
        }
        _denormalize_company_onto_position(record, self._company_record())
        self.assertEqual(record["company_description"], "AI infrastructure for developers.")
        self.assertEqual(record["company_domain"], "acme.ai")
        self.assertEqual(record["company_headcount"], 42)
        self.assertEqual(record["company_funding_total"], 1000000.0)
        self.assertEqual(record["company_stage"], "EXITED")
        self.assertEqual(record["company_sector_types"], ["ai_ml"])
        self.assertEqual(record["company_entity_types"], ["venture_backed_startup"])

    def test_does_not_overwrite_populated_fields(self) -> None:
        record = {
            "company_description": "from raw experience",
            "company_domain": "raw.example",
            "company_headcount": 7,
            "company_funding_total": 5.0,
            "company_stage": "SERIES_A",
            "company_sector_types": ["fintech"],
            "company_entity_types": ["bank"],
        }
        _denormalize_company_onto_position(record, self._company_record())
        self.assertEqual(record["company_description"], "from raw experience")
        self.assertEqual(record["company_domain"], "raw.example")
        self.assertEqual(record["company_headcount"], 7)
        self.assertEqual(record["company_funding_total"], 5.0)
        self.assertEqual(record["company_stage"], "SERIES_A")
        self.assertEqual(record["company_sector_types"], ["fintech"])
        self.assertEqual(record["company_entity_types"], ["bank"])

    def test_funding_stage_label_roundtrip(self) -> None:
        self.assertEqual(_funding_stage_label(99), "EXITED")
        self.assertEqual(_funding_stage_label("SEED"), "SEED")
        self.assertEqual(_funding_stage_label(0), "VENTURE_UNKNOWN")
        self.assertEqual(_funding_stage_label(None), "VENTURE_UNKNOWN")
        self.assertEqual(_funding_stage_label("OUT_OF_BUSINESS"), "VENTURE_UNKNOWN")


class StepPeopleCompanyJoinTests(unittest.TestCase):
    def test_step_people_joins_companies_records_onto_positions(self) -> None:
        person = {
            "id": "person-1",
            "full_name": "Pat Example",
            "work_experiences": [
                {"title": "Software Engineer", "company_name": "Acme AI", "is_current": True, "title_hash": "th-1"},
            ],
        }
        expected_company_id = build_roles([person])[0]["company_id"]
        self.assertTrue(expected_company_id)
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            ps = paths(run_dir)
            write_jsonl(ps["flattened"], [person])
            write_jsonl(ps["companies_records"], [{
                "id": expected_company_id,
                "company_urn": expected_company_id,
                "company_name": "Acme AI",
                "description": "AI infrastructure for developers.",
                "website_domain": "acme.ai",
                "headcount": 42,
                "funding_total": 1000000.0,
                "funding_stage": 2,
                "stage": "",
                "sector_types": ["ai_ml"],
                "entity_types": ["venture_backed_startup"],
            }])
            ledger = {"run_dir": str(run_dir), "default_operator_id": "operator:test"}
            artifacts, stats = step_people(ledger, ps)
            self.assertEqual(stats["people_records"], 1)
            record = read_jsonl(Path(artifacts["people"]))[0]
            self.assertEqual(record["company_id"], expected_company_id)
            self.assertEqual(record["company_description"], "AI infrastructure for developers.")
            self.assertEqual(record["company_domain"], "acme.ai")
            self.assertEqual(record["company_headcount"], 42)
            self.assertEqual(record["company_funding_total"], 1000000.0)
            self.assertEqual(record["company_stage"], "SEED")
            self.assertEqual(record["company_sector_types"], ["ai_ml"])
            self.assertEqual(record["company_entity_types"], ["venture_backed_startup"])

    def test_step_people_leaves_unjoined_positions_empty(self) -> None:
        person = {
            "id": "person-2",
            "full_name": "Lee Example",
            "work_experiences": [
                {"title": "Analyst", "company_name": "Unknown Co", "is_current": False, "title_hash": "th-2"},
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            ps = paths(run_dir)
            write_jsonl(ps["flattened"], [person])
            write_jsonl(ps["companies_records"], [])
            ledger = {"run_dir": str(run_dir), "default_operator_id": "operator:test"}
            artifacts, _stats = step_people(ledger, ps)
            record = read_jsonl(Path(artifacts["people"]))[0]
            self.assertEqual(record["company_description"], "")
            self.assertEqual(record["company_stage"], "")
            self.assertEqual(record["company_sector_types"], [])
            self.assertEqual(record["investor_names"], [])


if __name__ == "__main__":
    unittest.main()
