from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.deep_search import precedents


class SearchHarnessPrecedentTests(unittest.TestCase):
    def test_retrieves_capability_first_seed_chain_by_content_and_failure(self) -> None:
        cards = precedents.retrieve_next_moves(
            title="Reinforcement Learning Research Engineer",
            brief={"occupation": "machine learning research engineer",
                   "defining_capability": (
                       "Design reinforcement learning pipelines, reward functions, and "
                       "experiments for foundation models.")},
            query="Engineer with reinforcement learning experience",
            diagnosis="wrong_specialty", roots=())

        self.assertEqual(cards[0]["family"], "machine learning research engineering")
        self.assertEqual(cards[0]["chain"][0]["action"], "add_adjacent_pond")

    def test_next_move_retrieval_ignores_the_evolving_query(self) -> None:
        arguments = {
            "title": "Software Engineer, Growth",
            "brief": {
                "occupation": "product software engineer",
                "defining_capability": (
                    "Write production frontend and full-stack code for signup, onboarding, "
                    "activation, experiments, conversion, and product growth.")},
            "diagnosis": "too_few", "roots": (),
        }
        first = precedents.retrieve_next_moves(
            **arguments, query="Growth engineers in San Francisco")
        second = precedents.retrieve_next_moves(
            **arguments, query="An unrelated search for enterprise account executives")

        self.assertEqual([card["job"] for card in first],
                         [card["job"] for card in second])
        self.assertEqual(first[0]["job"], "Growth Engineer")

    def test_growth_engineering_and_marketing_cards_do_not_cross(self) -> None:
        engineering = precedents.retrieve_next_moves(
            title="Member of Technical Staff - Product (Growth)",
            brief={"occupation": "product software engineer",
                   "defining_capability": (
                       "Build user-facing growth surfaces and instrument experiments across "
                       "onboarding, activation, and conversion.")},
            query="Software engineer with growth experience in San Francisco",
            diagnosis="too_few", roots=())
        marketing = precedents.retrieve_next_moves(
            title="Head of Growth Marketing",
            brief={"occupation": "growth marketing leader",
                   "defining_capability": (
                       "Lead multi-channel acquisition across content, organic discovery, "
                       "product marketing, partnerships, brand, and performance.")},
            query="Growth marketing manager in San Francisco",
            diagnosis="too_few", roots=())

        self.assertEqual(engineering[0]["job"], "Growth Engineer")
        self.assertEqual(marketing[0]["job"], "Head of Growth Marketing")
        self.assertFalse(any(card["job"] == "Growth Engineer" for card in marketing))

    def test_fde_ml_card_does_not_apply_to_fde_systems(self) -> None:
        cards = precedents.retrieve_next_moves(
            title="Forward Deployed Engineer - Systems",
            brief={"occupation": "customer-facing systems engineer",
                   "defining_capability": (
                       "Lead cloud migrations, Kubernetes architecture, deployment strategy, "
                       "technical sales, adoption consulting, and organizational change.")},
            query="Forward deployed systems engineers in Europe",
            diagnosis="too_few", roots=())

        self.assertEqual(cards, [])

    def test_seed_move_cards_never_qualify_a_software_pond_by_customer_industry(self) -> None:
        industry = re.compile(
            r"\b(software|backend|frontend|infrastructure|platform|full[- ]stack) engineer with "
            r"(fintech|healthtech|biotech|edtech|proptech|insurtech|b2b saas|saas|developer tools|"
            r"ai infrastructure|consumer|e-?commerce) experience", re.IGNORECASE)
        for card in precedents._seed_move_cards():
            for link in card.get("chain") or []:
                for key in ("query", "next_query"):
                    with self.subTest(job=card.get("job"), query=link.get(key)):
                        self.assertIsNone(industry.search(str(link.get(key) or "")))

    def test_seed_move_cards_describe_when_the_lesson_applies(self) -> None:
        self.assertTrue(all(
            all(str(card.get(field) or "").strip()
                for field in ("family", "defining_capability", "excludes"))
            for card in precedents._seed_move_cards()
        ))

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

    def test_next_move_retrieval_excludes_unreviewed_history_and_keeps_seed_cross_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Search Engineer",
                "brief": {"occupation": "search infrastructure engineer",
                          "defining_capability": (
                              "Build and operate crawling, indexing, ranking, and retrieval "
                              "systems at scale.")},
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
                brief={"occupation": "search infrastructure engineer",
                       "defining_capability": (
                           "Build and operate crawling, indexing, ranking, and retrieval "
                           "systems at scale.")},
                query="Software Engineer with search systems experience in the Bay Area",
                diagnosis="weak_quality", roots=(root,))

        self.assertEqual(cards[0]["quality"], "seed")
        self.assertEqual(cards[0]["failure_mode"], "exhausted")
        self.assertIn("search systems", cards[0]["chain"][0]["next_query"])
        self.assertFalse(any(card.get("source") == str(run / "results.json") for card in cards))

    def test_explicitly_reviewed_move_becomes_precedent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            run.mkdir()
            (run / "results.json").write_text(json.dumps({
                "title": "Synthetic Platform Operator",
                "brief": {
                    "occupation": "platform operations",
                    "defining_capability": (
                        "Operate customer-facing platform workflows, diagnose failures, "
                        "and automate technical operations."),
                },
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
                brief={
                    "occupation": "platform operations",
                    "defining_capability": (
                        "Operate customer-facing platform workflows, diagnose failures, "
                        "and automate technical operations."),
                },
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
