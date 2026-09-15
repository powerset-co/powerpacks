import json
import unittest

from packs.search.primitives.deep_search import company_context


class MoveLikelihoodTests(unittest.TestCase):
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
        messages = company_context.move_likelihood_messages(
            jd="Staff engineer owning backend systems", pond_query="Backend engineers",
            candidate=candidate, hiring_company={"name": "Delta Systems", "headcount": 30,
                                               "pull_note": "Invented pull judgment"},
            as_of="2026-09-14",
        )
        self.assertEqual(len(messages), 2)
        payload = json.loads(messages[1]["content"])
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

    def test_one_judge_requests_only_move_label_and_reason(self):
        prompt = company_context.MOVE_LIKELIHOOD_PROMPT
        self.assertIn("plausible", prompt)
        self.assertIn("unlikely", prompt)
        self.assertIn("unclear", prompt)
        self.assertIn("Qualifications are already scored", prompt)
        self.assertIn("Missing compensation does not", prompt)
        self.assertNotIn("applied_precedent_ids", prompt)
        self.assertNotIn("four independent", prompt)

    def test_parse_requires_exact_label_and_nonempty_reason(self):
        for label in ("plausible", "unlikely", "unclear"):
            self.assertEqual(company_context.parse_move_likelihood(json.dumps({
                "label": label, "why": "  Current scope fits the move.  ",
            })), {"label": label, "why": "Current scope fits the move."})
        for payload in ({"label": "founder-lock-in", "why": "Founder"},
                        {"label": "plausible", "why": ""},
                        {"label": "plausible", "why": 42},
                        {"label": "plausible", "why": "x", "score": 5}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                company_context.parse_move_likelihood(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
