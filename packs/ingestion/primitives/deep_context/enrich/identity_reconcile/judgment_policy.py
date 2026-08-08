"""Deterministic identity verdict and threshold policy."""

from __future__ import annotations

from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import (
    DECISIVE_CONFIRM_THRESHOLD,
    IDENTITY_THRESHOLDS,
    IdentityOrigin,
)
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    ResearchReject,
)
from packs.ingestion.primitives.deep_context.enrich.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)

NO_PROFILE_REASON = "no usable LinkedIn profile"
VERDICTS = ("confirmed", "wrong_person", "needs_review")


@dataclass(frozen=True)
class IdentityAction:
    action: str
    via: str = ""


def threshold_for(origin: IdentityOrigin) -> float:
    """Confirm bar: 0.80 for research, 0.70 for attached.

    Research starts from a name/employer guess with no prior link to the
    person; attached evidence is already anchored to an imported identifier,
    so it needs less additional corroboration to confirm.
    """
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
) -> IdentityVerdict:
    return IdentityVerdict.from_payload({
        "verdict": value,
        "confidence": confidence,
        "supporting_evidence": list(supporting),
        "contradicting_evidence": list(contradicting),
        "linkedin_plausibly_absent": plausibly_absent,
        "recommend_deep_research": False,
        "reason": reason,
    })


def deterministic_identity(
    evidence: DossierEvidence,
    profile: JudgeProfile,
    origin: IdentityOrigin,
) -> IdentityVerdict:
    """Preserve the existing no-LLM behavior behind the shared judge.

    Runs instead of a paid call in --no-llm/offline mode. Research below
    research_proposal_min (0.50) is rejected outright — a low-confidence
    provider guess isn't even worth a human queueing decision. An attached
    profile is trusted at 0.9 confidence: enough to clear attached_confirm
    (0.70) so the offline stub still exercises the confirm path, but below
    decisive (0.95) so it never auto-wins a sibling conflict on its own.
    """
    del evidence
    if origin == IdentityOrigin.RESEARCH:
        confidence = profile.research_confidence
        if (
            profile.research_unverified
            or confidence < IDENTITY_THRESHOLDS["research_proposal_min"]
        ):
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
    if not profile.has_profile:
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
    verdict: IdentityVerdict,
    confirm_threshold: float | None = None,
) -> ResearchReject:
    """Project a verdict into the legacy llm_reject "yes"/"" string-boolean shape."""
    confidence = verdict.confidence
    threshold = confirm_threshold or threshold_for(IdentityOrigin.RESEARCH)
    if verdict.value.lower() == "confirmed" and confidence >= threshold:
        return ResearchReject("", "", "", f"{confidence:.3f}")
    return ResearchReject(
        "yes",
        f"{confidence:.3f}",
        verdict.reason or "deep-research proposal not corroborated by the dossier",
        "",
    )


def decide_actions(
    tasks: list[IdentityTask],
    confirm: float | None = None,
    detach: float | None = None,
    *,
    origin: IdentityOrigin = IdentityOrigin.ATTACHED,
) -> tuple[IdentityAction, ...]:
    """Return pinned keep-biased actions without mutating orchestration tasks.

    detach (0.85) sits above every confirm threshold on purpose: detaching an
    attached identity is destructive and harder to undo than keeping a
    "maybe", so removal demands more confidence than confirmation does.
    """
    thresholds = {
        "confirmed": confirm or threshold_for(origin),
        "wrong_person": detach or IDENTITY_THRESHOLDS["detach"],
    }

    def clears(task: IdentityTask, verdict: str) -> bool:
        return bool(
            task.verdict
            and task.verdict.value == verdict
            and task.verdict.confidence >= thresholds[verdict]
        )

    groups: dict[str, list[int]] = {}
    decisions = [IdentityAction("review") for _ in tasks]
    for index, task in enumerate(tasks):
        group_key = task.parent_id or task.parent_slug
        groups.setdefault(group_key, []).append(index)
    for group in groups.values():
        if len(group) == 1:
            index = group[0]
            task = tasks[index]
            if clears(task, "confirmed"):
                decisions[index] = IdentityAction("confirm", "normal")
            elif clears(task, "wrong_person"):
                decisions[index] = IdentityAction("detach", "normal")
            continue
        confirmed = [index for index in group if clears(tasks[index], "confirmed")]
        wrong = [index for index in group if clears(tasks[index], "wrong_person")]
        for index in wrong:
            decisions[index] = IdentityAction("detach", "normal")
        # A sibling conflict (one parent, several candidate links) only
        # auto-resolves when it can't be a coin flip: either the sole
        # confirmed candidate clears decisive (0.95) outright, or every other
        # sibling has independently cleared the wrong_person/detach bar.
        # Anything short of that stays "review" — the keep-biased default set
        # above the loop.
        decisive = (
            confirmed
            and tasks[confirmed[0]].verdict is not None
            and tasks[confirmed[0]].verdict.confidence >= DECISIVE_CONFIRM_THRESHOLD
        )
        if len(confirmed) == 1 and (decisive or len(wrong) == len(group) - 1):
            winner = confirmed[0]
            for index in group:
                decisions[index] = IdentityAction(
                    "confirm" if index == winner else "detach",
                    "conflict_resolved",
                )
    return tuple(decisions)
