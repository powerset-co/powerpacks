"""Shared evidence and score conversions preserve JD screening contracts."""

import unittest

from packs.search.primitives.llm_rerank_candidates import cross_encoder as ce
from packs.search.primitives.shared.human_ratings import score_1_to_5


class CrossEncoderTests(unittest.TestCase):
    def test_five_point_scale_preserves_order_and_handles_extreme_logits(self):
        raw = [-1000.0, -2.5, 0.0, 2.5, 1000.0]
        expected = [1.0, 1.30343272, 3.0, 4.69656728, 5.0]
        for score, normalized in zip(raw, expected):
            self.assertAlmostEqual(score_1_to_5(score), normalized)

    def test_profile_evidence_preserves_original_fields(self):
        profile = {
            "person_id": "person-1", "headline": "Builds storage", "title": "Engineer",
            "company": "Example Systems", "summary": "Original résumé evidence",
            "name": "Jordan Bravo", "inferred_age": 40, "inferred_birth_year": 1986,
            "gender": "example", "birth_date": "1986-01-01", "years_of_experience": 15,
            "city": "Example City", "location": "Example City", "trait_scores": {"Engineer": 1},
            "positions": [{"position_title": f"Role {index}", "start_date": "2010-01-01",
                           "end_date": "null", "is_current": False, "dense_text": "Generated duties",
                           "description": "Backend systems", "company_description": "Data infrastructure",
                           "company_funding_total": 12345, "company_headcount": 100,
                           "company_stage": "SEED", "investor_names": ["Example Ventures"],
                           "company_name": "Example Systems"}
                          for index in range(12)],
            "education": [{"school_name": "Example University", "degree": "BS",
                           "start_year": 2004, "end_year": 2008}],
        }
        # qlora-20260913-epoch2-v1 reference/lab/{ce_experiment,ce_expanded_data}.py.
        expected = {
            "headline": "Builds storage", "title": "Engineer", "company": "Example Systems",
            "summary": "Original résumé evidence",
            "education": [{"school_name": "Example University", "degree": "BS", "field_of_study": None}],
            "positions": [{"title": f"Role {index}", "company": "Example Systems", "start": "2010-01-01",
                           "is_current": False, "description": "Backend systems"} for index in range(12)],
            "companies": [{"company": "Example Systems", "company_description": "Data infrastructure",
                           "headcount": 100, "stage": "SEED", "funding_total": 12345,
                           "investors": ["Example Ventures"]}],
        }
        self.assertEqual(ce.profile_evidence(profile), expected)
        self.assertIn("inferred_age", profile)

    def test_company_dedup_keeps_distinct_facts_and_position_alias_precedence(self):
        passage = ce.profile_evidence({"positions": [
            {"title": "Engineer", "position_title": "Unused alias", "company": "Example",
             "start": " none ", "start_date": "2020", "headcount": 0, "company_headcount": 12},
            {"position_title": "Engineer", "company_name": "Example", "company_headcount": 0},
            {"title": "Engineer", "company": "Example", "headcount": 30, "investors": []},
            {},
        ]})
        self.assertEqual(passage, {
            "positions": [{"title": "Engineer", "company": "Example", "start": "2020"},
                          {"title": "Engineer", "company": "Example"},
                          {"title": "Engineer", "company": "Example"}, {}],
            "companies": [{"company": "Example", "headcount": 0},
                          {"company": "Example", "headcount": 30, "investors": []}, {}],
        })
