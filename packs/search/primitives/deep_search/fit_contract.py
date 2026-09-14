"""Typed contract for company-fit experts and taste cards."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping, NotRequired, Sequence, TypeAlias, TypedDict, cast


class FitDimension(StrEnum):
    ROLE_FIT = "role_fit"
    COMPANY_TASTE = "company_taste"
    CRAFT_AND_POTENTIAL = "craft_and_potential"
    MOVE_FEASIBILITY = "move_feasibility"
    FINAL_DECISION = "final_decision"


FIT_EXPERTS = tuple(dimension for dimension in FitDimension
                    if dimension is not FitDimension.FINAL_DECISION)


class RoleFitLabel(StrEnum):
    STRONG_FIT = "strong-fit"
    ADJACENT_FIT = "adjacent-fit"
    PROMISING_STEP_UP = "promising-step-up"
    JUNIOR_COULD_GROW = "junior-could-grow"
    TOO_SENIOR = "too-senior"
    WRONG_ROLE = "wrong-role"
    UNCLEAR = "unclear"


class CompanyTasteLabel(StrEnum):
    STRONG = "strong"
    NEUTRAL = "neutral"
    WEAK = "weak"
    UNCLEAR = "unclear"


class CraftPotentialLabel(StrEnum):
    EXCEPTIONAL = "exceptional"
    STRONG = "strong"
    PROMISING = "promising"
    UNCLEAR = "unclear"
    WEAK = "weak"


class MoveFeasibilityLabel(StrEnum):
    PLAUSIBLE = "plausible"
    COMP_STRETCH = "comp-stretch"
    COMP_MISMATCH = "comp-mismatch"
    WRONG_TIMING = "wrong-timing"
    DESTINATION_PULL = "destination-pull"
    FOUNDER_LOCK_IN = "founder-lock-in"
    UNCLEAR = "unclear"


class FitGroup(StrEnum):
    SEND_WORTHY = "send_worthy"
    CHAT_WORTHY = "chat_worthy"
    WRONG_TIMING_RELATIONSHIP = "wrong_timing_relationship"
    PASSED = "passed"


FIT_GROUPS = tuple(FitGroup)


class TraitStatus(StrEnum):
    DOING_NOW = "doing_now"
    EXPERIENCED = "experienced"
    CAPABLE = "capable"
    FOUNDATIONAL = "foundational"
    THIN = "thin"
    MISSING = "missing"
    UNKNOWN = "unknown"


# The panel's ladder: the role-fit expert scores each JD trait on it.
TRAIT_STATUS_VALUE = {
    TraitStatus.DOING_NOW: 0.95,
    TraitStatus.EXPERIENCED: 0.80,
    TraitStatus.CAPABLE: 0.70,
    TraitStatus.FOUNDATIONAL: 0.50,
    TraitStatus.THIN: 0.25,
    TraitStatus.MISSING: 0.0,
    TraitStatus.UNKNOWN: 0.0,
}
TRAIT_STATUS_NAMES = {
    TraitStatus.DOING_NOW: "Doing it now",
    TraitStatus.EXPERIENCED: "Experienced",
    TraitStatus.CAPABLE: "Capable",
    TraitStatus.FOUNDATIONAL: "Foundational",
    TraitStatus.THIN: "Thin",
    TraitStatus.MISSING: "No evidence",
    TraitStatus.UNKNOWN: "Not enough data",
}


def role_fit_coverage(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    statuses = [TraitStatus(str(row["status"])) for row in rows]
    satisfied = sum(status in {
        TraitStatus.DOING_NOW, TraitStatus.EXPERIENCED, TraitStatus.CAPABLE,
    } for status in statuses)
    foundational = sum(status is TraitStatus.FOUNDATIONAL for status in statuses)
    thin = sum(status is TraitStatus.THIN for status in statuses)
    fraction = (satisfied + foundational * 0.5 + thin * 0.25) / len(statuses)
    return round(fraction ** (2 / 3), 4)


FitLabel: TypeAlias = (
    RoleFitLabel | CompanyTasteLabel | CraftPotentialLabel | MoveFeasibilityLabel
)
FIT_LABEL_ENUMS = {
    FitDimension.ROLE_FIT: RoleFitLabel,
    FitDimension.COMPANY_TASTE: CompanyTasteLabel,
    FitDimension.CRAFT_AND_POTENTIAL: CraftPotentialLabel,
    FitDimension.MOVE_FEASIBILITY: MoveFeasibilityLabel,
}

FIT_DIMENSION_NAMES = {
    FitDimension.ROLE_FIT: "Role fit",
    FitDimension.COMPANY_TASTE: "Company taste",
    FitDimension.CRAFT_AND_POTENTIAL: "Craft/potential",
    FitDimension.MOVE_FEASIBILITY: "Move feasibility",
    FitDimension.FINAL_DECISION: "Final decision",
}
FIT_LABEL_NAMES = {
    FitDimension.ROLE_FIT: {
        RoleFitLabel.STRONG_FIT: "Strong fit",
        RoleFitLabel.ADJACENT_FIT: "Adjacent fit",
        RoleFitLabel.PROMISING_STEP_UP: "Plausible step-up",
        RoleFitLabel.JUNIOR_COULD_GROW: "Junior, could grow",
        RoleFitLabel.TOO_SENIOR: "Too senior",
        RoleFitLabel.WRONG_ROLE: "Wrong role",
        RoleFitLabel.UNCLEAR: "Not enough data",
    },
    FitDimension.COMPANY_TASTE: {
        CompanyTasteLabel.STRONG: "Strong company signal",
        CompanyTasteLabel.NEUTRAL: "Neutral company signal",
        CompanyTasteLabel.WEAK: "Weak company signal",
        CompanyTasteLabel.UNCLEAR: "Not enough data",
    },
    FitDimension.CRAFT_AND_POTENTIAL: {
        CraftPotentialLabel.EXCEPTIONAL: "Exceptional craft",
        CraftPotentialLabel.STRONG: "Strong craft",
        CraftPotentialLabel.PROMISING: "Promising potential",
        CraftPotentialLabel.UNCLEAR: "Not enough data",
        CraftPotentialLabel.WEAK: "Weak craft",
    },
    FitDimension.MOVE_FEASIBILITY: {
        MoveFeasibilityLabel.PLAUSIBLE: "Plausible now",
        MoveFeasibilityLabel.COMP_STRETCH: "Compensation stretch",
        MoveFeasibilityLabel.COMP_MISMATCH: "Compensation mismatch",
        MoveFeasibilityLabel.WRONG_TIMING: "Wrong timing",
        MoveFeasibilityLabel.DESTINATION_PULL: "Strong destination pull",
        MoveFeasibilityLabel.FOUNDER_LOCK_IN: "Founder lock-in",
        MoveFeasibilityLabel.UNCLEAR: "Not enough data",
    },
}

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
FitContext: TypeAlias = str | list[JsonValue] | dict[str, JsonValue]


class ExpertJudgment(TypedDict):
    label: FitLabel


class FinalDecisionJudgment(TypedDict):
    group: FitGroup


class FitCard(TypedDict):
    id: str
    dimension: FitDimension
    jd_context: FitContext
    candidate_context: FitContext
    judgment: ExpertJudgment | FinalDecisionJudgment
    excludes: FitContext
    reason: str
    source: NotRequired[str]
    source_jd: NotRequired[str]
    source_person: NotRequired[str]
    quality: NotRequired[str]
    quality_tier: NotRequired[int]
    retrieval_score: NotRequired[float]
    retrieval_evidence: NotRequired[dict[str, float]]


def parse_fit_dimension(value: object) -> FitDimension:
    try:
        return FitDimension(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown fit dimension: {value}") from exc


def parse_fit_label(dimension: FitDimension, value: object) -> FitLabel:
    label_enum = FIT_LABEL_ENUMS.get(dimension)
    if label_enum is None:
        raise ValueError(f"{dimension.value} has no expert labels")
    try:
        return label_enum(str(value))
    except ValueError as exc:
        raise ValueError(f"{dimension.value} judgment has an invalid label: {value}") from exc


def fit_label_values(dimension: FitDimension) -> tuple[str, ...]:
    label_enum = FIT_LABEL_ENUMS[dimension]
    return tuple(label.value for label in label_enum)


def fit_judgment_field(dimension: FitDimension) -> str:
    return "group" if dimension is FitDimension.FINAL_DECISION else "label"


def fit_judgment_values(dimension: FitDimension) -> tuple[str, ...]:
    if dimension is FitDimension.FINAL_DECISION:
        return tuple(group.value for group in FitGroup)
    return fit_label_values(dimension)


def fit_label_name(dimension: FitDimension, value: object) -> str:
    label = parse_fit_label(dimension, value)
    return FIT_LABEL_NAMES[dimension][label]


def _context(value: object, field: str, *, empty: bool = False) -> FitContext:
    if not isinstance(value, (str, list, dict)) or (not empty and not value):
        raise ValueError(f"fit card {field} must be non-empty JSON text, list, or object")
    return cast(FitContext, value)


def parse_fit_card(value: object) -> FitCard:
    if not isinstance(value, dict):
        raise ValueError("fit card must be an object")
    required = {"id", "dimension", "jd_context", "candidate_context", "judgment", "reason"}
    optional = {
        "excludes", "source", "source_jd", "source_person", "quality", "quality_tier",
        "retrieval_score", "retrieval_evidence",
    }
    if missing := required - value.keys():
        raise ValueError(f"fit card is missing fields: {', '.join(sorted(missing))}")
    if extra := value.keys() - required - optional:
        raise ValueError(f"fit card has unknown fields: {', '.join(sorted(extra))}")
    card_id = str(value["id"]).strip()
    reason = str(value["reason"]).strip()
    if not card_id or not reason:
        raise ValueError("fit card id and reason must be non-empty")
    dimension = parse_fit_dimension(value["dimension"])
    raw_judgment = value["judgment"]
    if not isinstance(raw_judgment, dict):
        raise ValueError(f"{dimension.value} judgment must be an object")
    if dimension is FitDimension.FINAL_DECISION:
        if set(raw_judgment) != {"group"}:
            raise ValueError("final_decision judgment has the wrong fields")
        try:
            judgment: ExpertJudgment | FinalDecisionJudgment = {
                "group": FitGroup(str(raw_judgment["group"]))
            }
        except ValueError as exc:
            raise ValueError("final_decision judgment has an invalid group") from exc
    else:
        if set(raw_judgment) != {"label"}:
            raise ValueError(f"{dimension.value} judgment has the wrong fields")
        judgment = {"label": parse_fit_label(dimension, raw_judgment["label"])}
    card: FitCard = {
        "id": card_id,
        "dimension": dimension,
        "jd_context": _context(value["jd_context"], "jd_context"),
        "candidate_context": _context(value["candidate_context"], "candidate_context"),
        "judgment": judgment,
        "excludes": _context(value.get("excludes", {}), "excludes", empty=True),
        "reason": reason,
    }
    for field in ("source", "source_jd", "source_person", "quality"):
        if field in value:
            card[field] = str(value[field])  # type: ignore[literal-required]
    if "quality_tier" in value:
        card["quality_tier"] = int(value["quality_tier"])
    if "retrieval_score" in value:
        card["retrieval_score"] = float(value["retrieval_score"])
    if "retrieval_evidence" in value:
        evidence = value["retrieval_evidence"]
        if not isinstance(evidence, dict):
            raise ValueError("fit card retrieval_evidence must be an object")
        card["retrieval_evidence"] = {
            str(key): float(score) for key, score in evidence.items()
        }
    return card
