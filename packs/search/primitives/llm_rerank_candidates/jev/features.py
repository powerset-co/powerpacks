"""Build the fixed company-capability feature vector for the Jev pilot model."""

from __future__ import annotations

from typing import Any


RECENCY_BUCKETS = (
    "current",
    "ended_within_5years",
    "ended_5plus_years_ago",
    "unknown",
)
QUALITY_TIERS = ("strong", "ordinary", "weak", "unknown")
EXCLUDED_ANSWERS = frozenset(("overall_rating", "school_signal"))

# This order is the frozen model input order, not an alphabetical convenience at
# prediction time. Keep changes explicit: a feature change requires a new model.
FEATURE_NAMES = (
    "company_current_relevant_strong",
    "company_current_strong",
    "company_current_strong_prior_weak",
    "company_dates_missing",
    "company_domain::0",
    "company_domain::1",
    "company_domain::2",
    "company_domain::3",
    "company_quality::ordinary",
    "company_quality::strong",
    "company_quality::unknown",
    "company_quality::weak",
    "company_relevant_ordinary",
    "company_relevant_strong",
    "company_relevant_unknown",
    "company_relevant_weak",
    "company_role_year_share_ordinary",
    "company_role_year_share_strong",
    "company_role_year_share_unknown",
    "company_role_year_share_weak",
    "continuity::current",
    "continuity::none",
    "continuity::recent",
    "continuity::senior_adjacent",
    "continuity::stale_switch",
    "continuity::unknown",
    "coverage::0",
    "coverage::1",
    "coverage::2",
    "coverage::3",
    "coverage::4",
    "direct_execution",
    "direct_x_continuity",
    "education_relevance::relevant",
    "education_relevance::unknown",
    "education_relevance::unrelated",
    "environment_fit::comparable",
    "environment_fit::mismatch",
    "environment_fit::transferable",
    "environment_fit::unknown",
    "evidence_basis::description",
    "evidence_basis::isolated_title",
    "evidence_basis::none",
    "evidence_basis::repeated_roles",
    "evidence_basis::summary",
    "function_match::0",
    "function_match::1",
    "function_match::2",
    "function_match::3",
    "function_x_company_quality",
    "funding_context::adverse",
    "funding_context::supported",
    "funding_context::unknown",
    "historical_match::0",
    "historical_match::1",
    "historical_match::2",
    "historical_match::3",
    "independent_execution_quality",
    "relevant_leadership",
    "repeated_practice",
    "role_execution_current",
    "role_execution_ended_5plus_years_ago",
    "role_execution_ended_within_5years",
    "role_execution_unknown",
    "role_match_current",
    "role_match_ended_5plus_years_ago",
    "role_match_ended_within_5years",
    "role_match_max",
    "role_match_unknown",
    "role_repeated_support",
    "role_substantive_max",
    "scope::0",
    "scope::1",
    "scope::2",
    "scope::3",
    "specialty::direct",
    "specialty::missing",
    "specialty::not_required",
    "specialty::transferable",
    "specialty::unknown",
    "stale_x_historical_adjacent",
    "stale_x_historical_direct",
    "transfer::0",
    "transfer::1",
    "transfer::2",
    "transfer::3",
    "wrong_function",
)


def _answer_features(answers: dict[str, dict[str, Any]]) -> dict[str, float]:
    result = {}
    for name, answer in answers.items():
        if name in EXCLUDED_ANSWERS or name.startswith("role_"):
            continue
        if answer["type"] == "noul":
            result[name] = float(answer["noul"])
            continue
        for option, probability in sorted(answer["probabilities"].items()):
            result[f"{name}::{option}"] = float(probability)
    return result


def _probability(answers: dict[str, dict[str, Any]], name: str, option: str) -> float:
    return float(answers[name]["probabilities"][option])


def _average_score(answers: dict[str, dict[str, Any]], name: str) -> float:
    probabilities = answers[name]["probabilities"]
    return sum(int(option) * float(value) for option, value in probabilities.items()) / (len(probabilities) - 1)


def _role_evidence(roles: list[dict[str, Any]], answers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "match": float(answers[f"role_{index}_function"]["noul"]),
            "execution": float(answers[f"role_{index}_execution"]["noul"]),
            "quality": {
                tier: float(value) for tier, value in answers[f"role_{index}_quality"]["probabilities"].items()
            },
            "dates": role["dates"],
        }
        for index, role in enumerate(roles)
    ]


def _add_role_features(features: dict[str, float], roles: list[dict[str, Any]]) -> None:
    total_match = sum(role["match"] for role in roles)
    features["role_match_max"] = max((role["match"] for role in roles), default=0.0)
    features["role_substantive_max"] = max((role["match"] * role["execution"] for role in roles), default=0.0)
    features["role_repeated_support"] = min(2.0, total_match) / 2.0

    for recency in RECENCY_BUCKETS:
        matching = [role for role in roles if role["dates"]["recency"] == recency]
        features[f"role_match_{recency}"] = max((role["match"] for role in matching), default=0.0)
        features[f"role_execution_{recency}"] = max(
            (role["match"] * role["execution"] for role in matching), default=0.0
        )

    for tier in QUALITY_TIERS:
        features[f"company_relevant_{tier}"] = sum(role["match"] * role["quality"][tier] for role in roles) / max(
            total_match, 1e-9
        )


def _add_tenure_features(features: dict[str, float], roles: list[dict[str, Any]]) -> None:
    known = [role for role in roles if role["dates"]["years_in_role"] is not None]
    years = sum(role["dates"]["years_in_role"] for role in known)
    features["company_dates_missing"] = float(not known)
    for tier in QUALITY_TIERS:
        features[f"company_role_year_share_{tier}"] = sum(
            role["dates"]["years_in_role"] * role["quality"][tier] for role in known
        ) / max(years, 1)

    current = [role for role in roles if role["dates"]["recency"] == "current"]
    features["company_current_strong"] = max((role["quality"]["strong"] for role in current), default=0.0)
    features["company_current_relevant_strong"] = max(
        (role["quality"]["strong"] * role["match"] for role in current), default=0.0
    )
    features["company_current_strong_prior_weak"] = (
        features["company_current_relevant_strong"] * features["company_role_year_share_weak"]
    )


def _add_interactions(features: dict[str, float], answers: dict[str, dict[str, Any]]) -> None:
    continuity = sum(_probability(answers, "continuity", option) for option in ("current", "recent", "senior_adjacent"))
    features["direct_x_continuity"] = float(answers["direct_execution"]["noul"]) * continuity
    features["function_x_company_quality"] = _average_score(answers, "function_match") * _probability(
        answers, "company_quality", "strong"
    )
    features["stale_x_historical_direct"] = _probability(answers, "continuity", "stale_switch") * _probability(
        answers, "historical_match", "3"
    )
    features["stale_x_historical_adjacent"] = _probability(answers, "continuity", "stale_switch") * _probability(
        answers, "historical_match", "2"
    )


def build_features(roles: list[dict[str, Any]], answers: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Return the 87 frozen non-school features from validated Jev answers."""
    features = _answer_features(answers)
    role_evidence = _role_evidence(roles, answers)
    _add_role_features(features, role_evidence)
    _add_tenure_features(features, role_evidence)
    _add_interactions(features, answers)

    if set(features) != set(FEATURE_NAMES):
        missing = sorted(set(FEATURE_NAMES) - set(features))
        extra = sorted(set(features) - set(FEATURE_NAMES))
        raise ValueError(f"Jev feature schema mismatch: missing={missing}, extra={extra}")
    return features
