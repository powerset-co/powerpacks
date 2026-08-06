"""Deterministic identity verdict and threshold policy."""

from __future__ import annotations

from typing import Any

from packs.ingestion.primitives.deep_context.db.models import (
    DECISIVE_CONFIRM_THRESHOLD,
    IDENTITY_THRESHOLDS,
    IdentityOrigin,
)
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence

NO_PROFILE_REASON = "no usable LinkedIn profile"
VERDICTS = ("confirmed", "wrong_person", "needs_review")


def threshold_for(origin: IdentityOrigin) -> float:
    key = "research_confirm" if origin == IdentityOrigin.RESEARCH else "attached_confirm"
    return IDENTITY_THRESHOLDS[key]


def _verdict(
    value: str,
    confidence: float,
    reason: str,
    *,
    supporting: tuple[str, ...] = (),
    contradicting: tuple[str, ...] = (),
    plausibly_absent: bool = False,
) -> dict[str, Any]:
    return {
        "verdict": value,
        "confidence": confidence,
        "supporting_evidence": list(supporting),
        "contradicting_evidence": list(contradicting),
        "linkedin_plausibly_absent": plausibly_absent,
        "recommend_deep_research": False,
        "reason": reason,
    }


def deterministic_identity(
    evidence: DossierEvidence,
    profile: dict[str, Any],
    origin: IdentityOrigin,
) -> dict[str, Any]:
    """Preserve the existing no-LLM behavior behind the shared judge."""
    del evidence
    if origin == IdentityOrigin.RESEARCH:
        confidence = float(profile.get("_research_confidence") or 0)
        if profile.get("_research_unverified") or confidence < 0.5:
            return _verdict(
                "wrong_person",
                0.0,
                "deep-research guess is unverified",
                contradicting=("unverified deep-research proposal",),
            )
        return _verdict(
            "needs_review",
            0.0,
            "speculative deep-research proposal needs the evidence judge",
        )
    if not profile.get("has_profile"):
        return _verdict(
            "needs_review", 0.0, NO_PROFILE_REASON, plausibly_absent=True
        )
    return _verdict(
        "confirmed",
        0.9,
        "offline stub trusts the attached profile",
        supporting=("attached profile (offline stub)",),
    )


def research_reject_fields(
    verdict: dict[str, Any],
    confirm_threshold: float | None = None,
) -> dict[str, str]:
    confidence = float(verdict.get("confidence") or 0)
    threshold = confirm_threshold or threshold_for(IdentityOrigin.RESEARCH)
    if str(verdict.get("verdict") or "").lower() == "confirmed" and confidence >= threshold:
        return {
            "llm_reject": "",
            "llm_reject_confidence": "",
            "llm_reject_reason": "",
            "confidence": f"{confidence:.3f}",
        }
    return {
        "llm_reject": "yes",
        "llm_reject_confidence": f"{confidence:.3f}",
        "llm_reject_reason": str(
            verdict.get("reason")
            or "deep-research proposal not corroborated by the dossier"
        ),
    }


def decide_actions(
    tasks: list[dict[str, Any]],
    confirm: float | None = None,
    detach: float | None = None,
    *,
    origin: IdentityOrigin = IdentityOrigin.ATTACHED,
) -> None:
    """Apply the pinned keep-biased thresholds, including conflict handling."""
    thresholds = {
        "confirmed": confirm or threshold_for(origin),
        "wrong_person": detach or IDENTITY_THRESHOLDS["detach"],
    }

    def clears(task: dict[str, Any], verdict: str) -> bool:
        result = task.get("verdict") or {}
        return result.get("verdict") == verdict and float(
            result.get("confidence") or 0
        ) >= thresholds[verdict]

    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        task["action"], task["via"] = "review", ""
        group_key = str(task.get("parent_id") or task.get("parent_slug") or "")
        groups.setdefault(group_key, []).append(task)
    for group in groups.values():
        if len(group) == 1:
            task = group[0]
            if clears(task, "confirmed"):
                task["action"], task["via"] = "confirm", "normal"
            elif clears(task, "wrong_person"):
                task["action"], task["via"] = "detach", "normal"
            continue
        confirmed = [task for task in group if clears(task, "confirmed")]
        wrong = [task for task in group if clears(task, "wrong_person")]
        decisive = (
            confirmed
            and float(confirmed[0]["verdict"].get("confidence") or 0)
            >= DECISIVE_CONFIRM_THRESHOLD
        )
        if len(confirmed) == 1 and (decisive or len(wrong) == len(group) - 1):
            winner = confirmed[0]
            for task in group:
                task["action"] = "confirm" if task is winner else "detach"
                task["via"] = "conflict_resolved"
