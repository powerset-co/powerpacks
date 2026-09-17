import json
import unittest

from packs.search.primitives.deep_search import candidate_judges as company_context


class CandidateJudgeEvidenceTests(unittest.TestCase):
    def test_request_preserves_full_original_career_and_company_context(self):
        candidate = {
            "name": "Jordan Bravo", "age": 44, "dense_text": "Invented duties",
            "headline": "Founder and software engineer", "summary": "Builds storage systems.",
            "positions": [{
                "title": "Founder", "company_name": "Example Systems", "is_current": True,
                "start_date": "2023-02-01", "description": "Builds APIs and reviews code.",
                "company_headcount": 4, "company_funding_total": 500000,
                "dense_text": "Generated executive duties", "seniority_band": "Executive",
            }, {
                "title": "Staff Engineer", "company_name": "Bravo Data", "is_current": False,
                "start_date": "2018-01-01", "end_date": "2023-01-01",
                "description": "Owned the storage engine.",
            }],
            "current_company_headcount": 5, "current_company_stage": "SEED",
            "current_company_funding": 750000, "current_company_funding_basis": "total_raised",
        }
        before = json.dumps(candidate, sort_keys=True)
        messages = company_context.candidate_judge_messages(dimension="domain",
            jd="Staff engineer owning backend systems", pond_query="Backend engineers",
            candidate=candidate, hiring_company={"name": "Delta Systems", "headcount": 30,
                                               "pull_note": "Invented pull judgment"},
            as_of="2026-09-14",
        )
        self.assertEqual(len(messages), 2)
        prefix, suffix = messages[1]["content"]
        payload = {**json.loads(prefix["text"]), **json.loads(suffix["text"])}
        self.assertEqual(payload["as_of"], "2026-09-14")
        self.assertEqual(payload["pond_query"], "Backend engineers")
        self.assertEqual(payload["candidate"]["positions"][1]["description"], "Owned the storage engine.")
        self.assertEqual(payload["candidate"]["positions"][0]["start"], "2023-02-01")
        self.assertEqual(payload["candidate"]["companies"][0]["headcount"], 4)
        self.assertEqual(payload["current_company"]["headcount"], 5)
        self.assertEqual(payload["current_company"]["funding_basis"], "total_raised")
        self.assertEqual(payload["hiring_company"], {"name": "Delta Systems", "headcount": 30})
        for field in ("age", "name", "dense_text", "seniority_band", "rerank_score", "traits"):
            self.assertNotIn(f'"{field}"', json.dumps(payload["candidate"]))
        self.assertIsNone(payload["comp_band"])
        self.assertEqual(json.dumps(candidate, sort_keys=True), before)

    def test_scores_are_strict_integers(self):
        for score in (True, 4.0, "4", 0, 6):
            with self.subTest(score=score), self.assertRaises(ValueError):
                company_context.parse_candidate_judge(json.dumps({"score": score,
                    "why": "Relevant work", "evidence": [], "concerns": []}), "domain")
        self.assertEqual(company_context.parse_candidate_judge(json.dumps({"score": 4,
            "why": "Relevant work", "evidence": [], "concerns": []}), "domain")["score"], 4)
