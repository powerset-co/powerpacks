"""Build the fixed profile-capability feature vector from validated Jev answers.

Changelog:
  2026-09-25: 31 features from the seven profile-level questions. The per-position, tenure and
    company-share features are gone with the per-position questions; the two continuity x
    historical interactions stay.
"""

from __future__ import annotations

from typing import Any

# This order is the frozen model input order. A feature change requires a new model bundle.
FEATURE_NAMES = (
    "transfer::0",
    "transfer::1",
    "transfer::2",
    "transfer::3",
    "continuity::current",
    "continuity::none",
    "continuity::recent",
    "continuity::senior_adjacent",
    "continuity::stale_switch",
    "continuity::unknown",
    "independent_execution_quality",
    "evidence_basis::description",
    "evidence_basis::isolated_title",
    "evidence_basis::none",
    "evidence_basis::repeated_roles",
    "evidence_basis::summary",
    "company_quality::ordinary",
    "company_quality::strong",
    "company_quality::unknown",
    "company_quality::weak",
    "specialty::direct",
    "specialty::missing",
    "specialty::not_required",
    "specialty::transferable",
    "specialty::unknown",
    "historical_match::0",
    "historical_match::1",
    "historical_match::2",
    "historical_match::3",
    "stale_x_historical_direct",
    "stale_x_historical_adjacent",
)
QUESTION_ORDER = (
    "transfer",
    "continuity",
    "independent_execution_quality",
    "evidence_basis",
    "company_quality",
    "specialty",
    "historical_match",
)


def _probability(answers: dict[str, dict[str, Any]], name: str, option: str) -> float:
    return float(answers[name]["probabilities"][option])


def build_features(answers: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Return the 31 frozen features from validated Jev answers, in model order."""
    features: dict[str, float] = {}
    for name in QUESTION_ORDER:
        answer = answers[name]
        if answer["type"] == "noul":
            features[name] = float(answer["noul"])
            continue
        for option, probability in sorted(answer["probabilities"].items()):
            features[f"{name}::{option}"] = float(probability)
    stale = _probability(answers, "continuity", "stale_switch")
    features["stale_x_historical_direct"] = stale * _probability(answers, "historical_match", "3")
    features["stale_x_historical_adjacent"] = stale * _probability(answers, "historical_match", "2")

    if tuple(features) != FEATURE_NAMES:
        missing = sorted(set(FEATURE_NAMES) - set(features))
        extra = sorted(set(features) - set(FEATURE_NAMES))
        raise ValueError(f"Jev feature schema mismatch: missing={missing}, extra={extra}")
    return features
