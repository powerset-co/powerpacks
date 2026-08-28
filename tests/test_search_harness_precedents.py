from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.deep_search import precedents


class SearchHarnessPrecedentTests(unittest.TestCase):
    def test_retrieves_capability_first_jake_chain_by_content_and_failure(self) -> None:
        cards = precedents.retrieve_next_moves(
            title="Reinforcement Learning Research Engineer",
            brief={"occupation": "machine learning engineer",
                   "defining_capability": "reinforcement learning"},
            query="Engineer with reinforcement learning experience",
            diagnosis="wrong_specialty", roots=())

        self.assertEqual(cards[0]["family"], "machine learning reinforcement learning")
        self.assertEqual(cards[0]["chain"][0]["action"], "add_adjacent_pond")

    def test_human_payload_override_becomes_retrievable_without_a_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Synthetic Infrastructure Engineer",
                "brief": {"occupation": "infrastructure engineer"},
                "iterations": [{
                    "query": "Infrastructure Engineer",
                    "pattern_default_edits": [],
                    "human_edit_delta": {"filters": {
                        "role_ids": {"from": ["frontend_engineer", "infrastructure_engineer"],
                                     "to": ["infrastructure_engineer"]}}},
                }],
            }), encoding="utf-8")

            cards = precedents.retrieve_payload_edits(
                title="Infrastructure Engineer",
                brief={"occupation": "infrastructure engineer"}, query="Infrastructure Engineer",
                roots=(root,))

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["human_edit_delta"]["filters"]["role_ids"]["to"],
                         ["infrastructure_engineer"])

    def test_payload_edits_record_accepted_and_reverted_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Synthetic Infrastructure Engineer",
                "brief": {"occupation": "infrastructure engineer"},
                "iterations": [{
                    "query": "Infrastructure Engineer",
                    "pattern_default_edits": [
                        {"field": "role_ids", "to": ["infrastructure_engineer"]},
                        {"field": "seniority_bands", "to": ["mid", "senior"]},
                    ],
                    "human_edit_delta": {"filters": {
                        "role_ids": {"from": ["infrastructure_engineer"],
                                     "to": ["backend_engineer"]}}},
                }],
            }), encoding="utf-8")

            cards = precedents.retrieve_payload_edits(
                title="Infrastructure Engineer",
                brief={"occupation": "infrastructure engineer"},
                query="Infrastructure Engineer", roots=(root,))

        verdicts = {row["field"]: row["verdict"] for row in cards[0]["pattern_default_edits"]}
        self.assertEqual(verdicts, {"role_ids": "reverted", "seniority_bands": "accepted"})

    def test_human_confirmed_unchanged_payload_edit_stays_positive_precedent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Synthetic Recruiter", "brief": {"occupation": "recruiter"},
                "iterations": [{
                    "query": "Recruiters in New York", "payload_reviewed": True,
                    "pattern_default_edits": [{"field": "seniority_bands",
                                               "to": ["senior", "manager"]}],
                    "human_edit_delta": None,
                }],
            }), encoding="utf-8")

            cards = precedents.retrieve_payload_edits(
                title="Synthetic Recruiter", brief={"occupation": "recruiter"},
                query="Recruiters in New York", roots=(root,))

        self.assertEqual(cards[0]["quality"], "human_confirmed")
        self.assertEqual(cards[0]["pattern_default_edits"][0]["verdict"], "accepted")

    def test_next_move_retrieval_excludes_unreviewed_history_and_keeps_jake_cross_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Search Engineer",
                "brief": {"occupation": "software engineer",
                          "defining_capability": "search systems"},
                "iterations": [{
                    "query": "Software Engineer with search systems experience in the Bay Area",
                    "diagnosis": "weak_quality",
                    "next_move": {"action": "refine_current_pond",
                                  "next_query": "Search Engineer in San Francisco"},
                    "proposal_delta": {"reviewed": False, "actual": {
                        "action": "refine_current_pond",
                        "next_query": "Search Engineer in San Francisco"}},
                }],
            }), encoding="utf-8")

            cards = precedents.retrieve_next_moves(
                title="Search Engineer",
                brief={"occupation": "software engineer",
                       "defining_capability": "search systems"},
                query="Software Engineer with search systems experience in the Bay Area",
                diagnosis="weak_quality", roots=(root,))

        self.assertEqual(cards[0]["quality"], "jake_seed")
        self.assertEqual(cards[0]["failure_mode"], "exhausted")
        self.assertIn("distributed systems", cards[0]["chain"][0]["next_query"])
        self.assertFalse(any(card.get("source") == str(run / "results.json") for card in cards))

    def test_explicitly_reviewed_move_becomes_precedent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Synthetic Platform Operator",
                "brief": {"occupation": "platform operator"},
                "iterations": [{
                    "query": "Platform operator in New York", "diagnosis": "weak_quality",
                    "next_move": {"action": "add_adjacent_pond",
                                  "next_query": "Technical operations analyst in New York"},
                    "proposal_delta": {"reviewed": True, "actual": {
                        "action": "add_adjacent_pond",
                        "next_query": "Technical operations analyst in New York"}},
                }],
            }), encoding="utf-8")

            cards = precedents.retrieve_next_moves(
                title="Synthetic Platform Operator",
                brief={"occupation": "platform operator"},
                query="Platform operator in New York", diagnosis="weak_quality",
                roots=(root,), limit=20)

        self.assertTrue(any(card.get("source") == str(run / "results.json") and
                            card.get("quality") == "human_confirmed" for card in cards))

    def test_reviewed_candidate_fit_becomes_tiered_precedent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Synthetic Systems Engineer",
                "brief": {"occupation": "software engineer"},
                "iterations": [{"shortlist_grades": [{
                    "title": "Senior Software Engineer", "company": "Synthetic Product Co",
                    "fit_override": {
                        "reviewed": True, "group": "send_worthy",
                        "why": "Human confirmed direct product evidence and a plausible move.",
                    },
                }]}],
            }), encoding="utf-8")

            cards = precedents.retrieve_fit_precedents(
                title="Synthetic Backend Engineer", brief={"occupation": "software engineer"},
                target_level="senior_ic",
                candidate={"title": "Senior Software Engineer",
                           "company": "Synthetic Product Co"},
                dimension="final_decision",
                roots=(root,))

        self.assertEqual(cards[0]["judgment"]["group"], "send_worthy")
        self.assertEqual(cards[0]["quality"], "human_confirmed")
        self.assertEqual(cards[0]["quality_tier"], 2)
        self.assertNotIn("retrieval_text", cards[0])

    def test_empty_fit_policy_loads_no_precedents(self) -> None:
        self.assertEqual(precedents.load_fit_precedents(roots=()), [])

    def test_fit_retrieval_excludes_the_source_person_but_not_the_company(self) -> None:
        cards = [{
            "id": "reviewed-product-fit", "dimension": "final_decision",
            "jd_context": "product engineer user-facing product decisions",
            "candidate_context": "software engineer at Synthetic Product Co consumer product",
            "judgment": {"group": "send_worthy"}, "reason": "Reviewed evidence.",
            "source_person": "person-1", "source_jd": "jd-1", "quality_tier": 2,
        }]
        kwargs = {
            "title": "Product Engineer",
            "brief": {"occupation": "product engineer",
                      "defining_capability": "user-facing product decisions"},
            "target_level": "senior_ic", "dimension": "final_decision", "cards": cards,
        }

        self.assertEqual(precedents.retrieve_fit_precedents(
            candidate={"person": "person-1", "title": "Software Engineer",
                       "company": "Synthetic Product Co"}, **kwargs), [])
        retrieved = precedents.retrieve_fit_precedents(
            candidate={"person": "person-2", "title": "Software Engineer",
                       "company": "Synthetic Product Co",
                       "recent_roles": [{"description": "consumer product"}]}, **kwargs)
        self.assertEqual(retrieved[0]["id"], "reviewed-product-fit")


if __name__ == "__main__":
    unittest.main()
