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


if __name__ == "__main__":
    unittest.main()
