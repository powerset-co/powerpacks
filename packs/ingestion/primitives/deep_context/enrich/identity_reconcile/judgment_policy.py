"""Deterministic identity verdict and threshold policy."""

from __future__ import annotations

import json
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.models import (
    DECISIVE_CONFIRM_THRESHOLD,
    IDENTITY_THRESHOLDS,
    IdentityOrigin,
)
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    ResearchReject,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)

NO_PROFILE_REASON = "no usable LinkedIn profile"
# The complete set of answers the judge can give. IdentityVerdict does not
# validate against it — `from_payload` takes whatever string the provider put in
# "verdict", including "" — so anything read back out of the store is checked
# here before it is trusted.
VERDICTS = ("confirmed", "wrong_person", "needs_review")


@dataclass(frozen=True)
class StoredJudgment:
    """A verdict already paid for, with the exact judge input it answered.

    The fingerprint is what makes the verdict reusable rather than merely
    present: it identifies the evidence, prompt, model and effort the judge was
    shown. Equal fingerprint means asking again would ask the same question.
    """

    verdict: IdentityVerdict
    fingerprint: str


def stored_judgments(db: Db) -> dict[str, StoredJudgment]:
    """Every candidate's persisted verdict, keyed by candidate_key.

    Malformed JSON, a non-object payload, or an empty verdict value is skipped
    silently rather than raised — that row simply doesn't appear here, so a
    caller treats it as unjudged (``reapply`` leaves it alone; the judge pays
    for it) instead of acting on a verdict nobody can read.
    """
    judgments: dict[str, StoredJudgment] = {}
    for link in links(db):
        try:
            verdict = json.loads(link.judgment_payload_json or "")
        except json.JSONDecodeError:
            continue
        try:
            parsed = IdentityVerdict.from_payload(verdict)
        except TypeError:
            continue
        if parsed.value:
            judgments[link.row_key] = StoredJudgment(parsed, str(link.judgment_fingerprint or ""))
    return judgments


def reuses_stored_verdict(
    stored: StoredJudgment | None,
    current_fingerprint: str,
    *,
    force: bool,
) -> bool:
    """Whether the verdict on file answers exactly this input, so skip paying.

    Three questions, and all three have to be yes:
      1. did the caller demand a fresh judgment (--force)?
      2. is there a verdict on file that means something?
      3. was it produced from this exact input?

    (2) is not paranoia about a shape that cannot occur. A stored verdict is
    whatever a provider once returned, and `IdentityVerdict.from_payload`
    accepts an absent "verdict" key as `value=""`. Reusing one of those would
    pin the row permanently: the fingerprint keeps matching every run, so the
    judge is never asked again and the row never resolves. Unreadable means
    judge, the same rule the rest of this stage follows.

    No check that the stored fingerprint is non-empty is needed — a row that was
    never judged holds "", and "" never equals a sha256 hex digest.
    """
    if force or stored is None:
        return False
    if stored.verdict.value not in VERDICTS:
        return False
    return stored.fingerprint == current_fingerprint


@dataclass(frozen=True)
class IdentityAction:
    action: str
    via: str = ""
    # The settlement decide_actions already worked out for this action — see
    # settled_machine_action. write_overrides translates task.action back
    # through that same function rather than reading these directly (it only
    # ever receives IdentityTask, not IdentityAction), but any caller that
    # does hold the IdentityAction can read the settlement straight off it.
    machine_action: str = ""
    approved: str | None = None


@dataclass(frozen=True)
class ResolvedThresholds:
    """The confirm/detach bars ``decide_actions`` actually applied.

    Every consumer of a threshold that gated a decision reads it from here —
    never re-resolves ``confirm``/``detach`` a second time. That second
    resolution is exactly how one CLI flag once produced two different detach
    bars in the same run: decide_actions collapsed an explicit 0.0 back to
    its default via ``or``, while a sibling inline check read the raw (still
    0.0) argument straight through.
    """

    confirm: float
    detach: float


@dataclass(frozen=True)
class Decision:
    """``decide_actions``' per-task actions plus the thresholds that produced them.

    Callers read ``.actions`` explicitly (``zip(tasks, decision.actions)``)
    rather than iterating ``decision`` itself — this is a decision record, not
    a sequence standing in for one. ``.thresholds`` is the ``ResolvedThresholds``
    actually applied, so a caller like ``deep_research_eligible`` never has to
    re-resolve a number that could silently drift from what decided these
    actions.
    """

    actions: tuple[IdentityAction, ...]
    thresholds: ResolvedThresholds


def threshold_for(origin: IdentityOrigin) -> float:
    """Confirm bar: 0.80 for research, 0.70 for attached.

    Research starts from a name/employer guess with no prior link to the
    person; attached evidence is already anchored to an imported identifier,
    so it needs less additional corroboration to confirm.
    """
    key = "research_confirm" if origin == IdentityOrigin.RESEARCH else "attached_confirm"
    return IDENTITY_THRESHOLDS[key]


def resolve_thresholds(
    confirm: float | None,
    detach: float | None,
    origin: IdentityOrigin,
) -> ResolvedThresholds:
    """Resolve caller overrides against the origin defaults, once.

    ``None`` means "caller didn't override" and falls back to the default;
    an explicit ``0.0`` is a real bar and must survive, which is why this is
    ``is not None`` and never a truthy ``or`` (0.0 is falsy).
    """
    return ResolvedThresholds(
        confirm=confirm if confirm is not None else threshold_for(origin),
        detach=detach if detach is not None else IDENTITY_THRESHOLDS["detach"],
    )


def settled_machine_action(action: str, verdict: IdentityVerdict | None) -> tuple[str, str | None]:
    """The one confirm/detach/review -> (machine_action, approved) mapping.

    ``decide_actions`` calls this to fill in each ``IdentityAction``'s
    settlement; ``write_overrides`` calls it again to translate an
    already-decided ``task.action`` (including a hand-built task that never
    went through ``decide_actions`` at all, e.g. healing's ``terminate()``)
    into what it writes. Neither caller re-derives this mapping itself.
    """
    if action == "confirm":
        return "verify", "auto"
    if action == "detach":
        return "detach", "auto"
    # Below threshold on both sides ("review"), or no action was ever
    # decided: still pre-classify machine_action from the raw verdict so a
    # pending review row carries a suggested action instead of nothing.
    return ("detach" if verdict and verdict.value == "wrong_person" else "verify"), None


def settled_verdict(value: str, reason: str) -> IdentityVerdict:
    """A rule's conclusion dressed in the verdict shape — NOT a judge answer.

    healing.terminate decides detach/confirm deterministically (dead fetch,
    standing synthetic) and needs a verdict-shaped payload to ride
    write_overrides. confidence 1.0 states the rule's certainty, not a
    model's. The reason strings callers pass are heal's exact-string
    handshake with downstream readers — pinned constants, never prose.
    """
    return IdentityVerdict.from_payload(
        {"verdict": value, "confidence": 1.0, "reason": reason}
    )


def deep_research_eligible(task: IdentityTask, thresholds: ResolvedThresholds) -> bool:
    """A confident detach the judge itself flagged as worth chasing, unless it
    already concluded no LinkedIn plausibly exists for them.

    ``thresholds`` must be the exact ``ResolvedThresholds`` the run's
    ``decide_actions`` call produced — see that dataclass's docstring for why.
    """
    return bool(
        task.verdict
        and task.verdict.value == "wrong_person"
        and task.verdict.confidence >= thresholds.detach
        and task.verdict.recommend_deep_research
        and not task.verdict.linkedin_plausibly_absent
    )


def auto_approve_clean_reject(has_reject_fields: bool, llm_reject: str | None) -> bool:
    """A reject-check that ran and came back clean promotes to an implicit
    auto-approve — the one place ``upsert_retargets`` asks this question."""
    return has_reject_fields and not (llm_reject or "").strip()


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
    """Settle a task the paid judge could not answer, without calling it.

    One caller: run_stage's free pass (judge_batch(use_llm=False)), which
    gives a verdict to every attached task still holding None after judging —
    typically no profile to fetch (a missing RapidAPI key leaves tasks
    profile-less and they land here; a missing OPENAI key with judgeable tasks
    is NOT graceful — judge_batch raises at client construction). `origin` is
    always ATTACHED on this path: research proposals only ever run the paid
    judge (their use_llm=False route was deleted with reconcile_deep_research
    --no-llm, and this function's RESEARCH branch went with it — a speculative
    proposal must never be settled by a stub that trusts the profile). See
    reconcile_linkedin.py's changelog for why no such switch may come back.
    An attached profile is trusted at 0.9 confidence: enough to clear
    attached_confirm (0.70) so the offline stub still exercises the confirm
    path, but below decisive (0.95) so it never auto-wins a sibling conflict
    on its own.
    """
    del evidence, origin
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
    threshold = confirm_threshold if confirm_threshold is not None else threshold_for(IdentityOrigin.RESEARCH)
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
) -> Decision:
    """Return pinned keep-biased actions without mutating orchestration tasks.

    detach (0.85) sits above every confirm threshold on purpose: detaching an
    attached identity is destructive and harder to undo than keeping a
    "maybe", so removal demands more confidence than confirmation does.

    The confirm/detach bars actually applied ride along on the return value's
    ``.thresholds`` (see ``Decision``/``ResolvedThresholds``) — read them from
    there instead of re-resolving ``confirm``/``detach`` again. Per-task
    actions are on ``.actions`` — callers zip against that, not the return
    value itself.
    """
    resolved = resolve_thresholds(confirm, detach, origin)
    thresholds = {"confirmed": resolved.confirm, "wrong_person": resolved.detach}

    def clears(task: IdentityTask, verdict: str) -> bool:
        return bool(
            task.verdict
            and task.verdict.value == verdict
            and task.verdict.confidence >= thresholds[verdict]
        )

    def decided(action: str, via: str, task: IdentityTask) -> IdentityAction:
        machine_action, approved = settled_machine_action(action, task.verdict)
        return IdentityAction(action, via, machine_action, approved)

    groups: dict[str, list[int]] = {}
    decisions = [decided("review", "", task) for task in tasks]
    for index, task in enumerate(tasks):
        group_key = task.parent_id or task.parent_slug
        groups.setdefault(group_key, []).append(index)
    for group in groups.values():
        if len(group) == 1:
            index = group[0]
            task = tasks[index]
            if clears(task, "confirmed"):
                decisions[index] = decided("confirm", "normal", task)
            elif clears(task, "wrong_person"):
                decisions[index] = decided("detach", "normal", task)
            continue
        confirmed = [index for index in group if clears(tasks[index], "confirmed")]
        wrong = [index for index in group if clears(tasks[index], "wrong_person")]
        for index in wrong:
            decisions[index] = decided("detach", "normal", tasks[index])
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
                decisions[index] = decided(
                    "confirm" if index == winner else "detach",
                    "conflict_resolved",
                    tasks[index],
                )
    return Decision(tuple(decisions), resolved)
