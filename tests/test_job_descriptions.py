from __future__ import annotations

import json
import unittest

from packs.indexing.lib import job_descriptions

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

    def test_extracts_job_description_from_polluted_page_source(self) -> None:
        description = "WHAT YOU'LL DO\n" + "Build distributed systems in Haskell. " * 20
        row = job_description_record({
            "listing_id": "jd-1",
            "company": "example.com",
            "title": "Software Engineer",
            "description": "window.COUNTRY_CODE = 'US';" + "x" * 25_000 + json.dumps({"description": description}),
        })

        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("Build distributed systems", row["retrieval_text"])
        self.assertNotIn("window.COUNTRY_CODE", row["retrieval_text"])

    def test_rejects_polluted_page_without_job_description(self) -> None:
        row = job_description_record({
            "listing_id": "jd-1",
            "company": "example.com",
            "title": "Software Engineer",
            "description": "window.COUNTRY_CODE = 'US';" + "x" * 25_000,
        })

        self.assertIsNone(row)

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
                "company_headcount": 100,
            },
            {
                "id": "p-2", "person_id": "person-2", "company_domain": "example.com",
                "position_title": "VP Sales", "start_date_epoch": 1_672_531_200, "end_date_epoch": 0,
                "company_headcount": 100,
            },
        ]
        matches = match_job_descriptions_to_positions(jobs, positions)
        self.assertEqual([(row["job_description_id"], row["position_id"]) for row in matches], [("jd-1", "p-1")])
        self.assertEqual(title_match("Senior Software Developer", "Software Engineer"), (1.0, "title_exact"))

    def test_maps_current_open_job_without_claiming_a_posted_date(self) -> None:
        jobs = [{
            "id": "jd-1", "company_domain": "example.com", "title": "Backend Engineer",
            "posted_date": "", "is_open": True,
        }]
        positions = [{
            "id": "p-1", "person_id": "person-1", "company_domain": "example.com",
            "position_title": "Backend Engineer", "start_date_epoch": 1_577_836_800,
            "end_date_epoch": 0,
            "company_headcount": 25,
        }]
        matches = match_job_descriptions_to_positions(jobs, positions)
        self.assertEqual(matches[0]["match_type"], "title_exact_observed_open")
        self.assertEqual(matches[0]["posting_position_gap_days"], 0)

    def test_matches_same_role_phrase_across_specializations(self) -> None:
        self.assertEqual(
            title_match("Senior Product Manager", "Product Manager, Content Sharing"),
            (0.6, "title_phrase"),
        )
        self.assertIsNone(title_match("Principal Software Engineer", "Research Engineer"))

    def test_decays_nearby_postings_and_rejects_old_ones(self) -> None:
        jobs = [{
            "id": "jd-1", "company_domain": "example.com", "title": "Backend Engineer",
            "posted_date": "2024-06-01",
        }]
        positions = [{
            "id": "p-1", "person_id": "person-1", "company_domain": "example.com",
            "position_title": "Backend Engineer", "start_date_epoch": 1_577_836_800,
            "end_date_epoch": 1_640_995_200,
            "company_headcount": 25,
        }]
        matches = match_job_descriptions_to_positions(jobs, positions)
        self.assertEqual(matches[0]["posting_position_gap_days"], 882)
        self.assertEqual(matches[0]["match_score"], 0.2275)

        jobs[0]["posted_date"] = "2026-06-01"
        self.assertEqual(match_job_descriptions_to_positions(jobs, positions), [])


class PositionWorkMatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = {
            "id": "jd-1", "company_domain": "example.com", "title": "Software Engineer",
            "posted_date": "2024-06-01", "retrieval_text": "Build distributed storage engines.",
            "vector": [1.0, 0.0],
        }
        self.position = {
            "id": "position-1", "person_id": "person-1", "company_domain": "example.com",
            "position_title": "Software Engineer", "start_date_epoch": 1_672_531_200,
            "end_date_epoch": 0, "company_headcount": 101,
            "description": "Built distributed storage systems for database replication.",
        }
        self.evidence = {
            "job_description_id": "jd-1", "position_id": "position-1",
            "position_evidence": "Built distributed storage systems",
            "jd_evidence": "Build distributed storage engines.",
            "rationale": "Both positions build distributed storage.",
        }

    def test_large_and_unknown_companies_cannot_link_by_title_only(self) -> None:
        for headcount in [101, 100_000, 0, None]:
            with self.subTest(headcount=headcount):
                self.position["company_headcount"] = headcount
                self.assertEqual(match_job_descriptions_to_positions([self.job], [self.position]), [])

    def test_small_company_title_links_are_weak(self) -> None:
        self.position["company_headcount"] = 100
        matches = match_job_descriptions_to_positions([self.job], [self.position])
        self.assertEqual(matches[0]["match_score"], 0.35)

    def test_supported_work_links_do_not_require_similar_titles(self) -> None:
        self.position["position_title"] = "Member of Technical Staff"
        self.evidence["position_evidence"] = "BUILT distributed\n storage systems"
        matches = match_job_descriptions_to_positions(
            [self.job], [self.position], work_matches=[self.evidence],
        )
        self.assertEqual(matches[0]["match_score"], 0.8)
        self.assertEqual(matches[0]["match_type"], "work_semantic")
        self.assertEqual(matches[0]["position_id"], "position-1")

    def test_work_links_keep_pair_identity_and_date_penalty(self) -> None:
        self.position["company_headcount"] = 50
        title_match_row = match_job_descriptions_to_positions([self.job], [self.position])[0]
        self.position["end_date_epoch"] = 1_704_067_200
        work_match_row = match_job_descriptions_to_positions(
            [self.job], [self.position], work_matches=[self.evidence],
        )[0]
        self.assertEqual(work_match_row["id"], title_match_row["id"])
        self.assertEqual(work_match_row["match_score"], 0.52)
        self.assertGreater(work_match_row["posting_position_gap_days"], 0)

    def test_quoted_evidence_can_include_outer_quotation_marks(self) -> None:
        evidence = {**self.evidence,
                    "position_evidence": '"' + self.evidence["position_evidence"] + '"',
                    "jd_evidence": '“' + self.evidence["jd_evidence"] + '”'}
        self.assertEqual(len(match_job_descriptions_to_positions(
            [self.job], [self.position], work_matches=[evidence],
        )), 1)

    def test_fabricated_or_empty_quotes_do_not_create_links(self) -> None:
        for field in ["position_evidence", "jd_evidence"]:
            for value in ["", "Invented GPU kernel development"]:
                with self.subTest(field=field, value=value):
                    evidence = {**self.evidence, field: value}
                    self.assertEqual(match_job_descriptions_to_positions(
                        [self.job], [self.position], work_matches=[evidence],
                    ), [])

    def test_absent_description_and_title_only_quotes_cannot_support_work(self) -> None:
        for description in ["", "Software Engineer", "Software Engineer at Example"]:
            with self.subTest(description=description):
                self.position["description"] = description
                self.evidence["position_evidence"] = "Software Engineer"
                self.assertEqual(match_job_descriptions_to_positions(
                    [self.job], [self.position], work_matches=[self.evidence],
                ), [])

    def test_different_work_without_positive_review_does_not_link(self) -> None:
        self.position["description"] = "Built mobile application interfaces."
        self.assertEqual(match_job_descriptions_to_positions(
            [self.job], [self.position], work_matches=[],
        ), [])

    def test_evidence_cannot_override_employer_or_date(self) -> None:
        for overrides in [
            {"company_domain": "other.example"},
            {"start_date_epoch": 0},
            {"end_date_epoch": 1_546_300_800},
        ]:
            with self.subTest(overrides=overrides):
                position = {**self.position, **overrides}
                self.assertIsNone(job_descriptions.posting_position_gap_days(self.job, position))
                self.assertEqual(match_job_descriptions_to_positions(
                    [self.job], [position], work_matches=[self.evidence],
                ), [])

        self.job.update(posted_date="", is_open=False)
        self.assertIsNone(job_descriptions.posting_position_gap_days(self.job, self.position))

    def test_semantic_candidates_use_work_not_title_and_filter_before_top_k(self) -> None:
        jobs = [
            {**self.job, "id": "wrong-company", "company_domain": "other.example"},
            {**self.job, "id": "wrong-date", "posted_date": "2010-01-01"},
            {**self.job, "id": "same-title", "vector": [0.0, 1.0], "retrieval_text": "Build mobile apps."},
            {**self.job, "id": "different-title", "title": "Member of Technical Staff"},
        ]
        matches = job_descriptions.semantic_job_candidates(jobs, self.position, [1.0, 0.0], top_k=1)
        self.assertEqual([row["id"] for row in matches], ["different-title"])

    def test_semantic_candidates_skip_missing_vectors_and_deduplicate_mirrors(self) -> None:
        jobs = [
            self.job,
            {**self.job, "id": "mirror"},
            {**self.job, "id": "no-vector", "vector": None, "retrieval_text": "Other work."},
            {**self.job, "id": "another", "vector": [0.8, 0.2], "retrieval_text": "Own storage reliability."},
        ]
        matches = job_descriptions.semantic_job_candidates(jobs, self.position, [1.0, 0.0], top_k=2)
        self.assertEqual([row["id"] for row in matches], ["jd-1", "another"])

    def test_semantic_candidates_prefer_nearby_work(self) -> None:
        jobs = [
            {**self.job, "id": "older", "posted_date": "2021-01-01"},
            {**self.job, "id": "current", "vector": [0.9, 0.1], "retrieval_text": "Build storage infrastructure."},
        ]
        matches = job_descriptions.semantic_job_candidates(jobs, self.position, [1.0, 0.0], top_k=1)
        self.assertEqual(matches[0]["id"], "current")


if __name__ == "__main__":
    unittest.main()
