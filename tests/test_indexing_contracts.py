import json
import unittest
from pathlib import Path

from packs.indexing.lib.artifacts import (
    build_company_corpus,
    build_education_corpus,
    build_location_corpus,
    build_summary_records,
    stable_person_uuid,
)
from packs.indexing.lib.identity import person_uuid


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "packs" / "search" / "contracts" / "turbopuffer"


def contract_attributes(name):
    spec = json.loads((CONTRACT_DIR / f"{name}.namespace.json").read_text())
    return {attr["name"] for attr in spec["attributes"]}


class IndexingContractTest(unittest.TestCase):
    def setUp(self):
        self.people = [
            {
                "public_identifier": "ada-example",
                "full_name": "Ada Example",
                "headline": "Security engineer",
                "summary": "Python security infrastructure.",
                "city": "New York",
                "state": "NY",
                "country": "United States",
                "work_experiences": [{"title": "Engineer", "company_name": "SecureCo", "company_public_identifier": "secureco"}],
                "education": [{"school_name": "MIT", "degree": "MS", "field_of_study": "EECS", "end_year": "2018"}],
            }
        ]

    def assert_contract_subset(self, record, namespace, extras=frozenset()):
        allowed = contract_attributes(namespace) | set(extras)
        self.assertTrue(set(record) <= allowed, sorted(set(record) - allowed))

    def test_company_records_are_contract_shaped_with_local_metadata_extras(self):
        record = build_company_corpus(self.people)[0]
        self.assert_contract_subset(record, "companies", {"rapidapi_company_id", "company_public_identifier", "company_key", "canonical_key", "person_count"})
        self.assertIn("id", record)
        self.assertIn("semantic_text", record)

    def test_education_and_school_records_match_contracts_plus_operational_id(self):
        result = build_education_corpus(self.people)
        self.assert_contract_subset(result["education"][0], "education", {"id", "school_canonical_key"})
        self.assert_contract_subset(result["schools"][0], "schools", {"canonical_key"})
        self.assertIn("id", result["education"][0])
        self.assertEqual(result["education"][0]["person_id"], result["education"][0]["base_id"])

    def test_summary_contract_records_do_not_leak_internal_text(self):
        result = build_summary_records(self.people)
        self.assert_contract_subset(result["summaries"][0], "summaries")
        self.assertEqual(set(result["summaries"][0]), {"id", "tech_skills", "allowed_operator_ids"})
        self.assertEqual(result["summaries"][0]["id"], stable_person_uuid(self.people[0]))
        self.assertIn("text", result["internal_text"][0])

    def test_location_records_are_local_artifacts_not_turbopuffer_contract_uploads(self):
        record = build_location_corpus(self.people)[0]
        self.assertEqual(set(record), {"id", "city", "state", "country", "location_raw", "metro_area", "macro_region", "person_count"})
        self.assertNotEqual(record["id"], "New York")

    def test_stable_person_uuid_ignores_legacy_input_uuid(self):
        row = {**self.people[0], "id": "00000000-0000-4000-8000-000000000000"}
        self.assertEqual(stable_person_uuid(row), person_uuid("linkedin:ada-example"))
        self.assertNotEqual(stable_person_uuid(row), row["id"])


if __name__ == "__main__":
    unittest.main()
