"""Review progress, manifests, stage selection, and workflow state."""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.candidates import (
    is_candidate_id,
)
from packs.ingestion.primitives.deep_context.enrichment_contract import (
    IN_FLIGHT_STATUSES,
    STATUS_COMPLETED,
    STATUS_NEEDS_APPROVAL,
    read_enrichment_manifest,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    REVIEW_MANIFEST,
    VERDICTS_JSONL,
    now_iso,
)
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.deep_context.review_store import (
    judge_accepted_candidate_retarget,
    judge_rejected_candidate_retarget,
)

from .model import SYNTHETIC_PEOPLE_CSV, USER_WORTH_VALUES, _cand_rank, _all_review_parents, _worth_key, build_parents, candidate_state, extend_and_annotate, is_effective_no, summarize

def live_counts(verdicts_path: Path, review_path: Path, synthetic_path: Path,
                facts_dir: Path, connections: set[str] | None = None) -> dict[str, int]:
    """Fresh GLOBAL tab counts after a mutation. Every POST returns these so the client
    repaints the header stats and tab pills authoritatively — recomputing counts from
    the DOM would drift on filtered views (only the visible subset is in the DOM)."""
    parents, overrides = build_parents(verdicts_path, review_path)
    return summarize(extend_and_annotate(parents, overrides, synthetic_path, facts_dir,
                                         connections))


def is_import_candidate_parent(parent: dict[str, Any]) -> bool:
    return any(cand.get("import_candidate") for cand in parent.get("candidates") or [])


def is_candidate_origin(parent: dict[str, Any]) -> bool:
    """A reconciled/synthetic person that came from an unresolved import."""
    return any(is_candidate_id(str(person_id or "")) for person_id in parent.get("person_ids") or [])


def is_worth_subject(parent: dict[str, Any]) -> bool:
    """A standalone imported contact whose add/no decision stays reviewable.

    Retarget and synthetic results remain in this scope because their durable
    worth key is still the candidate id. A candidate folded into an existing
    real parent does not: that person's network membership already exists.
    """
    person_ids = [str(value or "") for value in parent.get("person_ids") or []]
    return is_import_candidate_parent(parent) or (
        bool(person_ids)
        and all(is_candidate_id(person_id) for person_id in person_ids)
        and is_candidate_id(_worth_key(parent))
    )


def in_worth_view(parent: dict[str, Any]) -> bool:
    """The worth SECTION is worth_view's row set, nothing else: judged people
    (facts verdict + human override, identities grouped by index parent),
    regardless of people.csv / network membership. See worth_view.py's header
    for the entire logic — this module only renders it."""
    return parent.get("worth_row") is not None


def explicit_worth(parent: dict[str, Any]) -> str:
    """The user's terminal binary worth decision, ignoring model/default advice."""
    worth = parent.get("worth") or {}
    decision = str(worth.get("decision") or "").strip().lower()
    return decision if worth.get("source") == "user" and decision in USER_WORTH_VALUES else ""


def _effective_yes(parent: dict[str, Any]) -> bool:
    row = parent.get("worth_row")
    return bool(row) and row["effective"] == "yes"


def _effective_no_row(parent: dict[str, Any]) -> bool:
    row = parent.get("worth_row")
    return bool(row) and row["effective"] == "no"


def needs_worth_review(parent: dict[str, Any]) -> bool:
    """Only model-uncertain people need the first human decision: worth_view
    effective == maybe. A synthetic-profile row is already past the worth
    decision (it went through research/minting) and is handled in the
    LinkedIn/mint stage, so it is excluded even when its row is a maybe."""
    row = parent.get("worth_row")
    return (bool(row) and row["effective"] == "maybe"
            and not any(cand.get("synthetic") for cand in parent.get("candidates") or []))


def is_lookup_ready(parent: dict[str, Any]) -> bool:
    return is_worth_subject(parent) and _effective_yes(parent)


def pending_linkedin_candidates(parent: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates that still need the second human Yes/No.

    Existing high-confidence links may remain machine-approved. Every new
    identity originating from an import candidate must be explicitly checked;
    ``approved=auto`` on a synthetic row is profile completeness, not confidence
    that this is the right human, so it is still a pending identity decision.
    """
    if is_import_candidate_parent(parent) or is_effective_no(parent):
        return []
    from_candidate = is_candidate_origin(parent)
    pending: list[dict[str, Any]] = []
    for cand in parent.get("candidates") or []:
        approved = str(cand.get("approved") or "").strip().lower()
        if cand.get("synthetic"):
            if approved not in {"yes", "no"}:
                pending.append(cand)
        elif from_candidate:
            # A judge-ACCEPTED found profile stands, and so does a rejection AT
            # OR ABOVE the confirm bar (review_store's two predicates): the
            # identity judge already vetted both against the dossier. Only
            # unjudged candidates and sub-bar rejections — which conflate
            # near-confirm flavors — still need the human Yes/No.
            if (approved not in {"yes", "no"}
                    and not judge_accepted_candidate_retarget(cand)
                    and not judge_rejected_candidate_retarget(cand)):
                pending.append(cand)
        elif candidate_state(cand) == "review":
            pending.append(cand)
    return sorted(pending, key=_cand_rank)


def identity_in_scope(parent: dict[str, Any]) -> bool:
    if is_import_candidate_parent(parent) or is_effective_no(parent):
        return False
    if is_candidate_origin(parent) or any(c.get("synthetic") for c in parent.get("candidates") or []):
        return True
    return any(candidate_state(c) == "review"
               or str(c.get("approved") or "").strip().lower() in {"yes", "no"}
               for c in parent.get("candidates") or [])


def review_progress(parents: list[dict[str, Any]]) -> dict[str, int]:
    # Worth counts come from worth_view rows, deduped: several review-parents
    # can share one PERSON row (merged identities render once, count once).
    seen: set[int] = set()
    worth_total = worth_pending = worth_yes = worth_no = 0
    for parent in parents:
        row = parent.get("worth_row")
        if row is None or id(row) in seen:
            continue
        seen.add(id(row))
        worth_total += 1
        if needs_worth_review(parent):
            worth_pending += 1
        elif row["effective"] == "yes":
            worth_yes += 1
        elif row["effective"] == "no":
            worth_no += 1
    lookup_ready = [parent for parent in parents if is_lookup_ready(parent)]
    identity_scope = [parent for parent in parents if identity_in_scope(parent)]
    identity_pending = [parent for parent in identity_scope if pending_linkedin_candidates(parent)]
    return {
        "total": len(parents),
        "worth_total": worth_total,
        "worth_pending": worth_pending,
        "worth_yes": worth_yes,
        "worth_no": worth_no,
        "lookup_ready": len(lookup_ready),
        "linkedin_total": len(identity_scope),
        "linkedin_pending": len(identity_pending),
        "linkedin_done": len(identity_scope) - len(identity_pending),
        "rejected": sum(1 for parent in parents if is_effective_no(parent)),
    }


def review_state_token(progress: dict[str, int], selection: dict[str, Any],
                       enrichment: dict[str, Any],
                       review_manifest: dict[str, Any], *,
                       job_running: bool = False) -> str:
    """Ephemeral browser refresh token derived from the fixed file state PLUS
    whether the in-process pipeline job is alive. The job bit closes a TOCTOU:
    a page rendered while the job was finishing can carry a token computed from
    the job's FINAL manifest writes — without the bit, nothing changes after the
    job exits and the observer never reloads the "working" screen."""
    payload = {
        "progress": progress,
        "selection": selection,
        "job_running": bool(job_running),
        "enrichment": {
            "status": enrichment.get("status"),
            "current": enrichment.get("current"),
            "approval_current": enrichment.get("approval_current"),
            "counts": enrichment.get("counts") or {},
            "updated_at": enrichment.get("updated_at"),
        },
        "review": {
            "stage": review_manifest.get("stage"),
            "status": review_manifest.get("status"),
            "completed_stages": review_manifest.get("completed_stages") or [],
            "updated_at": review_manifest.get("updated_at"),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def browser_stage_for_next_action(next_action: str) -> str:
    if next_action == "review_people":
        return "worth"
    if next_action in {
            "preview_enrichment", "await_enrichment_approval",
            "run_approved_enrichment", "run_enrichment_from_cache",
            "wait_for_enrichment", "retry_enrichment", "assemble_synthetic",
            "continue_enrichment"}:
        return "enrich"
    if next_action in {"review_linkedin", "finish_linkedin"}:
        return "linkedin"
    return "done"


def worth_selection_from_parents(
    parents: list[dict[str, Any]], *, manifest_path: Path = REVIEW_MANIFEST,
) -> dict[str, Any]:
    decisions = [
        {"person_id": _worth_key(parent),
         "decision": str((parent.get("worth") or {}).get("decision") or "maybe")}
        for parent in parents if is_worth_subject(parent) and _worth_key(parent)
    ]
    decisions.sort(key=lambda row: row["person_id"])
    encoded = json.dumps(decisions, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    review_manifest = read_review_manifest(manifest_path)
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "total": len(decisions),
        "yes": sum(row["decision"] == "yes" for row in decisions),
        "maybe": sum(row["decision"] == "maybe" for row in decisions),
        "no": sum(row["decision"] == "no" for row in decisions),
        "review_revision": str(review_manifest.get("people_revision") or ""),
    }


def current_worth_selection(*, manifest_path: Path = REVIEW_MANIFEST) -> dict[str, Any]:
    """The one authoritative People-worth selection digest, built from the live review
    parents. Both the review status and the enrichment manifest must stamp THIS value so
    their sha256 can never drift: a candidate promoted to a verified LinkedIn parent (e.g.
    via a retarget/verify) leaves the worth pool here for both sides at once, instead of
    the enrichment side re-deriving the set from candidate files and disagreeing by one."""
    parents = _all_review_parents(
        VERDICTS_JSONL, LINKEDIN_OVERRIDES_CSV, SYNTHETIC_PEOPLE_CSV, FACTS_DIR,
        DEFAULT_PEOPLE_CSV, PARENTS_DIR, DOSSIER_DIR, PROFILE_CACHE_DIR)
    return worth_selection_from_parents(parents, manifest_path=manifest_path)


def approve_enrichment_manifest(path: Path = ENRICH_MANIFEST, *,
                                selection: dict[str, Any]) -> dict[str, Any]:
    """Persist the UI's exact spend approval in the fixed enrichment manifest.

    The manifest write is inert. The HTTP handler validates this revision-bound
    record, then starts the approved in-process enrichment job with exactly the
    persisted budget.
    """
    enrichment = read_enrichment_manifest(path, selection=selection)
    if not enrichment.get("current"):
        raise ValueError("Enrichment preview is stale; refresh the preview before approving")
    # A five-second browser observer can leave a just-completed approval button
    # visible briefly after the agent has already advanced the fixed manifest.
    # Treat that stale click as an idempotent success so the client simply
    # reloads into the current progress state instead of showing a false error.
    if enrichment.get("status") in IN_FLIGHT_STATUSES:
        return enrichment
    if enrichment.get("status") != STATUS_NEEDS_APPROVAL:
        raise ValueError("Enrichment is not waiting for approval")
    if enrichment.get("approval_current"):
        return enrichment
    try:
        would_submit = int(enrichment.get("would_submit") or 0)
        estimate = round(float(enrichment.get("estimated_usd") or 0), 2)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Enrichment estimate is invalid") from exc
    if would_submit <= 0:
        raise ValueError("No paid enrichment approval is required")
    if not math.isfinite(estimate) or estimate <= 0:
        raise ValueError("Enrichment estimate must be a positive finite amount")
    recorded_selection = enrichment.get("selection") or {}
    payload = {key: value for key, value in enrichment.items()
               if key not in {"current", "approval_current"}}
    payload["approval"] = {
        "status": "approved",
        "approved_at": now_iso(),
        "approved_budget_usd": estimate,
        "estimated_usd": estimate,
        "would_submit": would_submit,
        "selection_sha256": str(recorded_selection.get("sha256") or ""),
        "review_revision": str(recorded_selection.get("review_revision") or ""),
    }
    payload["updated_at"] = now_iso()
    write_manifest(path.parent.name, payload, import_dir=path.parent.parent)
    return read_enrichment_manifest(path, selection=selection)


def phase_counts(progress: dict[str, int], stage: str) -> dict[str, int]:
    if stage == "worth":
        return {
            "total": progress["worth_total"],
            "yes": progress["worth_yes"],
            "no": progress["worth_no"],
            "pending": progress["worth_pending"],
            "ready_for_lookup": progress["lookup_ready"],
        }
    if stage == "linkedin":
        return {
            "total": progress["linkedin_total"],
            "yes_or_no": progress["linkedin_done"],
            "pending": progress["linkedin_pending"],
        }
    raise ValueError(f"unknown review stage: {stage}")


def read_review_manifest(path: Path = REVIEW_MANIFEST) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_review_manifest(stage: str, status: str, progress: dict[str, int], *,
                          path: Path = REVIEW_MANIFEST,
                          review_path: Path = LINKEDIN_OVERRIDES_CSV,
                          synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                          launched: bool = False) -> dict[str, Any]:
    if stage not in {"worth", "enrich", "linkedin"}:
        raise ValueError(f"unknown review stage: {stage}")
    if status not in {"awaiting_user", "completed"}:
        raise ValueError(f"unknown review status: {status}")
    if path.name != "manifest.json":
        raise ValueError("review manifest path must end in manifest.json")
    if stage == "enrich":
        raise ValueError("Enrich completion must be written from the enrichment manifest")
    counts = phase_counts(progress, stage)
    if status == "completed" and counts["pending"]:
        raise ValueError(f"{counts['pending']} decisions still need an answer")
    existing = read_review_manifest(path)
    completed = {str(value) for value in existing.get("completed_stages") or []
                 if value in {"worth", "enrich", "linkedin"}}
    if (existing.get("status") == "completed"
            and existing.get("stage") in {"worth", "enrich", "linkedin"}):
        completed.add(str(existing["stage"]))
    if status == "awaiting_user":
        # awaiting_user records status for display but NEVER demotes
        # completed_stages: the flow only moves forward, so a server relaunch
        # or later machine maybes cannot reopen a finished stage. Clearing the
        # ladder is the restart primitive's explicit job.
        pass
    else:
        # Worth must precede LinkedIn, but enrichment does NOT block it: the
        # LinkedIn stage is reviewable (and completable) even when enrichment is
        # still running or failed, so a broken enrichment never strands the flow.
        if stage == "linkedin" and "worth" not in completed:
            raise ValueError("People decisions must be completed before LinkedIn")
        completed.add(stage)
    people_revision = str(existing.get("people_revision") or "")
    if stage == "worth" and launched:
        people_revision = str(time.time_ns())
    if not people_revision:
        people_revision = str(time.time_ns())
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "counts": counts,
        "completed_stages": sorted(completed, key=("worth", "enrich", "linkedin").index),
        "people_revision": people_revision,
        "review_csv": str(review_path),
        "synthetic_people_csv": str(synthetic_path),
        "privacy": {"message_bodies_read": False, "network_called": True,
                    "paid_provider_called": False,
                    "note": "avatar cache misses may fetch an existing LinkedIn CDN image"},
    }
    if launched:
        payload["launched_at"] = now_iso()
        payload["launched_at_unix_ns"] = time.time_ns()
    elif existing.get("stage") == stage:
        for key in ("launched_at", "launched_at_unix_ns"):
            if key in existing:
                payload[key] = existing[key]
    if status == "completed":
        payload["completed_at"] = now_iso()
    return write_manifest(path.parent.name, payload, import_dir=path.parent.parent)


def write_enrichment_handoff(
    enrichment: dict[str, Any], *, path: Path = REVIEW_MANIFEST,
    review_path: Path = LINKEDIN_OVERRIDES_CSV,
    synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
) -> dict[str, Any]:
    """Record only the user's Continue handoff after current enrichment finished."""
    if enrichment.get("status") != STATUS_COMPLETED or not enrichment.get("current"):
        raise ValueError("Enrichment is not complete for the current People decisions")
    existing = read_review_manifest(path)
    completed = {str(value) for value in existing.get("completed_stages") or []
                 if value in {"worth", "enrich", "linkedin"}}
    if "worth" not in completed:
        raise ValueError("People decisions must be completed before enrichment")
    completed.add("enrich")
    completed.discard("linkedin")
    payload = {
        "stage": "enrich",
        "status": "completed",
        "counts": enrichment.get("counts") or {},
        "completed_stages": sorted(completed, key=("worth", "enrich", "linkedin").index),
        "people_revision": str(existing.get("people_revision") or ""),
        "review_csv": str(review_path),
        "synthetic_people_csv": str(synthetic_path),
        "completed_at": now_iso(),
        "privacy": {"message_bodies_read": False, "network_called": False,
                    "paid_provider_called": False},
    }
    return write_manifest(path.parent.name, payload, import_dir=path.parent.parent)


def enrichment_handoff_completed(path: Path = REVIEW_MANIFEST) -> bool:
    return "enrich" in set(read_review_manifest(path).get("completed_stages") or [])


def phase_is_completed(stage: str, progress: dict[str, int], path: Path = REVIEW_MANIFEST) -> bool:
    manifest = read_review_manifest(path)
    counts = phase_counts(progress, stage)
    completed = set(manifest.get("completed_stages") or [])
    if stage in completed:
        # The ladder only moves FORWARD: once a stage is recorded completed,
        # later machine work (new maybes from a re-judge or fresh synthesis)
        # never reopens the gate — new items soft-surface in that stage's
        # Review tab instead of yanking the user backward. The one sanctioned
        # backward move is the explicit restart, which clears completed_stages.
        return True
    return (manifest.get("stage") == stage
            and manifest.get("status") == "completed"
            and manifest.get("counts") == counts
            and counts["pending"] == 0)
