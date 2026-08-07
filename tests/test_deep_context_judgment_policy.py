"""Identity action policy returns values without mutating judge tasks."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.identity_reconcile.judgment_policy import (
    IdentityAction,
    decide_actions,
)
from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)


class IdentityJudgmentPolicyTest(unittest.TestCase):
    def test_conflict_actions_are_typed_and_input_tasks_stay_unchanged(self) -> None:
        tasks = [IdentityTask(
            parent_id="family",
            candidate_key=key,
            verdict=IdentityVerdict.from_payload({
                "verdict": verdict,
                "confidence": confidence,
            }),
            evidence=DossierEvidence(name="Jordan Bravo"),
            linkedin=JudgeProfile(),
        ) for key, verdict, confidence in (
            ("winner", "confirmed", 0.96),
            ("sibling", "needs_review", 0.2),
        )]
        original = tuple(tasks)

        actions = decide_actions(tasks)

        self.assertEqual(
            actions,
            (
                IdentityAction("confirm", "conflict_resolved"),
                IdentityAction("detach", "conflict_resolved"),
            ),
        )
        self.assertEqual(tuple(tasks), original)


if __name__ == "__main__":
    unittest.main()
