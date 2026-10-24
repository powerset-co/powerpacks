"""Realize's research path writes experiences in the fetched-profile shape, title hash included."""
from __future__ import annotations

import json
import unittest

from packs.ingestion.primitives.deep_context_v2.realize.realize import fill_research
from packs.ingestion.schemas.company_identity import generate_title_hash


class FillResearchTests(unittest.TestCase):
    def test_every_experience_carries_its_upstream_title_hash(self) -> None:
        row = {"public_identifier": "", "linkedin_url": ""}
        fill_research(row, {
            "real_name": "A Person",
            "summary": "",
            "location_city": "Berlin",
            "location_country": "Germany",
            "work_experience": [
                {"title": "Co-Founder", "company_name": "Acme", "company_domain": "acme.io",
                 "description": "Built the thing", "start_date": "2021-01", "end_date": None, "is_current": True},
                {"title": "Advisor", "company_name": "Beta", "description": None,
                 "start_date": "2019-01", "end_date": "2020-12", "is_current": False},
            ],
            "education": [],
        })
        experiences = json.loads(row["work_experiences"])
        self.assertEqual([exp["title"] for exp in experiences], ["Co-Founder", "Advisor"])
        self.assertEqual(experiences[0]["title_hash"], generate_title_hash("Co-Founder", "Built the thing"))
        self.assertEqual(experiences[1]["title_hash"], generate_title_hash("Advisor", ""))
        self.assertTrue(all(exp["title_hash"] for exp in experiences))
        self.assertEqual(experiences[0]["company"], "Acme")
        self.assertTrue(experiences[0]["is_current_position"])
        self.assertEqual((row["current_title"], row["current_company"]), ("Co-Founder", "Acme"))
        self.assertEqual(row["location_raw"], "Berlin, Germany")


if __name__ == "__main__":
    unittest.main()
