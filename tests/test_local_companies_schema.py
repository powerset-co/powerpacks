import unittest
from decimal import Decimal
from packs.indexing.lib.local_companies_schema import (
    COMPANY_COLUMNS,
    COMPANY_PERSON_COLUMNS,
    json_safe,
    normalize_company_person_row,
    normalize_company_row,
    validate_company_columns,
    validate_company_person_columns,
)


class LocalCompaniesSchemaTests(unittest.TestCase):
    def test_company_columns_validate_required_and_report_extra(self):
        result = validate_company_columns(["id", "name", "unexpected"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["extra"], ["unexpected"])
        self.assertFalse(validate_company_columns(["id"])["ok"])

    def test_person_columns_validate_required(self):
        self.assertTrue(validate_company_person_columns(["id", "name"])["ok"])
        self.assertFalse(validate_company_person_columns(["name"])["ok"])
        self.assertIn("id", COMPANY_PERSON_COLUMNS)
        self.assertIn("people_has_more", COMPANY_COLUMNS)

    def test_normalize_company_row_aliases_lists_and_numbers(self):
        row = normalize_company_row(
            {
                "company_id": "c1",
                "company_name": " Acme AI ",
                "entity_sector_text": "ai, infrastructure",
                "entity_type": '["startup"]',
                "employee_count": "42",
                "total_funding": Decimal("1200.50"),
                "person_count": "3",
            }
        )
        self.assertEqual(row["id"], "c1")
        self.assertEqual(row["name"], "Acme AI")
        self.assertEqual(row["sector_types"], ["ai", "infrastructure"])
        self.assertEqual(row["entity_types"], ["startup"])
        self.assertEqual(row["headcount"], 42)
        self.assertEqual(row["funding_total"], 1200.5)
        self.assertEqual(row["people_count"], 3)

    def test_normalize_company_person_row(self):
        row = normalize_company_person_row(
            {
                "person_id": "p1",
                "full_name": " Ada Lovelace ",
                "title": "Founder",
                "is_current": "yes",
                "tenure_years": "2.5",
                "all_positions": '[{"title":"Founder"}]',
            }
        )
        self.assertEqual(row["id"], "p1")
        self.assertEqual(row["name"], "Ada Lovelace")
        self.assertEqual(row["position_title"], "Founder")
        self.assertTrue(row["is_current"])
        self.assertEqual(row["tenure_years"], 2.5)
        self.assertEqual(row["all_positions"], [{"title": "Founder"}])

    def test_json_safe_matches_contact_large_number_behavior(self):
        self.assertEqual(json_safe(10**30), str(10**30))
        self.assertIsNone(json_safe(float("inf")))
        self.assertIsNone(json_safe(float("nan")))
        self.assertEqual(json_safe(Decimal("9007199254740992")), "9007199254740992")
        self.assertEqual(json_safe(Decimal("12.25")), "12.25")


if __name__ == "__main__":
    unittest.main()
