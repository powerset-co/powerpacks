"""Positions and company artifacts must join on the same stable UUID."""

import unittest

from packs.indexing.lib.artifacts import build_company_corpus, stable_company_uuid
from packs.indexing.lib.people import _position_to_profile, build_roles


class CompanyIdConvergenceTests(unittest.TestCase):
    def test_position_and_company_ids_match_for_provider_and_fallback_keys(self):
        fixtures = [
            ({"company_id": "12345", "company_name": "Example Co"}, "3622ee4f-7f81-5498-9da6-6225d721bcca"),
            ({"rapidapi_company_id": "12345", "company_name": "Example Co"}, "3622ee4f-7f81-5498-9da6-6225d721bcca"),
            ({"company_id": "urn:harmonic:company:12345", "company_name": "Example Co"}, "89369f03-12e4-5d3e-80ff-ff4adf383e89"),
            ({"company_name": " Example   Co "}, "89369f03-12e4-5d3e-80ff-ff4adf383e89"),
            ({"company_id": "12345", "company_linkedin_url": "https://linkedin.com/company/Example-Co/?trk=test", "company_name": "Example Co"}, None),
        ]
        for experience, expected in fixtures:
            with self.subTest(experience=experience):
                experience = {**experience, "title": "Engineer"}
                person = {"id": "9b1654d9-9465-5f0d-bb11-f5f16b1a2c38", "work_experiences": [experience]}
                company = build_company_corpus([person])[0]
                role = build_roles([person])[0]
                self.assertEqual(role["company_id"], company["id"])
                self.assertEqual(_position_to_profile(experience, 0)["company_id"], company["id"])
                self.assertEqual(stable_company_uuid(experience), company["id"])
                if expected:
                    self.assertEqual(company["id"], expected)

    def test_reviewed_company_id_wins_over_harmonic_name(self):
        canonical = "797a0ad9-b73d-5722-a6d1-7fdbd6315119"
        experience = {
            "company": "urn:harmonic:company:8327420",
            "company_name": "Cornell University Graduate School",
            "canonical_company_id": canonical,
            "title": "Researcher",
        }
        person = {"id": "9b1654d9-9465-5f0d-bb11-f5f16b1a2c38",
                  "work_experiences": [experience]}
        self.assertEqual(build_company_corpus([person])[0]["id"], canonical)
        self.assertEqual(build_roles([person])[0]["company_id"], canonical)
        self.assertEqual(_position_to_profile(experience, 0)["company_id"], canonical)

    def test_reviewed_company_id_wins_with_linkedin_slug(self):
        canonical = "797a0ad9-b73d-5722-a6d1-7fdbd6315119"
        experience = {
            "company": "urn:harmonic:company:8327420",
            "company_name": "Cornell University Graduate School",
            "company_linkedin_url": "https://linkedin.com/company/cornell-university",
            "company_public_identifier": "cornell-university",
            "canonical_company_id": canonical,
            "title": "Researcher",
        }
        person = {"id": "9b1654d9-9465-5f0d-bb11-f5f16b1a2c38",
                  "work_experiences": [experience]}
        self.assertEqual(build_company_corpus([person])[0]["id"], canonical)
        self.assertEqual(build_roles([person])[0]["company_id"], canonical)
        self.assertEqual(_position_to_profile(experience, 0)["company_id"], canonical)

    def test_zero_provider_ids_use_distinct_company_names(self):
        ids = []
        for name in ("Example One", "Example Two"):
            experience = {
                "company_name": name, "companyId": 0,
                "rapidapi_company_id": "0", "company_key": "rapidapi:0",
                "title": "Engineer",
            }
            person = {"id": "person", "work_experiences": [experience]}
            company = build_company_corpus([person])[0]
            self.assertEqual(company["id"], stable_company_uuid({"company_name": name}))
            self.assertEqual(_position_to_profile(experience, 0)["company_id"], company["id"])
            ids.append(company["id"])
        self.assertNotEqual(*ids)

    def test_no_company_stays_unlinked(self):
        self.assertIsNone(_position_to_profile({"title": "Engineer"}, 0)["company_id"])


if __name__ == "__main__":
    unittest.main()
