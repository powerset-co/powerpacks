"""Identity action policy returns values without mutating judge tasks."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judgment_policy import (
    IdentityAction,
    decide_actions,
    deep_research_eligible,
    research_reject_fields,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
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

        decision = decide_actions(tasks)

        self.assertEqual(
            decision.actions,
            (
                IdentityAction("confirm", "conflict_resolved", "verify", "auto"),
                IdentityAction("detach", "conflict_resolved", "detach", "auto"),
            ),
        )
        self.assertEqual(tuple(tasks), original)

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

    def test_research_reject_fields_zero_confirm_threshold_is_not_defaulted(self) -> None:
        verdict = IdentityVerdict.from_payload({"verdict": "confirmed", "confidence": 0.1})

        rejection = research_reject_fields(verdict, confirm_threshold=0.0)

        # 0.1 clears an explicit 0.0 bar, so this must read as accepted (no
        # llm_reject) — a truthy `or` would have defaulted the threshold back
        # up to research_confirm (0.80) and rejected it instead.
        self.assertEqual(rejection.llm_reject, "")


if __name__ == "__main__":
    unittest.main()
