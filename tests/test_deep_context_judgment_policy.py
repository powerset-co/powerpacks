"""Identity action policy returns values without mutating judge tasks."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judgment_policy import (
    IdentityAction,
    decide_actions,
    deep_research_eligible,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    CONNECTION_RULE,
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)


class IdentityJudgmentPolicyTest(unittest.TestCase):
    def test_verdict_confidence_must_be_a_probability(self) -> None:
        with self.assertRaises(ValueError):
            IdentityVerdict.from_payload({"verdict": "confirmed"})
        invalid = (None, "0.9", True, -0.01, 1.01, float("nan"), float("inf"))
        for confidence in invalid:
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                IdentityVerdict.from_payload(
                    {"verdict": "confirmed", "confidence": confidence}
                )

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

        decision = decide_actions(tasks)

        self.assertEqual(
            decision.actions,
            (
                IdentityAction("verify", "conflict_resolved"),
                IdentityAction("detach", "conflict_resolved"),
            ),
        )
        self.assertEqual(tuple(tasks), original)

    def test_connection_rule_remains_a_decisive_conflict_winner(self) -> None:
        tasks = [
            IdentityTask(
                parent_id="family",
                candidate_key="connection",
                rule=CONNECTION_RULE,
                evidence=DossierEvidence(name="Jordan Bravo"),
                linkedin=JudgeProfile(),
            ),
            IdentityTask(
                parent_id="family",
                candidate_key="sibling",
                evidence=DossierEvidence(name="Jordan Bravo"),
                linkedin=JudgeProfile(),
            ),
        ]

        self.assertEqual(
            decide_actions(tasks).actions,
            (
                IdentityAction("verify", "conflict_resolved"),
                IdentityAction("detach", "conflict_resolved"),
            ),
        )

    def test_zero_detach_threshold_resolves_to_zero_everywhere(self) -> None:
        """An explicit 0.0 must not collapse back to the origin default via a
        truthy `or`, and the exact same resolved bar must gate both the
        detach decision and deep-research eligibility — see
        judgment_policy.decide_actions/Decision/ResolvedThresholds."""
        task = IdentityTask(
            parent_id="solo",
            candidate_key="candidate",
            verdict=IdentityVerdict.from_payload({
                "verdict": "wrong_person",
                "confidence": 0.1,
                "recommend_deep_research": True,
            }),
            evidence=DossierEvidence(name="Jordan Bravo"),
            linkedin=JudgeProfile(),
        )

        decided = decide_actions([task], detach=0.0)

        self.assertEqual(decided.thresholds.detach, 0.0)
        self.assertEqual(decided.actions[0].action, "detach")
        self.assertTrue(deep_research_eligible(task, decided.thresholds))

if __name__ == "__main__":
    unittest.main()
