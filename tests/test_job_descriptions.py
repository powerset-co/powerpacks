from __future__ import annotations

import unittest

from packs.indexing.lib.job_descriptions import (
    focused_description,
    job_description_record,
    match_job_descriptions_to_positions,
    normalize_domain,
    title_match,
)


class JobDescriptionTest(unittest.TestCase):
    def test_focuses_role_sections_and_drops_benefits(self) -> None:
        text = """ABOUT US
We make things.

WHAT YOU'LL DO
Build distributed systems in Haskell and Kubernetes. Own production reliability and mentor engineers.

REQUIREMENTS
Five years of backend engineering. Strong Haskell skills and practical Kubernetes operations experience.

BENEFITS
Free lunch and a large compensation paragraph.
"""
        focused = focused_description(text)
        self.assertIn("Build distributed systems", focused)
        self.assertIn("Strong Haskell", focused)
        self.assertNotIn("Free lunch", focused)

    def test_builds_canonical_skill_metadata(self) -> None:
        row = job_description_record({
            "listing_id": "jd-1",
            "company": "https://www.example.com/jobs",
            "title": "Senior Software Engineer",
            "description": "WHAT YOU'LL DO\n" + "Build services with Haskell and k8s. " * 20,
            "is_open": True,
        })
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(normalize_domain("https://www.Example.com/jobs"), "example.com")
        self.assertEqual(row["tech_skills"], ["haskell", "kubernetes"])

    def test_maps_same_company_compatible_titles(self) -> None:
        jobs = [{
            "id": "jd-1",
            "company_domain": "example.com",
            "title": "Senior Software Developer",
            "posted_date": "2024-06-01",
        }]
        positions = [
            {
                "id": "p-1", "person_id": "person-1", "company_domain": "www.example.com",
                "position_title": "Software Engineer", "start_date_epoch": 1_672_531_200, "end_date_epoch": 0,
            },
            {
                "id": "p-2", "person_id": "person-2", "company_domain": "example.com",
                "position_title": "VP Sales", "start_date_epoch": 1_672_531_200, "end_date_epoch": 0,
            },
        ]
        matches = match_job_descriptions_to_positions(jobs, positions)
        self.assertEqual([(row["job_description_id"], row["position_id"]) for row in matches], [("jd-1", "p-1")])
        self.assertEqual(title_match("Senior Software Developer", "Software Engineer"), (1.0, "title_exact"))

    def test_decays_nearby_postings_and_rejects_old_ones(self) -> None:
        jobs = [{
            "id": "jd-1", "company_domain": "example.com", "title": "Backend Engineer",
            "posted_date": "2024-06-01",
        }]
        positions = [{
            "id": "p-1", "person_id": "person-1", "company_domain": "example.com",
            "position_title": "Backend Engineer", "start_date_epoch": 1_577_836_800,
            "end_date_epoch": 1_640_995_200,
        }]
        matches = match_job_descriptions_to_positions(jobs, positions)
        self.assertEqual(matches[0]["posting_position_gap_days"], 882)
        self.assertEqual(matches[0]["match_score"], 0.65)

        jobs[0]["posted_date"] = "2026-06-01"
        self.assertEqual(match_job_descriptions_to_positions(jobs, positions), [])


if __name__ == "__main__":
    unittest.main()
