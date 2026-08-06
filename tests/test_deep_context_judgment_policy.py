"""Identity action policy returns values without mutating judge tasks."""

from __future__ import annotations

import copy
import unittest

from packs.ingestion.primitives.deep_context.identity_reconcile.judgment_policy import (
    IdentityAction,
    decide_actions,
)


class IdentityJudgmentPolicyTest(unittest.TestCase):
    def test_conflict_actions_are_typed_and_input_tasks_stay_unchanged(self) -> None:
        tasks = [
            {
                "parent_id": "family",
                "candidate_key": "winner",
                "verdict": {"verdict": "confirmed", "confidence": 0.96},
            },
            {
                "parent_id": "family",
                "candidate_key": "sibling",
                "verdict": {"verdict": "needs_review", "confidence": 0.2},
            },
        ]
        original = copy.deepcopy(tasks)

        actions = decide_actions(tasks)

        self.assertEqual(
            actions,
            (
                IdentityAction("confirm", "conflict_resolved"),
                IdentityAction("detach", "conflict_resolved"),
            ),
        )
        self.assertEqual(tasks, original)


if __name__ == "__main__":
    unittest.main()
