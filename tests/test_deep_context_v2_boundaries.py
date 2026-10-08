"""v2's typed boundaries: each external shape parses a real good input and rejects a malformed one by name."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile
from packs.ingestion.primitives.deep_context_v2.db.schema import SourceChannel
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import ProfileRecord, profile_from_record
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


def _record(**experience: object) -> dict[str, object]:
    """A profile cache record as read_usable_cached_profile returns it (raw_response and extras left out)."""
    job: dict[str, object] = {"title": "Engineer", "company_name": "Acme", "company": "Acme",
                              "starts_at": {"year": 2016, "month": 3, "day": None}, "ends_at": None}
    job.update(experience)
    return {
        "fetched_at": "2026-10-01T00:00:00Z", "public_identifier": "jordan-bravo", "raw_response": {},
        "normalized_profile": {
            "success": True, "member_id": "4242", "full_name": "Jordan Bravo", "headline": "", "location_str": "",
            "city": "Oakland", "state": "", "country": "United States", "experiences": [job],
            "education": [{"school": "UCLA", "school_name": "UCLA", "degree": "BS"}],
        },
    }


class ProfileRecordTests(unittest.TestCase):
    def test_parses_a_cached_profile_and_rejects_a_malformed_one(self) -> None:
        profile = profile_from_record("https://www.linkedin.com/in/jordan-bravo", ProfileRecord.model_validate(_record()))
        self.assertEqual(profile.experiences, ("Engineer @ Acme, 2016-present",))
        self.assertEqual(profile.education, ("BS — UCLA",))
        self.assertEqual(profile.location, "Oakland, United States")

        with self.assertRaises(ValidationError) as caught:
            ProfileRecord.model_validate(_record(starts_at={"year": "sometime"}))
        self.assertIn("normalized_profile.experiences.0.starts_at.year", str(caught.exception))


class OwnerProfileTests(unittest.TestCase):
    def test_parses_owner_json_and_rejects_a_malformed_one(self) -> None:
        payload: dict[str, object] = {
            "name": "Arthur Chen", "emails": ["Arthur@Example.com"], "phones": ["+1 (408) 555-0100"],
            "linkedin_url": "https://www.linkedin.com/in/arthur", "notes": "", "locations": ["San Francisco"],
            "work": [{"company": "Powerset", "title": "Engineer", "start": 2025, "end": 0}],
            "education": [{"school": "UCLA", "start": 2007, "end": 2010, "note": "BS"}],
        }
        owner = OwnerProfile.model_validate(payload)
        self.assertEqual(owner.emails, ("arthur@example.com",))
        self.assertEqual(owner.phones, ("+14085550100",))
        self.assertEqual((owner.work[0].start, owner.work[0].end), ("2025", ""))

        with self.assertRaises(ValidationError) as caught:
            OwnerProfile.model_validate({**payload, "work": [{"company": "Powerset", "start": 2025, "end": 0}]})
        self.assertIn("work.0.title", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
