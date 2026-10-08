"""v2's typed boundaries: each external shape parses a real good input and rejects a malformed one by name."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from packs.ingestion.primitives.deep_context_v2.db.schema import SourceChannel
from packs.ingestion.primitives.deep_context_v2.import_load.import_row import ImportRow


def _cells(**overrides: str) -> dict[str, str]:
    """A messages people.csv row as the importer writes it (the superset columns v2 ignores left out)."""
    cells: dict[str, str] = {
        "id": "candidate:phone:+13235550100", "full_name": "Annmay Yang", "first_name": "Annmay", "last_name": "Yang",
        "primary_email": "", "primary_phone": "+13235550100", "all_phones": '["+13235550100"]',
        "source_channels": "imessage,whatsapp", "interaction_counts": '{"imessage": 113171, "whatsapp": 4}',
        "last_interaction": "2026-10-07T04:53:50+00:00", "linkedin_url": "",
    }
    cells.update(overrides)
    return cells


class ImportRowTests(unittest.TestCase):
    def test_parses_importer_rows_and_rejects_a_malformed_one(self) -> None:
        row = ImportRow.model_validate(_cells())
        self.assertEqual(row.source_channels, (SourceChannel.IMESSAGE, SourceChannel.WHATSAPP))
        self.assertEqual(row.interaction_counts, {"imessage": 113171, "whatsapp": 4})
        # The Gmail importer writes a blank cell for an address with no counted messages.
        gmail = ImportRow.model_validate(_cells(primary_phone="", primary_email="a@example.com",
                                                source_channels="gmail_msgvault", interaction_counts=""))
        self.assertEqual(gmail.interaction_counts, {})

        with self.assertRaises(ValidationError) as caught:
            ImportRow.model_validate(_cells(source_channels="fax", interaction_counts="{gmail: 3"))
        message = str(caught.exception)
        self.assertIn("source_channels", message)
        self.assertIn("interaction_counts", message)


if __name__ == "__main__":
    unittest.main()
