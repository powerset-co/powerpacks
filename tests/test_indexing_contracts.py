import csv
import json
import re
import unittest
from pathlib import Path

from packs.indexing.lib.artifacts import (
    build_company_corpus,
    build_education_corpus,
    build_location_corpus,
    build_summary_records,
    stable_person_uuid,
)
from packs.indexing.lib.contracts import load_search_contract, normalize_record_for_contract, validate_record
from packs.indexing.lib.identity import company_uuid, person_uuid


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "packs" / "search" / "contracts" / "turbopuffer"


def contract_spec(name):
    return json.loads((CONTRACT_DIR / f"{name}.namespace.json").read_text())


def contract_attributes(name):
    spec = contract_spec(name)
    return {attr["name"] for attr in spec["attributes"]}


def contract_record_fields(name):
    spec = contract_spec(name)
    fields = contract_attributes(name)
    if isinstance(spec.get("vector"), dict):
        fields.add("vector")
    return fields


def first_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                return json.loads(line)
    raise AssertionError(f"empty jsonl: {path}")


def _word_tokenize(text):
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return tokens + [f"{tokens[idx]} {tokens[idx + 1]}" for idx in range(len(tokens) - 1)]


def _company_corpus_to_record(row):
    aliases = row.get("name_aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    customer_type = row.get("customer_type")
    if isinstance(customer_type, list):
        customer_types = customer_type
    elif customer_type:
        customer_types = [customer_type]
    else:
        customer_types = []
    return {
        "id": row.get("company_urn", ""),
        "company_name": row.get("company_name", ""),
        "name_aliases_text": " ".join([row.get("company_name", ""), *aliases]).strip(),
        "semantic_text": row.get("semantic_text", ""),
        "entity_sector_text": row.get("word_text", ""),
        "doc2query_text": row.get("d2q_text", ""),
        "website_domain": row.get("website_domain", ""),
        "linkedin_url": row.get("linkedin_url", ""),
        "description": row.get("description", ""),
        "headcount": row.get("headcount"),
        "funding_total": row.get("funding_total"),
        "funding_stage": row.get("funding_stage"),
        "stage": row.get("stage", ""),
        "city": row.get("city", ""),
        "state": row.get("state", ""),
        "country": row.get("country", ""),
        "metro_area": row.get("metro_area", ""),
        "macro_region": row.get("macro_region", ""),
        "entity_types": row.get("entity_types") or [],
        "sector_types": row.get("sector_types") or [],
        "technology_types": row.get("technology_types") or [],
        "customer_type": customer_types,
        "investor_urns": row.get("investor_urns") or [],
        "accelerators": row.get("accelerators") or [],
        "yc_batches": row.get("yc_batches") or [],
        "founded_year": row.get("founded_year"),
        "last_funding_at": row.get("last_funding_at"),
        "valuation": row.get("valuation"),
        "logo_url": row.get("logo_url", ""),
        "allowed_operator_ids": [],
    }


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
        allowed = contract_record_fields(namespace) | set(extras)
        self.assertTrue(set(record) <= allowed, sorted(set(record) - allowed))

    def test_company_corpus_is_aleph_source_shaped(self):
        record = build_company_corpus(self.people)[0]
        self.assertTrue({"company_urn", "company_name", "name_aliases", "word_text", "char_text", "d2q_text", "doc2query", "semantic_text"} <= set(record))
        self.assertIn("id", record)  # local stable id alias for downstream joins
        self.assertIn("semantic_text", record)
        self.assertEqual(record["canonical_key"], "linkedin_company:secureco")
        self.assertEqual(record["company_urn"], company_uuid("linkedin_company:secureco"))

    def test_education_and_school_records_match_contracts_plus_operational_id(self):
        result = build_education_corpus(self.people)
        self.assert_contract_subset(result["education"][0], "education", {"id", "school_canonical_key"})
        self.assert_contract_subset(result["schools"][0], "schools", {"canonical_key"})
        self.assertIn("id", result["education"][0])
        self.assertEqual(result["education"][0]["person_id"], result["education"][0]["base_id"])

    def test_education_field_of_study_reads_camelcase_linkedin_key(self):
        people = [
            {
                "public_identifier": "ada-example",
                "full_name": "Ada Example",
                "education": [
                    {
                        "schoolName": "Cornell University",
                        "school": "Cornell University",
                        "degree": "Doctor of Philosophy - Ph.D.",
                        "fieldOfStudy": "Psychology",
                        "ends_at": {"year": 2028, "month": 5, "day": 0},
                    }
                ],
            }
        ]
        record = build_education_corpus(people)["education"][0]
        self.assertEqual(record["field_of_study"], "Psychology")
        self.assertEqual(record["school_name"], "Cornell University")
        self.assertEqual(record["end_year"], 2028)

    def test_summary_contract_records_are_pre_embedding_upload_shape(self):
        result = build_summary_records(self.people)
        self.assert_contract_subset(result["summaries"][0], "summaries")
        self.assertEqual(set(result["summaries"][0]), {"id", "person_id", "base_id", "summary", "summary_tokens", "word_tokens", "phrase_tokens", "tech_skills", "allowed_operator_ids"})
        self.assertEqual(result["summaries"][0]["id"], stable_person_uuid(self.people[0]))
        self.assertIn("Python", result["summaries"][0]["summary"])
        self.assertIn("python", result["summaries"][0]["summary_tokens"])
        self.assertIn("python security", result["summaries"][0]["summary_tokens"])
        self.assertIn("text", result["internal_text"][0])

    def test_summary_records_carry_bootstrap_alias_and_token_fields(self):
        record = build_summary_records(self.people)["summaries"][0]
        # person_id and base_id alias the base person id (bootstrap parity).
        self.assertEqual(record["person_id"], record["id"])
        self.assertEqual(record["base_id"], record["id"])
        # word_tokens match the bootstrap shape: raw unigrams + bigrams of the summary.
        self.assertEqual(record["word_tokens"], record["summary_tokens"])
        self.assertIn("python", record["word_tokens"])
        self.assertIn("python security", record["word_tokens"])
        # phrase_tokens are deduped stemmed 1- to 4-grams of the summary.
        self.assertIn("secur", record["phrase_tokens"])  # "security"/"Security" stems to "secur", deduped
        self.assertEqual(record["phrase_tokens"].count("secur"), 1)
        self.assertIn("python secur infrastructur", record["phrase_tokens"])
        self.assertTrue(any(len(phrase.split()) == 4 for phrase in record["phrase_tokens"]))

    def test_location_records_are_local_artifacts_not_turbopuffer_contract_uploads(self):
        record = build_location_corpus(self.people)[0]
        self.assertEqual(set(record), {"id", "city", "state", "country", "location_raw", "metro_area", "macro_region", "person_count"})
        self.assertNotEqual(record["id"], "New York")

    def test_turbopuffer_contracts_accept_aleph_vector_and_optional_attrs(self):
        people_contract = load_search_contract("turbopuffer/people.namespace.json")
        record = {
            "id": "position-1",
            "base_id": "person-1",
            "vector": [0.0] * 1536,
            "position_title": "Engineer",
            "tenure_years": 2.5,
        }
        result = validate_record(record, people_contract)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["extra"], [])

        normalized = normalize_record_for_contract(record, people_contract)
        self.assertIn("vector", normalized)
        self.assertEqual(len(normalized["vector"]), 1536)

    def test_turbopuffer_contracts_reject_unknown_extras_and_bad_vector_dim(self):
        summaries_contract = load_search_contract("turbopuffer/summaries.namespace.json")
        extra = validate_record({"id": "p1", "vector": [0.0] * 1536, "unknown": "nope"}, summaries_contract)
        self.assertFalse(extra["ok"])
        self.assertEqual(extra["extra"], ["unknown"])

        bad_vector = validate_record({"id": "p1", "vector": [0.0]}, summaries_contract)
        self.assertFalse(bad_vector["ok"])
        self.assertIn("vector dimension 1 != 1536", bad_vector["errors"])

    def test_aleph_parity_fields_are_declared(self):
        self.assertIn("tenure_years", contract_attributes("people"))
        self.assertTrue({"summary", "summary_tokens"} <= contract_attributes("summaries"))
        self.assertTrue({"stage", "accelerators", "logo_url"} <= contract_attributes("companies"))
        self.assertIn("education_id", contract_attributes("education"))

    def test_copied_aleph_seed_artifacts_validate_against_contracts(self):
        seed = ROOT / ".powerpacks/aleph-seed/2026-05-08/pipeline_output"
        if not seed.exists():
            self.skipTest("copied Aleph seed artifacts not present")

        people_contract = load_search_contract("turbopuffer/people.namespace.json")
        flattened = first_jsonl(seed / "unified/flattened_people.jsonl")
        role_embedding = first_jsonl(seed / "unified/roles/roles_with_embeddings.jsonl")
        people_record = {
            "id": flattened["id"],
            "vector": role_embedding["dense_embedding"],
            "word_tokens": _word_tokenize(flattened["position"]["title"]),
            "char_tokens": [],
            "d2q_tokens": _word_tokenize(" ".join(role_embedding.get("doc2query") or [])),
            "phrase_tokens": [],
            "position_title": flattened["position"]["title"],
            "seniority_band": role_embedding.get("seniority_band", ""),
            "company_id": flattened.get("company_id", ""),
            "city": flattened.get("city", ""),
            "state": flattened.get("state", ""),
            "country": flattened.get("country", ""),
            "macro_region": flattened.get("macro_region", ""),
            "is_current": flattened.get("is_current", False),
            "total_years_experience": flattened.get("total_years_experience", 0.0),
            "start_date_epoch": 0,
            "end_date_epoch": 0,
            "tenure_years": flattened.get("tenure_years", 0.0),
            "inferred_birth_year": flattened.get("inferred_birth_year", 0),
            "base_id": flattened.get("base_person_id", ""),
            "role_track": role_embedding.get("role_track", ""),
            "metro_areas": flattened.get("metro_areas", []),
            "allowed_operator_ids": [],
            "role_ids": role_embedding.get("role_ids", []),
        }
        self.assertTrue(validate_record(people_record, people_contract)["ok"])

        company_contract = load_search_contract("turbopuffer/companies.namespace.json")
        company_corpus = first_jsonl(seed / "company/companies_corpus_v3.jsonl")
        company_embedding = first_jsonl(seed / "company/company_embeddings_v3.jsonl")
        company_record = _company_corpus_to_record(company_corpus)
        company_record["vector"] = company_embedding["embedding"]
        self.assertTrue(validate_record(company_record, company_contract)["ok"])

        summary_contract = load_search_contract("turbopuffer/summaries.namespace.json")
        summary_embedding = first_jsonl(seed / "unified/summary_embeddings.jsonl")
        summary_text = "summary fixture"
        summary_record = {
            "id": summary_embedding["person_id"],
            "summary": summary_text,
            "vector": summary_embedding["embedding"],
            "summary_tokens": _word_tokenize(summary_text),
            "tech_skills": [],
            "allowed_operator_ids": [],
        }
        self.assertTrue(validate_record(summary_record, summary_contract)["ok"])

        education_contract = load_search_contract("turbopuffer/education.namespace.json")
        education_row = first_jsonl(seed / "education/people_education.jsonl")
        education_record = {"id": "education-edge", "canonical_education_id": education_row["education_id"], "allowed_operator_ids": [], **education_row}
        self.assertTrue(validate_record(education_record, education_contract)["ok"])

    def test_stable_person_uuid_ignores_legacy_input_uuid(self):
        row = {**self.people[0], "id": "00000000-0000-4000-8000-000000000000"}
        self.assertEqual(stable_person_uuid(row), person_uuid("linkedin:ada-example"))
        self.assertNotEqual(stable_person_uuid(row), row["id"])


if __name__ == "__main__":
    unittest.main()
