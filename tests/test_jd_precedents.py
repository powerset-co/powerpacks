from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import decompose_jd, precedents


class JdPrecedentTests(unittest.TestCase):
    def test_collections_and_experts_are_filtered_before_ranking(self):
        jd = "Build and operate production search infrastructure at scale."
        plan = {"job_title": "Search Engineer", "normalized_archetype": "search engineer"}
        signature = {"job": "Search Engineer", "family": "search engineer",
                     "defining_capability": jd, "excludes": ""}
        policy = {
            "move_cards": [{**signature, "reason": "pond-only"}],
            "trait_cards": [{**signature, "reason": "trait-only"}],
            "taste_cards": [
                {**signature, "dimension": "company_taste", "reason": "company-only"},
                {**signature, "dimension": "role_fit", "reason": "role-only"},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "cards.json"
            path.write_text(json.dumps(policy))
            with mock.patch.object(precedents, "SEED_PATH", path):
                for collection, dimension, expected in (
                    ("pond", None, "pond-only"), ("traits", None, "trait-only"),
                    ("taste", precedents.FitDimension.ROLE_FIT, "role-only"),
                    ("taste", precedents.FitDimension.COMPANY_TASTE, "company-only"),
                ):
                    with self.subTest(collection=collection, dimension=dimension):
                        cards = precedents.retrieve_jd_precedents(
                            jd, plan, collection=collection, dimension=dimension, limit=1)
                        self.assertEqual([card["reason"] for card in cards], [expected])

    def test_empty_trait_and_taste_collections_do_not_fall_back_to_pond(self):
        jd = "Build and operate crawling, indexing, ranking, and retrieval systems at scale."
        plan = {"job_title": "Search Engineer",
                "normalized_archetype": "search infrastructure engineering"}
        policy = {"move_cards": [{"job": "Search Engineer", "family": "search engineering",
                                  "defining_capability": jd, "reason": "Pond only"}],
                  "trait_cards": [], "taste_cards": []}
        with mock.patch.object(precedents, "_read", return_value=policy):
            self.assertTrue(precedents.retrieve_jd_precedents(jd, plan, collection="pond"))
            self.assertEqual(precedents.retrieve_jd_precedents(jd, plan, collection="traits"), [])
            self.assertEqual(precedents.retrieve_jd_precedents(
                jd, plan, collection="taste", dimension=precedents.FitDimension.ROLE_FIT), [])

    def test_pond_retrieves_from_jd_before_traits_exist(self):
        jd = (
            "Executive Assistant\nResponsibilities\n"
            "Own a demanding principal’s calendar, travel, inbox, personal logistics, "
            "and stakeholder access in a high-velocity organization."
        )
        plan = {"job_title": "Executive Assistant",
                "normalized_archetype": "executive support", "traits": []}
        cards = decompose_jd.retrieve_precedent_cards(jd, plan)
        self.assertEqual(cards[0]["job"], "Executive Assistant")

    def test_shared_retrieval_does_not_change_with_traits(self):
        jd = (
            "Build and operate crawling, indexing, ranking, and retrieval systems "
            "whose latency, cost, freshness, and correctness matter at scale."
        )
        plan = {"job_title": "Search Engineer",
                "normalized_archetype": "search infrastructure engineering", "traits": []}
        before = precedents.retrieve_jd_precedents(jd, plan, collection="pond")
        after = precedents.retrieve_jd_precedents(jd, {
            **plan, "traits": [{"kind": "capability", "trait": "Run paid marketing"}]},
            collection="pond")
        self.assertEqual(before, after)
        self.assertEqual(before[0]["job"], "Search Engineer")

    def test_inline_and_html_responsibilities_are_not_discarded(self):
        work = (
            "Build and operate crawling, indexing, ranking, and retrieval systems "
            "whose latency, cost, freshness, and correctness matter at scale."
        )
        plan = {"job_title": "Search Engineer",
                "normalized_archetype": "search infrastructure engineering"}
        for jd in (f"Responsibilities: {work}", f"<h2>Responsibilities</h2><p>{work}</p>"):
            with self.subTest(jd=jd):
                self.assertEqual(precedents.jd_brief(jd, plan)["defining_capability"], jd)
                self.assertEqual(precedents.retrieve_jd_precedents(jd, plan, collection="pond")[0]["job"],
                                 "Search Engineer")

    def test_title_alone_does_not_retrieve_a_work_card(self):
        self.assertEqual(precedents.retrieve_jd_precedents(
            "", {"job_title": "Search Engineer", "normalized_archetype": "search engineer"},
            collection="pond"), [])

    def test_same_title_unrelated_work_does_not_retrieve(self):
        self.assertEqual(precedents.retrieve_jd_precedents(
            "Lead cloud migrations, Kubernetes architecture, deployment strategy, "
            "technical sales, adoption consulting, and organizational change.",
            {"job_title": "Forward Deployed Engineer - Systems",
             "normalized_archetype": "customer-facing systems engineer"}, collection="pond"), [])


if __name__ == "__main__":
    unittest.main()
