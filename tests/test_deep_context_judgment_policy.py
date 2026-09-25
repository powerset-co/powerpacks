"""Identity verdicts parse only probability confidences."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityVerdict,
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


if __name__ == "__main__":
    unittest.main()
