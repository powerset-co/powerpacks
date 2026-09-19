"""Recruiter presentation ratings shared by the viewer and labeling harness."""
import math
from typing import Any, Mapping

SCORE_SCALE = 5
RUBRIC = {
    1: "Clear no — Wrong role or clearly lacks required experience",
    2: "Lean no — Some relevant experience, but I wouldn’t share them",
    3: "Borderline — Concerns around fit, but worth sharing",
    4: "Yes — Qualified and relevant",
    5: "Strong yes — Particularly compelling",
}
SCORES = tuple(RUBRIC)
LEGACY_SCORES = {1: 1, 2: 2, 3: 2, 4: 2, 7: 3, 8: 4, 9: 5, 10: 5}


def score_1_to_5(score: float, *, score_type: str = "raw_yes_minus_no_logit") -> float:
    """Preserve native ratings; normalize Qwen margins from saved runs."""
    if score_type in ("expected_rating_1_to_5", "ordinal_rating_1_to_5"):
        return score
    if score >= 0:
        return 1 + 4 / (1 + math.exp(-score))
    exp_score = math.exp(score)
    return 1 + 4 * exp_score / (1 + exp_score)


def convert_rating(value: Mapping[str, Any]) -> dict[str, Any]:
    """Unmarked saved ratings used the old ten-point rubric; never infer by score."""
    score, scale = value.get("score"), value.get("scale", 10)
    if scale not in (5, 10):
        raise ValueError("Unknown rating scale")
    allowed = SCORES if scale == 5 else LEGACY_SCORES
    if score is not None and (type(score) is not int or score not in allowed):
        raise ValueError(f"Invalid score for the {scale}-point rubric")
    return {**value, "score": LEGACY_SCORES[score] if scale == 10 and score is not None else score,
            "scale": SCORE_SCALE}
