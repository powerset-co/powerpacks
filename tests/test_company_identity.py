from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.schemas.company_identity import (
    build_company_identity_lookup,
    extract_company_public_identifier,
    extract_rapidapi_company_id,
    generate_title_hash,
    normalize_company_linkedin_url,
    rapidapi_experience_to_powerpacks,
    resolve_company_identity,
)


class CompanyIdentityTests(unittest.TestCase):
    def test_company_url_normalization_and_slug_extraction(self) -> None:
        self.assertEqual(
            normalize_company_linkedin_url("www.linkedin.com/company/Acme%20Corp/?trk=public"),
            "https://www.linkedin.com/company/acme corp",
        )
        self.assertEqual(extract_company_public_identifier("https://linkedin.com/company/Foo-Bar#about"), "foo-bar")
        self.assertEqual(normalize_company_linkedin_url("https://www.linkedin.com/in/not-company"), "")
        self.assertEqual(extract_company_public_identifier("acme"), "")

    def test_extract_rapidapi_company_id_top_level_and_nested_ignores_harmonic(self) -> None:
        self.assertEqual(extract_rapidapi_company_id({"company_id": "12345"}), "12345")
        self.assertEqual(extract_rapidapi_company_id({"company": {"id": "nested-1"}}), "nested-1")
        self.assertEqual(
            extract_rapidapi_company_id({"companyUrn": "urn:harmonic:company:bad", "company": {"id": "ok"}}),
            "ok",
        )
        self.assertEqual(extract_rapidapi_company_id({"company_urn": "urn:harmonic:company:bad"}), "")

    def test_resolve_unresolved_and_company_key_precedence(self) -> None:
        unresolved = resolve_company_identity({"company_name": "Mystery"})
        self.assertEqual(unresolved["company_name"], "Mystery")
        self.assertEqual(unresolved["rapidapi_company_id"], "")
        self.assertEqual(unresolved["company_key"], "")

        resolved = resolve_company_identity(
            {
                "company_id": "999",
                "company_linkedin_url": "https://www.linkedin.com/company/acme/",
                "company_name": "Acme",
            }
        )
        self.assertEqual(resolved["rapidapi_company_id"], "999")
        self.assertEqual(resolved["company_public_identifier"], "acme")
        self.assertEqual(resolved["company_linkedin_url"], "https://www.linkedin.com/company/acme")
        self.assertEqual(resolved["company_key"], "linkedin_company:acme")

        harmonic = resolve_company_identity(
            {"company_urn": "urn:harmonic:company:bad", "company_linkedin_url": "linkedin.com/company/acme"}
        )
        self.assertEqual(harmonic["rapidapi_company_id"], "")
        self.assertEqual(harmonic["company_key"], "linkedin_company:acme")

    def test_metadata_lookup_by_rapidapi_id_and_linkedin_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "companies.jsonl"
            rows = [
                {
                    "rapidapi_company_id": "123",
                    "company_linkedin_url": "https://www.linkedin.com/company/acme",
                    "company_name": "Acme Metadata",
                },
                {"linkedin_slug": "slug-only", "name": "Slug Metadata"},
            ]
            corpus.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            lookup = build_company_identity_lookup([corpus])

        self.assertIn("rapidapi:123", lookup)
        self.assertIn("linkedin_company:acme", lookup)
        self.assertEqual(resolve_company_identity({"company_id": "123"}, lookup)["company_name"], "Acme Metadata")
        by_slug = resolve_company_identity("https://www.linkedin.com/company/slug-only", lookup)
        self.assertEqual(by_slug["company_name"], "Slug Metadata")
        self.assertEqual(by_slug["company_key"], "linkedin_company:slug-only")

    def test_rapidapi_experience_conversion_dates_and_current(self) -> None:
        converted = rapidapi_experience_to_powerpacks(
            {
                "title": "CEO",
                "companyName": "Acme",
                "companyId": "123",
                "companyURL": "https://www.linkedin.com/company/acme/",
                "companyUsername": "acme",
                "description": "Built things",
                "start_date": {"year": 2020, "month": 5, "day": 1},
                "end_date": {"year": 2022},
                "location": {"name": "NYC"},
                "is_current": False,
            }
        )
        self.assertEqual(converted["title"], "CEO")
        self.assertEqual(converted["company"], "Acme")
        self.assertEqual(converted["rapidapi_company_id"], "123")
        self.assertEqual(converted["company_public_identifier"], "acme")
        self.assertEqual(converted["company_linkedin_url"], "https://www.linkedin.com/company/acme")
        self.assertEqual(converted["company_key"], "linkedin_company:acme")
        self.assertEqual(converted["starts_at"], {"year": 2020, "month": 5, "day": 1})
        self.assertEqual(converted["ends_at"], {"year": 2022, "month": None, "day": None})
        self.assertFalse(converted["is_current_position"])
        self.assertEqual(converted["location"], "NYC")
        self.assertEqual(converted["source"], "rapidapi")
        self.assertEqual(converted["title_hash"], generate_title_hash("CEO", "Built things"))

        current = rapidapi_experience_to_powerpacks({"title": "Now", "start_year": "2023"})
        self.assertEqual(current["starts_at"], {"year": 2023, "month": None, "day": None})
        self.assertIsNone(current["ends_at"])
        self.assertTrue(current["is_current_position"])
        self.assertEqual(current["title_hash"], generate_title_hash("Now", ""))

    def test_title_hash_matches_aleph_contract(self) -> None:
        self.assertEqual(
            generate_title_hash("Staff Engineer\nPlatform", "Built APIs\r\nand infrastructure"),
            generate_title_hash("Staff Engineer Platform", "Built APIs  and infrastructure"),
        )
        self.assertEqual(
            generate_title_hash("Engineer", "x" * 600),
            generate_title_hash("Engineer", "x" * 500),
        )
        self.assertEqual(rapidapi_experience_to_powerpacks({"description": "No title"})["title_hash"], "")


if __name__ == "__main__":
    unittest.main()
