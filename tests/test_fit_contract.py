from __future__ import annotations

import json
import unittest

from packs.search.primitives.deep_search.fit_contract import (
    CompanyTasteLabel,
    CraftPotentialLabel,
    FitDimension,
    FitGroup,
    MoveFeasibilityLabel,
    RoleFitLabel,
    parse_fit_card,
)


class FitContractTests(unittest.TestCase):
    def test_dimensions_and_labels_are_closed_typed_values(self) -> None:
        self.assertEqual(
            [value.value for value in FitDimension],
            [
                "role_fit",
                "company_taste",
                "craft_and_potential",
                "move_feasibility",
                "final_decision",
            ],
        )
        self.assertEqual(
            [value.value for value in RoleFitLabel],
            [
                "strong-fit",
                "adjacent-fit",
                "promising-step-up",
                "junior-could-grow",
                "too-senior",
                "wrong-role",
                "unclear",
            ],
        )
        self.assertEqual(
            [value.value for value in CompanyTasteLabel],
            ["strong", "neutral", "weak", "unclear"],
        )
        self.assertEqual(
            [value.value for value in CraftPotentialLabel],
            ["exceptional", "strong", "promising", "unclear", "weak"],
        )
        self.assertEqual(
            [value.value for value in MoveFeasibilityLabel],
            [
                "plausible",
                "comp-stretch",
                "comp-mismatch",
                "wrong-timing",
                "destination-pull",
                "founder-lock-in",
                "unclear",
            ],
        )

    def test_expert_card_accepts_structured_job_and_candidate_context(self) -> None:
        card = parse_fit_card({
            "id": "role:payments-operator",
            "dimension": "role_fit",
            "jd_context": {
                "role_family": "operations",
                "target_level": "director",
                "company_vertical": "fintech",
                "traits": ["transaction scale", "team leadership"],
            },
            "candidate_context": {
                "roles": ["payments operations"],
                "company_verticals": ["banking", "fintech"],
                "signals": {"transactions_managed": "high", "promotions": 2},
            },
            "judgment": {"label": "strong-fit"},
            "excludes": {"candidate_context": {"roles": ["sales operations"]}},
            "reason": "The candidate has owned the defining work at the required scale.",
        })

        self.assertIs(card["dimension"], FitDimension.ROLE_FIT)
        self.assertIs(card["judgment"]["label"], RoleFitLabel.STRONG_FIT)
        self.assertEqual(json.loads(json.dumps(card))["judgment"]["label"], "strong-fit")

    def test_final_decision_card_has_a_typed_group(self) -> None:
        card = parse_fit_card({
            "id": "decision:qualified-but-early",
            "dimension": "final_decision",
            "jd_context": {"role_family": "engineering"},
            "candidate_context": {"signals": ["recent move"]},
            "judgment": {"group": "wrong_timing_relationship"},
            "excludes": {},
            "reason": "Qualified candidate, but the move timing is poor.",
        })

        self.assertIs(card["dimension"], FitDimension.FINAL_DECISION)
        self.assertIs(card["judgment"]["group"], FitGroup.WRONG_TIMING_RELATIONSHIP)

    def test_rejects_a_label_owned_by_another_dimension(self) -> None:
        with self.assertRaisesRegex(ValueError, "company_taste judgment has an invalid label"):
            parse_fit_card({
                "id": "company:bad-label",
                "dimension": "company_taste",
                "jd_context": {"role_family": "design"},
                "candidate_context": {"company": "Example"},
                "judgment": {"label": "strong-fit"},
                "excludes": {},
                "reason": "Invalid cross-dimension label.",
            })

    def test_rejects_arbitrary_judgment_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "final_decision judgment has the wrong fields"):
            parse_fit_card({
                "id": "decision:bad-shape",
                "dimension": "final_decision",
                "jd_context": {"role_family": "design"},
                "candidate_context": {"company": "Example"},
                "judgment": {"label": "strong", "group": "send_worthy"},
                "excludes": {},
                "reason": "Invalid judgment shape.",
            })


if __name__ == "__main__":
    unittest.main()
