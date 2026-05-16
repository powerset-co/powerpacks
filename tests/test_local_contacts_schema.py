import unittest
from datetime import datetime
from decimal import Decimal

from packs.indexing.lib.local_contacts_schema import (
    LINKEDIN_CANDIDATE_COLUMNS,
    json_safe,
    normalize_contact_row,
    parse_emails,
    to_bool,
    validate_linkedin_candidate_columns,
)


class LocalContactsSchemaTests(unittest.TestCase):
    def test_validate_required_and_extra_columns(self):
        result = validate_linkedin_candidate_columns(LINKEDIN_CANDIDATE_COLUMNS + ["extra"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["extra"], ["extra"])
        missing = validate_linkedin_candidate_columns(["id"])
        self.assertFalse(missing["ok"])
        self.assertIn("primary_email", missing["missing"])

    def test_email_bool_and_numeric_normalization(self):
        self.assertEqual(
            parse_emails('["A@EXAMPLE.com", "a@example.com", "b@test.com"]'), ["a@example.com", "b@test.com"]
        )
        self.assertTrue(to_bool("yes"))
        self.assertFalse(to_bool("0"))
        row = normalize_contact_row(
            {"primary_email": "A@Example.com", "total_messages": "4.0", "candidate_count": "bad"}
        )
        self.assertEqual(row["primary_email"], "a@example.com")
        self.assertEqual(row["all_emails"], ["a@example.com"])
        self.assertEqual(row["total_messages"], 4)
        self.assertEqual(row["candidate_count"], 0)

    def test_candidate_to_unified_contact_and_json_safe(self):
        row = normalize_contact_row(
            {
                "id": "1",
                "display_name": "Ada Lovelace",
                "confirmed_linkedin_url": "https://linkedin.com/in/ada",
                "updated_at": datetime(2024, 1, 2, 3, 4, 5),
                "llm_confidence": Decimal("0.95"),
                "all_emails": "ada@example.com; other@example.com",
            }
        )
        self.assertEqual(row["linkedin_url"], "https://linkedin.com/in/ada")
        self.assertEqual(row["updated_at"], "2024-01-02T03:04:05")
        self.assertEqual(row["all_emails"], ["ada@example.com", "other@example.com"])
        self.assertEqual(json_safe(10**30), str(10**30))


if __name__ == "__main__":
    unittest.main()
