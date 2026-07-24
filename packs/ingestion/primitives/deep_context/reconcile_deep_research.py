"""[Phase 3, escalation] Re-research people whose attached LinkedIn was WRONG.

Recovers the correct identity for high-confidence `wrong_person` detaches that the
judge recommended for research. User-touched rows are excluded because the current
single-row override schema cannot preserve a sticky decision and a pending retarget
proposal at the same time. It leans on local history first, then
hands the still-unresolved subset to the existing Parallel.ai deep-research primitive.

Cost-gated and opt-in: estimate the Parallel.ai spend, get explicit approval, then
pass ``--approve --budget <approved-estimate>``. The budget defaults to zero so a
changed queue cannot spend against an unstated ceiling. People the judge flagged
`linkedin_plausibly_absent` are
EXCLUDED by default — some people legitimately have no LinkedIn and "no profile exists"
is a valid final answer; we never force a match. Pass --include-plausibly-absent to
research them anyway for SYNTHETIC profiles (assemble_synthetic_profile.py).

Reuses `packs/ingestion/primitives/deep_research_contacts` (Parallel.ai core2x) — this
step only builds the research queue, estimates cost, enforces the gate, and shells out.

Outputs (under .powerpacks/deep-context/reconcile/deep-research/):
  research_queue.csv     queue handed to deep_research_contacts
  manifest.json          subset size, estimated cost, gate decision, run status

Changelog:
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.enrichment_contract import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_INVALID_BUDGET,
    STATUS_NEEDS_APPROVAL,
    STATUS_NOOP,
    STATUS_RAN,
    STATUS_RESEARCH_COMPLETE,
    STATUS_REUSED,
    STATUS_RUNNING,
)
from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.build_parents import parent_id_for
from packs.ingestion.primitives.deep_context.candidates import (
    candidate_carry,
    candidate_key_of,
    candidate_row,
    candidates_resolved_by_existing,
    effective_network_worth,
    is_candidate_id,
    load_candidates,
)
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    RAW_DIR,
    VERDICTS_JSONL,
    emit,
    read_jsonl,
    load_owner,
    owner_background_block,
    parse_list,
    slugify,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    DEFAULT_CONFIRM,
    RESEARCH_CONFIDENCE_FLOOR,
    USER_APPROVED,
    dossier_view,
    judge_research_proposal,
    load_override_rows,
    research_proposal_task,
    research_reject_fields,
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.review_store import RESEARCH_CONFIRM_THRESHOLD
# The enrichment manifest must stamp the SAME worth-selection digest the review UI computes,
# so the two never drift and stall the flow. Single source of truth lives in review_web. The
# research-profile view is reused so the judge sees the SAME (name/headline/experience/education)
# shape the review UI renders — no second profile parser to drift.
from packs.ingestion.primitives.deep_context.review_web.model import (
    _research_profile_view,
)
from packs.ingestion.primitives.deep_context.review_web.workflow import (
    current_worth_selection,
)
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)
# Reuse the canonical pricing from the deep-research primitive (don't mirror/drift).
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    PROCESSOR_PRICING_USD,
    filter_already_done,
)

DEFAULT_PROCESSOR = "core2x"
DEFAULT_BUDGET = 0.0
DR_OUT_DIR = DEEP_RESEARCH_DIR
QUEUE_CSV = DR_OUT_DIR / "research_queue.csv"
QUEUE_FIELDS = [
    "handle",
    "source_parent_slug",
    "source_person_ids",
    "source_candidate_public_identifier",
    "display_name",
    "bio",
    "known_info",
    "primary_email",
    "phone_e164",
    "area_code",
    "source_channel",
    "retarget_hint",
]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_people_rows(people_csv: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if not people_csv.exists():
        return rows
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = str(row.get("id") or "").strip()
            if pid:
                rows[pid] = row
    return rows


def _is_rejected_retarget(row: dict[str, str]) -> bool:
    """A retarget the judge rejected (llm_reject=yes) that the user has NOT approved. Such a row is
    a dead guess, not a decision — it must not permanently mark the person as "decided", so the
    next (cheap, already-completed) research pass can re-propose. A user decision stays terminal."""
    if (row.get("action") or "").strip().lower() != "retarget":
        return False
    if (row.get("approved") or "").strip().lower() in USER_APPROVED:
        return False
    return (row.get("llm_reject") or "").strip().lower() == "yes"


def eligible_subset(verdicts: list[dict[str, Any]], threshold: float,
                    overrides: dict[str, dict[str, str]] | None = None,
                    include_plausibly_absent: bool = False) -> list[dict[str, Any]]:
    """Model detaches that external research could resolve.

    Eligible means a high-confidence `wrong_person` detach the judge flagged
    `recommend_deep_research`, whose parent did not already keep a confirmed link.
    User-touched rows, excluded links, and existing (non-rejected) retargets are skipped.
    A judge-rejected, un-approved retarget does NOT count as decided (re-research is cheap once
    completed). `linkedin_plausibly_absent` people are skipped by default (no profile exists is a
    valid answer) and included only with include_plausibly_absent=True — the synthetic path."""
    overrides = overrides or {}
    has_retarget = {pub for pub, r in overrides.items()
                    if (r.get("action") or "").strip().lower() == "retarget"
                    and not _is_rejected_retarget(r)}
    excluded = {pub for pub, r in overrides.items()
                if (r.get("action") or "").strip().lower() == "exclude"}
    user_decided = {pub for pub, r in overrides.items()
                    if (r.get("approved") or "").strip().lower() in {"yes", "no"}}
    parents_with_kept = {
        r.get("parent_slug") for r in verdicts
        if (r.get("verdict") or {}).get("verdict") == "confirmed"
        and float((r.get("verdict") or {}).get("confidence") or 0) >= threshold
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in verdicts:
        v = r.get("verdict") or {}
        pub = (r.get("candidate_key") or "").strip().lower()
        if pub and (pub in seen or pub in has_retarget or pub in excluded or pub in user_decided):
            continue
        if v.get("linkedin_plausibly_absent"):
            # Excluded from RETARGET research by design (some people legitimately have no
            # profile) — but they are the primary SYNTHETIC-profile candidates: research
            # them only when the caller opts in (synthetic-profiles-plan §5).
            if include_plausibly_absent and r.get("parent_slug") not in parents_with_kept:
                if pub:
                    seen.add(pub)
                out.append(r)
            continue
        model_ok = (v.get("verdict") == "wrong_person"
                    and float(v.get("confidence") or 0) >= threshold
                    and v.get("recommend_deep_research")
                    and r.get("parent_slug") not in parents_with_kept)
        if model_ok:
            if pub:
                seen.add(pub)
            out.append(r)
    return out


def candidate_subset(facts_dir: Path,
                     overrides: dict[str, dict[str, str]] | None = None,
                     *,
                     worth_skipped: list[str] | None = None,
                     resolved_candidates: set[str] | None = None) -> list[dict[str, Any]]:
    """Dossier-bearing import candidates as research subjects (opt-in via
    --include-candidates). Candidates have no resolved LinkedIn by definition;
    eligibility means their facts file exists — the facts ARE the dossier context
    the queue bio is built from. Entries mirror the verdict-row shape so
    build_queue / propose_retargets consume them unchanged; the review.csv key for
    a candidate is its person_id (candidate:<key>).

    A candidate is eligible when it is in the Added pile: either the model said
    yes and the user did not override it, or the user explicitly said yes. Model
    maybe/no candidates remain in the review or Rejected piles unless the user
    moves them. Every candidate that is not currently Added is appended to
    ``worth_skipped`` when provided."""
    overrides = overrides or {}
    resolved_candidates = (candidates_resolved_by_existing()
                           if resolved_candidates is None else resolved_candidates)
    decided = {pub for pub, r in overrides.items()
               if ((r.get("action") or "").strip().lower() in {"retarget", "exclude"}
                   and not _is_rejected_retarget(r))
               or (r.get("approved") or "").strip().lower() in USER_APPROVED}
    out: list[dict[str, Any]] = []
    for person in load_candidates():
        pid = person.person_id
        if (pid.lower() in decided or pid.lower() in resolved_candidates
                or not (facts_dir / f"{pid}.jsonl").exists()):
            continue
        worth = effective_network_worth(pid, overrides, facts_dir)
        if worth["decision"] != "yes":
            if worth_skipped is not None:
                worth_skipped.append(pid)
            continue
        out.append({
            "parent_slug": slugify(person.full_name, parent_id_for([pid])),
            "name": person.full_name,
            "person_ids": [pid],
            "candidate_key": pid,   # retarget proposals key review.csv on this
            "linkedin": {},
            "verdict": {"verdict": "no_linkedin_candidate", "confidence": 0.0,
                        "reason": "unresolved import candidate — no LinkedIn attached"},
            "match_emails": person.emails,
            "match_phones": person.phones,
        })
    return out


def _dossier_bio(child_pids: list[str], facts_dir: Path, raw_dir: Path) -> str:
    records: list[dict[str, Any]] = []
    for pid in child_pids:
        path = facts_dir / f"{pid}.jsonl"
        if path.exists():
            records.extend(
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    merged = compose.merge_facts(records) if records else {}
    parts = []
    aliases = [str(value).strip() for value in (merged.get("aliases") or []) if str(value).strip()]
    if aliases:
        parts.append(f"Also known as: {', '.join(aliases[:8])}")
    if merged.get("relationship_to_owner"):
        parts.append(f"My relationship: {merged['relationship_to_owner']}")
    emps = [e.get("name", "") for e in (merged.get("employers") or []) if e.get("name")]
    if emps:
        parts.append(f"Employers (from our messages): {', '.join(emps)}")
    if merged.get("school"):
        parts.append(f"School: {merged['school']}")
    if merged.get("location"):
        parts.append(f"Location: {merged['location']}")
    if merged.get("topics"):
        parts.append(f"We discuss: {', '.join(merged['topics'][:8])}")
    identifiers = [str(value).strip() for value in (merged.get("identifiers") or [])
                   if str(value).strip()]
    if identifiers:
        parts.append(f"Identifiers from our messages: {', '.join(identifiers[:12])}")
    shared = [
        f"{value.get('overlap', 'other')}: {value.get('detail', '')}".strip(": ")
        for value in (merged.get("shared_context") or [])
        if isinstance(value, dict) and value.get("detail")
    ]
    if shared:
        parts.append(f"Shared context with me: {'; '.join(shared[:8])}")
    return ". ".join(parts)


def build_queue(subset: list[dict[str, Any]], people: dict[str, dict[str, str]],
                facts_dir: Path, raw_dir: Path) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    owner = load_owner()
    owner_context = owner_background_block(owner) if owner else ""
    for r in subset:
        pids = r.get("person_ids") or []
        row = next((people[p] for p in pids if p in people), {})
        if not row:
            crow = next((candidate_row(candidate_key_of(p)) for p in pids if is_candidate_id(p)), None)
            if crow:
                row = candidate_carry(crow)
        emails = [row.get("primary_email", "")] + parse_list(row.get("all_emails"))
        phones = [row.get("primary_phone", "")] + parse_list(row.get("all_phones"))
        email = next((e for e in emails if e and "@" in e), "")
        phone = next((p for p in phones if p), "")
        rejected = (r.get("linkedin") or {}).get("linkedin_url", "")
        if any(is_candidate_id(p) for p in pids):
            hint = ("No LinkedIn is attached yet (unresolved import candidate). "
                    "Find this person's correct LinkedIn if one exists.")
        else:
            hint = (f"The previously attached LinkedIn {rejected} was judged WRONG "
                    f"({(r.get('verdict') or {}).get('reason', '')}). Find the correct person.")
        known_info = hint
        if owner_context:
            known_info += (
                "\n\nUse the mailbox owner's background only as an identity/network-context "
                "prior. Prefer candidates whose geography, school, employers, era, or social "
                "context plausibly intersect it; do not require an overlap.\n"
                f"{owner_context}"
            )
        queue.append({
            "handle": r.get("parent_slug", ""),
            "source_parent_slug": r.get("parent_slug", ""),
            "source_person_ids": json.dumps(pids, ensure_ascii=False),
            "source_candidate_public_identifier": r.get("candidate_key", ""),
            "display_name": r.get("name", ""),
            "bio": _dossier_bio(pids, facts_dir, raw_dir),
            "known_info": known_info,
            "primary_email": email,
            "phone_e164": phone,
            "area_code": "",
            "source_channel": "email" if email else "phone",
            "retarget_hint": hint,
        })
    return queue


def _dig(profile: dict[str, Any], key: str) -> str:
    """Defensively pull a field from the canonical or older research shapes."""
    if not isinstance(profile, dict):
        return ""
    for loc in (
        profile,
        profile.get("research") or {},
        profile.get("profile") or {},
        profile.get("social") or {},
        profile.get("metadata") or {},
    ):
        val = (loc or {}).get(key) if isinstance(loc, dict) else None
        if val:
            return str(val)
    return ""


def _find_linkedin(profile: dict[str, Any]) -> str:
    return _dig(profile, "linkedin_url")


def _find_reason(profile: dict[str, Any]) -> str:
    """Best-effort justification for the retarget, from common research fields."""
    for key in ("research_notes", "reasoning", "rationale", "summary", "headline"):
        val = _dig(profile, key)
        if val:
            return f"deep research: {val}"
    return "deep research found a correct LinkedIn"


def _find_confidence(profile: dict[str, Any]) -> float | None:
    """The research output's OWN identity/name confidence for the proposed profile.

    In the canonical Parallel shape this is `person.confidence` (a name/identity confidence in
    [0,1]); older/alternate shapes may put it top-level or under name_confidence. We deliberately
    do NOT read summary/completeness confidences — those are about the write-up, not identity.
    Returns the parsed float, or None when nothing usable is present (caller maps None -> 0.0)."""
    if not isinstance(profile, dict):
        return None
    person = profile.get("person") if isinstance(profile.get("person"), dict) else {}
    candidates = [
        person.get("confidence"),
        person.get("name_confidence"),
        profile.get("name_confidence"),
        profile.get("identity_confidence"),
        (profile.get("social") or {}).get("name_confidence") if isinstance(profile.get("social"), dict) else None,
    ]
    for value in candidates:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


# Phrases a research write-up uses when it admits it could NOT verify the contact's identifier —
# the exact "could not directly verify the Gmail address" case. Matched case-insensitively.
_UNVERIFIED_MARKERS = (
    "could not directly verify",
    "could not verify",
    "unable to verify",
    "not verified",
    "unverified",
    "no confirming match",
    "not_found",
    "not found",
    "best contextual match",
    "best-guess",
    "best guess",
    "inferred",
    "no direct confirmation",
    "cannot confirm",
    "could not confirm",
)


def _research_unverified(profile: dict[str, Any]) -> bool:
    """True when the research output itself admits it could not verify the contact's identifier.

    Reads the free-text notes/status the model writes (person.notes, metadata.research_notes,
    social.linkedin_status) for explicit non-verification language. Deterministic; used by the
    --no-llm fallback and to bias the judge's context."""
    if not isinstance(profile, dict):
        return False
    person = profile.get("person") if isinstance(profile.get("person"), dict) else {}
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
    texts = [
        str(person.get("notes") or ""),
        str(metadata.get("research_notes") or ""),
        str(social.get("linkedin_status") or ""),
        _find_reason(profile),
    ]
    blob = " ".join(texts).lower()
    return any(marker in blob for marker in _UNVERIFIED_MARKERS)


# The retarget identity judge defaults to medium reasoning effort (still
# overridable via --reasoning-effort). Fan-out is latency-bound, not
# TPM-bound: measured on real data, 32 lanes at high effort moved ~32
# verdicts/min at roughly 2-3% of the tier-5 TPM budget, so the cap is our
# own choice. 128 keeps a healthy margin; the usage-tier profile still caps
# below it on smaller tiers (tier_4 -> 96, tier_1 -> 16).
DEFAULT_JUDGE_CONCURRENCY = 128


def judge_concurrency() -> int:
    """Retarget-judge fan-out: an explicit POWERPACKS_OPENAI_CONCURRENCY env override
    wins verbatim (the shared OpenAI fan-out knob); otherwise the usage-tier profile
    capped at DEFAULT_JUDGE_CONCURRENCY. Per-call retry/backoff lives in judge_task."""
    tier = env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
                              fallback=DEFAULT_JUDGE_CONCURRENCY)
    if (os.getenv("POWERPACKS_OPENAI_CONCURRENCY") or "").strip():
        return tier
    return min(DEFAULT_JUDGE_CONCURRENCY, tier)


def proposal_fingerprint(old_pub: str, new_url: str, dossier: dict[str, Any],
                         profile_view: dict[str, Any]) -> str:
    """Stable sha256 of the EVIDENCE one retarget judgment consumed: the identity pair
    (old_pub → proposed LinkedIn URL) plus the exact dossier view and research-profile
    view fed to the judge. Same sha == same evidence == the stored verdict stands
    (accepts AND rejections); new research output or a changed dossier yields a
    different sha and a normal re-judge."""
    payload = json.dumps(
        {"old_pub": old_pub, "new_linkedin_url": new_url,
         "dossier": dossier, "profile": profile_view},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def propose_retargets_from_output(out_dir: Path, subset: list[dict[str, Any]],
                                  overrides_csv: Path, *,
                                  facts_dir: Path | None = None, raw_dir: Path | None = None,
                                  use_llm: bool = False, owner_block: str = "",
                                  model: str = "", effort: str = "medium",
                                  confirm_threshold: float = DEFAULT_CONFIRM,
                                  timeout: int = 120, max_retries: int = 6,
                                  heartbeat: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """After deep research, propose a `retarget` (pending) for each detached person whose research
    found a correct LinkedIn — into the same decisions table (sticky upsert).

    The proposal carries the research output's OWN identity confidence (never a hardcoded 0.0), and
    is JUDGED before it lands: the same email-evidence identity judge that vets attached links vets
    this (dossier × proposed-profile) pair. A judge rejection marks the row llm_reject=yes + reason
    (rendered by the UI) instead of silently sticking a wrong guess. --no-llm uses the deterministic
    fallback: an unverified / sub-threshold guess is rejected, never auto-approved.

    Judgments are CACHED by evidence fingerprint (see proposal_fingerprint): a person whose
    would-be proposal matches the sha stored on their retarget row keeps the prior verdict —
    including rejections, which would otherwise re-judge on every pass — so a steady-state $0
    pass makes ZERO judge calls. Rows judged before the fingerprint existed are grandfathered:
    the current sha is stamped without a judge call and the stored verdict kept. Genuinely new
    evidence is judged CONCURRENTLY (bounded by judge_concurrency(); per-proposal retry/timeout
    semantics unchanged), with ``heartbeat(done, total)`` called per completion so the UI can
    render honest progress. User-decided rows are never touched (sticky upsert)."""
    facts_dir = facts_dir if facts_dir is not None else FACTS_DIR
    raw_dir = raw_dir if raw_dir is not None else RAW_DIR
    existing = load_override_rows(overrides_csv)
    proposals: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    cached = grandfathered = 0
    for r in subset:
        handle = r.get("parent_slug", "")
        profile = _read_json(out_dir / handle / "01_research_parallel.json")
        new_url = _find_linkedin(profile)
        old_pub = (r.get("candidate_key") or extract_public_identifier((r.get("linkedin") or {}).get("linkedin_url", ""))).lower()
        if not new_url or not old_pub:
            continue
        carried = _find_confidence(profile)
        confidence = carried if carried is not None else 0.0
        unverified = _research_unverified(profile)
        person_ids = r.get("person_ids") or []
        dossier = dossier_view(person_ids, facts_dir, raw_dir)
        li_view = _research_profile_view(profile)
        fingerprint = proposal_fingerprint(old_pub, new_url, dossier, li_view)
        proposal = {
            "old_public_identifier": old_pub, "new_linkedin_url": new_url,
            "linkedin_url": (r.get("linkedin") or {}).get("linkedin_url", ""),
            "match_emails": r.get("match_emails") or [], "match_phones": r.get("match_phones") or [],
            "person_id": (person_ids or [""])[0], "confidence": confidence,
            "reason": _find_reason(profile), "source": "deep-research",
            "judge_fingerprint": fingerprint,
        }
        prior = existing.get(old_pub) or {}
        prior_retarget = (prior.get("action") or "").strip().lower() == "retarget"
        prior_fingerprint = (prior.get("llm_judge_fingerprint") or "").strip()
        if prior_retarget and prior_fingerprint == fingerprint:
            cached += 1  # same evidence — the stored verdict stands (incl. rejections)
            continue
        if (prior_retarget and not prior_fingerprint
                and (prior.get("new_linkedin_url") or "").strip() == normalize_linkedin_url(new_url)):
            # Grandfather rows judged before the fingerprint existed: stamp the current
            # evidence sha WITHOUT a judge call and keep the stored verdict (no llm_reject
            # keys on the proposal, so upsert_retargets preserves the prior columns).
            grandfathered += 1
            proposals.append(proposal)
            continue
        # Judge the (dossier evidence × proposed profile) pair through the SAME machinery as an
        # attached link, flavored as a speculative research proposal (non-name corroboration
        # required). Reject outcomes stamp the UI-rendered llm_reject* columns; never auto-approve.
        pending.append({
            "proposal": proposal,
            "task": research_proposal_task(
                dossier, li_view, name=r.get("name", ""),
                match_emails=r.get("match_emails") or [], match_phones=r.get("match_phones") or [],
                confidence=confidence, unverified=unverified),
        })

    if pending:
        # Bounded fan-out via threads: judge_research_proposal is a self-contained sync
        # wrapper (own client, per-call timeout + retry/backoff), so a thread pool keeps
        # those semantics — and the existing per-proposal mock seam — intact.
        if heartbeat:
            heartbeat(0, len(pending))
        done = 0

        def judge_one(item: dict[str, Any]) -> dict[str, Any]:
            return judge_research_proposal(
                item["task"], use_llm=use_llm, owner_block=owner_block, model=model or "",
                effort=effort, timeout=timeout, max_retries=max_retries)

        with ThreadPoolExecutor(max_workers=min(judge_concurrency(), len(pending))) as pool:
            futures = {pool.submit(judge_one, item): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                item["proposal"].update(research_reject_fields(future.result(), confirm_threshold))
                done += 1
                if heartbeat:
                    heartbeat(done, len(pending))
        proposals.extend(item["proposal"] for item in pending)  # stable subset order

    result = upsert_retargets(overrides_csv, proposals)
    result.update({"judge_calls": len(pending), "cached_verdicts": cached,
                   "grandfathered": grandfathered})
    return result


def write_enrichment_manifest(payload: dict[str, Any], path: Path = ENRICH_MANIFEST) -> dict[str, Any]:
    """Write the one fixed observer contract for the Enrich Contacts UI.

    Provider task-group/run identifiers deliberately stay in the provider's
    existing private artifacts; this manifest exposes only stage status, counts,
    estimate, and stable input/output paths.
    """
    if path.name != "manifest.json":
        raise ValueError("enrichment manifest path must end in manifest.json")
    return write_manifest(path.parent.name, payload, import_dir=path.parent.parent)


def _manifest_counts(*, total: int, completed: int = 0, failed: int = 0) -> dict[str, int]:
    completed = min(max(0, completed), max(0, total))
    failed = min(max(0, failed), max(0, total - completed))
    return {
        "total": max(0, total),
        "completed": completed,
        "pending": max(0, total - completed - failed),
        "failed": failed,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    manifest_text = str(getattr(args, "manifest", "") or "").strip()
    manifest_path = Path(manifest_text) if manifest_text else None
    if not math.isfinite(args.budget) or args.budget < 0:
        result = {
            "source": "reconcile_deep_research",
            "status": STATUS_INVALID_BUDGET,
            "budget_usd": args.budget,
            "message": "--budget must be a finite, non-negative USD amount",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }
        if manifest_path:
            write_enrichment_manifest({
                "stage": "enrich", "status": STATUS_FAILED,
                "counts": _manifest_counts(total=0, failed=0),
                "error": result["message"],
            }, manifest_path)
        return result
    verdicts = list(read_jsonl(Path(args.verdicts_jsonl)))
    overrides = load_override_rows(Path(args.overrides_csv))
    resolved_candidates = candidates_resolved_by_existing()
    # Same authoritative digest the review UI stamps — a candidate promoted to a verified
    # LinkedIn parent leaves the worth pool for BOTH sides here, so they can't disagree by one.
    selection = current_worth_selection()
    subset = eligible_subset(verdicts, args.confirm_threshold, overrides,
                             include_plausibly_absent=getattr(args, "include_plausibly_absent", False))
    worth_skipped: list[str] = []
    candidates = (candidate_subset(
        Path(args.facts_dir), overrides, worth_skipped=worth_skipped,
        resolved_candidates=resolved_candidates)
                  if getattr(args, "include_candidates", False) else [])
    subset += candidates
    people = load_people_rows(Path(args.people_csv))
    queue = build_queue(subset, people, Path(args.facts_dir), Path(args.raw_dir))
    pending_queue, reused_completed = filter_already_done(queue, DR_OUT_DIR)
    duplicate_handles = max(0, len(queue) - len(pending_queue) - reused_completed)
    cost_per = PROCESSOR_PRICING_USD.get(args.processor, PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR])
    est_usd = round(len(pending_queue) * cost_per, 2)

    base = {
        "source": "reconcile_deep_research",
        "eligible": len(subset),
        "eligible_candidates": len(candidates),
        "candidates_skipped_not_added": len(worth_skipped),
        "would_submit": len(pending_queue),
        "reused_completed": reused_completed,
        "duplicate_handles": duplicate_handles,
        "processor": args.processor,
        "cost_per_person_usd": cost_per,
        "estimated_usd": est_usd,
        "budget_usd": args.budget,
        "selection": selection,
        "updated_at": now_iso(),
    }

    DR_OUT_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS)
        w.writeheader()
        w.writerows(queue)

    def persist(result: dict[str, Any], status: str, *, completed: int = 0,
                failed: int = 0) -> dict[str, Any]:
        if manifest_path:
            write_enrichment_manifest({
                "stage": "enrich",
                "status": status,
                "counts": _manifest_counts(
                    total=len(queue), completed=completed, failed=failed),
                "selection": selection,
                "eligible": len(subset),
                "eligible_candidates": len(candidates),
                "candidates_skipped_not_added": len(worth_skipped),
                "would_submit": len(pending_queue),
                "reused_completed": reused_completed,
                "duplicate_handles": duplicate_handles,
                "processor": args.processor,
                "cost_per_person_usd": cost_per,
                "estimated_usd": est_usd,
                "budget_usd": args.budget,
                "input": {
                    "review_csv": str(args.overrides_csv),
                    "facts_dir": str(args.facts_dir),
                    "queue_csv": str(QUEUE_CSV),
                },
                "outputs": {
                    "research_dir": str(DR_OUT_DIR),
                    "review_csv": str(args.overrides_csv),
                },
                "privacy": {
                    "message_bodies_read": False,
                    "paid_provider_called": status in {STATUS_RUNNING, STATUS_RESEARCH_COMPLETE, STATUS_FAILED},
                },
                "result_status": result.get("status", ""),
            }, manifest_path)
        return result

    # Judge each proposed retarget with the SAME identity judge attached links use, inside this
    # already-approved enrichment pass. --no-llm uses the deterministic fallback (never
    # auto-approves; rejects unverified / sub-threshold guesses). Owner background is a network
    # prior for the judge, same as reconcile_linkedin.
    use_llm = not getattr(args, "no_llm", False)
    owner_block = owner_background_block(load_owner()) if load_owner() else ""

    def heartbeat(done: int, total: int) -> None:
        # Honest judging progress in the ONE fixed manifest (no new state files):
        # cheap per-completion writes the UI polls while a pass judges N proposals.
        if manifest_path:
            write_enrichment_manifest({
                "stage": "enrich", "status": STATUS_RUNNING,
                "phase": "judging_retargets", "done": done, "total": total,
                "counts": _manifest_counts(total=len(queue), completed=reused_completed),
                "selection": selection,
            }, manifest_path)

    def propose() -> dict[str, Any]:
        return propose_retargets_from_output(
            DR_OUT_DIR, subset, Path(args.overrides_csv),
            facts_dir=Path(args.facts_dir), raw_dir=Path(args.raw_dir),
            use_llm=use_llm, owner_block=owner_block,
            model=getattr(args, "model", "") or "",
            effort=getattr(args, "reasoning_effort", "medium") or "medium",
            confirm_threshold=args.confirm_threshold, heartbeat=heartbeat)

    if not subset:
        return persist(
            {**base, "status": STATUS_NOOP, "queue_csv": str(QUEUE_CSV),
             "reason": "no effective-Yes contacts need enrichment"},
            STATUS_RESEARCH_COMPLETE)

    if args.dry_run:
        return persist(
            {**base, "status": STATUS_DRY_RUN, "queue_csv": str(QUEUE_CSV),
             "elapsed_ms": int((time.monotonic() - started) * 1000)},
            STATUS_NEEDS_APPROVAL, completed=reused_completed)

    if not pending_queue:
        proposals = propose()
        return persist(
            {**base, "status": STATUS_REUSED, "queue_csv": str(QUEUE_CSV),
             "output_dir": str(DR_OUT_DIR),
             "retargets_proposed": proposals.get("proposed", 0),
             "judge_calls": proposals.get("judge_calls", 0),
             "cached_verdicts": proposals.get("cached_verdicts", 0),
             "grandfathered": proposals.get("grandfathered", 0),
             "reason": "all eligible people already have completed Parallel research",
             "elapsed_ms": int((time.monotonic() - started) * 1000)},
            STATUS_RESEARCH_COMPLETE, completed=len(queue))

    # Every paid run needs current-run approval, and the estimate must stay below
    # the ceiling the user approved.
    if not args.approve or est_usd > args.budget:
        return persist(
            {**base, "status": STATUS_NEEDS_APPROVAL, "queue_csv": str(QUEUE_CSV),
             "message": f"deep research for {len(pending_queue)} net-new people is ~${est_usd:.2f} "
                        f"({reused_completed} completed reused, {duplicate_handles} duplicates skipped); "
                        f"get explicit approval, then re-run with --approve and "
                        f"an approved --budget at or above the estimate (current ${args.budget:.2f})",
             "elapsed_ms": int((time.monotonic() - started) * 1000)},
            STATUS_NEEDS_APPROVAL, completed=reused_completed)

    # Delegate the spend to the existing Parallel.ai primitive (reuse, don't rebuild).
    # STREAM its output live to our stderr (the primitive prints `[deep_research_contacts]
    # poll status ...` every poll) so the run isn't a silent black box — Parallel.ai jobs can
    # take minutes. Keep our own stdout clean for the final JSON manifest.
    print(f"[deep-research] researching {len(pending_queue)} net-new people via Parallel.ai ({args.processor}); "
          "this can take several minutes — live progress below:", file=sys.stderr, flush=True)
    persist({**base, "status": STATUS_RUNNING}, STATUS_RUNNING, completed=reused_completed)
    cmd = [sys.executable, "-m", "packs.ingestion.primitives.deep_context.deep_research_contacts",
           "run", "--input", str(QUEUE_CSV), "--output-dir", str(DR_OUT_DIR), "--processor", args.processor]
    if manifest_path:
        cmd.extend(["--manifest", str(manifest_path)])
    proc = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, text=True)
    print(f"[deep-research] research process exited ({proc.returncode}).", file=sys.stderr, flush=True)
    # Propose retargets (pending) for any correct LinkedIn the research found.
    proposals = {"proposed": 0}
    if proc.returncode == 0:
        proposals = propose()
    result = {
        **base, "status": STATUS_RAN if proc.returncode == 0 else STATUS_FAILED,
        "queue_csv": str(QUEUE_CSV), "output_dir": str(DR_OUT_DIR),
        "retargets_proposed": proposals.get("proposed", 0),
        "judge_calls": proposals.get("judge_calls", 0),
        "cached_verdicts": proposals.get("cached_verdicts", 0),
        "grandfathered": proposals.get("grandfathered", 0),
        "returncode": proc.returncode, "progress": "streamed live to stderr",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return persist(
        result,
        STATUS_RESEARCH_COMPLETE if proc.returncode == 0 else STATUS_FAILED,
        completed=len(queue) if proc.returncode == 0 else reused_completed,
        failed=0 if proc.returncode == 0 else len(pending_queue))


def _finite_non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deep-research the correct identity for wrong_person detaches (cost-gated).")
    p.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    p.add_argument("--overrides-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--manifest", default=str(ENRICH_MANIFEST),
                   help="Fixed Enrich Contacts progress manifest")
    p.add_argument("--processor", default=DEFAULT_PROCESSOR, choices=sorted(PROCESSOR_PRICING_USD))
    p.add_argument("--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD)
    p.add_argument("--budget", type=_finite_non_negative_float, default=DEFAULT_BUDGET,
                   help="Maximum explicitly approved spend (finite, non-negative USD)")
    p.add_argument("--approve", action="store_true", help="Confirm the user approved this run's displayed estimate")
    p.add_argument("--dry-run", action="store_true", help="Build the queue + estimate only; no Parallel.ai spend")
    p.add_argument("--include-plausibly-absent", action="store_true",
                   help="Also research people the judge flagged linkedin_plausibly_absent — the synthetic-profile candidates (synthetic-profiles-plan §5)")
    p.add_argument("--include-candidates", action="store_true",
                   help="Also research dossier-bearing import candidates (import/*/candidates.csv) — contacts with no resolved LinkedIn at all")
    # The proposed-retarget identity judge (reused from reconcile_linkedin) runs inside this same
    # approved pass. --no-llm falls back to the deterministic verdict (rejects unverified guesses).
    p.add_argument("--no-llm", action="store_true",
                   help="Judge proposed retargets deterministically (offline/tests) instead of the LLM")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Model for the proposed-retarget identity judge")
    p.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"],
                   help="Reasoning effort for the proposed-retarget identity judge")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
