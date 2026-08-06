"""Research effective-Yes parents that still need a usable identity.

SQLite selects the queue. Parallel writes one raw and normalized result per
parent. The shared identity judge accepts or rejects proposed LinkedIns; the
assembly step creates a synthetic fallback when none survives.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
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
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    emit,
    ENRICH_MANIFEST,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    load_owner,
    owner_background_block,
    RAW_DIR,
    ROOT,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.pipeline.contract import StageManifest
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    DEFAULT_CONFIRM,
    linkedin_view,
    dossier_view,
    judge_research_proposal,
    research_proposal_task,
    research_reject_fields,
    upsert_retargets,
)
from packs.ingestion.primitives.enrich.rapidapi_client import hydrate_profiles
from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)
# Reuse the canonical pricing from the deep-research primitive (don't mirror/drift).
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    PROCESSOR_PRICING_USD,
    ResearchRunParams,
    filter_already_done,
    research_artifact_inventory,
    run_research,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_manifest
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError

DEFAULT_PROCESSOR = "core2x"
DEFAULT_BUDGET = 0.0
CANONICAL_DB = ROOT / "deep-context.sqlite"
RESEARCH_CONFIRM_THRESHOLD = 0.80
# The research payload statuses a pass treats as success — the same set the
# exit-code era called "exit 0" ({no_work, completed}); completed_with_errors
# (old exit 2) stays a failed pass.
RESEARCH_OK_STATUSES = frozenset({"no_work", "completed"})
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
# Declared-contract template for the per-person research output the Parallel.ai
# primitive writes under DR_OUT_DIR (`<handle>/01_research_parallel.json`). This
# module is the producer, so the constant lives HERE and the consumers
# (assemble_synthetic_profile, this module's own reuse pass) import it — graph
# edges are string equality on declared paths (`pipeline/contract.py`).
RESEARCH_PROFILE_TEMPLATE = str(DR_OUT_DIR / "{handle}" / "01_research_parallel.json")
def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


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
    shared = [
        f"{value.get('overlap', 'other')}: {value.get('detail', '')}".strip(": ")
        for value in (merged.get("shared_context") or [])
        if isinstance(value, dict) and value.get("detail")
    ]
    if shared:
        parts.append(f"Shared context with me: {'; '.join(shared[:8])}")
    return ". ".join(parts)


def build_queue(
    subset: list[dict[str, Any]], facts_dir: Path, raw_dir: Path,
) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    owner = load_owner()
    owner_context = owner_background_block(owner) if owner else ""
    for r in subset:
        pids = r.get("person_ids") or []
        email = next((str(value) for value in r.get("match_emails") or [] if "@" in str(value)), "")
        phone = next((str(value) for value in r.get("match_phones") or [] if str(value)), "")
        rejected = (r.get("linkedin") or {}).get("linkedin_url", "")
        context = ""
        if rejected:
            context = (
                f"Rejected LinkedIn: {rejected}. "
                f"Reason: {(r.get('verdict') or {}).get('reason', '')}"
            )
        if owner_context:
            context = "\n".join(filter(None, (context, f"Mailbox owner: {owner_context}")))
        queue.append({
            "handle": r.get("parent_slug", ""),
            "source_parent_slug": r.get("parent_slug", ""),
            "source_person_ids": json.dumps(pids, ensure_ascii=False),
            "source_candidate_public_identifier": r.get("candidate_key", ""),
            "display_name": r.get("name", ""),
            "bio": _dossier_bio(pids, facts_dir, raw_dir),
            "known_info": context,
            "primary_email": email,
            "phone_e164": phone,
            "area_code": "",
            "source_channel": "email" if email else "phone",
            "retarget_hint": "",
        })
    return queue


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


def _research_evidence(profile: dict[str, Any]) -> dict[str, Any]:
    """Parse the one normalized provider artifact shape."""
    person = profile.get("person") or {}
    metadata = profile.get("metadata") or {}
    social = profile.get("social") or {}
    location = profile.get("location") or {}
    positions = profile.get("positions") or []
    education_rows = profile.get("education") or []
    url = str(social.get("linkedin_url") or "").strip()
    notes = str(metadata.get("research_notes") or person.get("notes") or "").strip()
    headline = profile.get("headline") or {}
    headline = str(headline.get("text") or "") if isinstance(headline, dict) else str(headline)
    try:
        confidence = float(person.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    experiences = [
        f"{row.get('title') or '?'} @ {row.get('company_name') or '?'}"
        for row in positions if isinstance(row, dict)
        and (row.get("title") or row.get("company_name"))
    ]
    education = [
        " — ".join(filter(None, (
            ", ".join(filter(None, (str(row.get("degree") or ""),
                                      str(row.get("field_of_study") or "")))),
            str(row.get("school_name") or ""),
        )))
        for row in education_rows if isinstance(row, dict)
        and (row.get("school_name") or row.get("degree") or row.get("field_of_study"))
    ]
    place = str(location.get("raw") or "").strip() or ", ".join(
        str(location.get(key) or "").strip() for key in ("city", "state", "country")
        if str(location.get(key) or "").strip()
    )
    reason = f"deep research: {notes}" if notes else "deep research found a correct LinkedIn"
    unverified = any(marker in f"{notes} {social.get('linkedin_status') or ''}".lower()
                     for marker in _UNVERIFIED_MARKERS)
    return {
        "url": url, "confidence": confidence, "reason": reason, "unverified": unverified,
        "profile": {
            "public_identifier": extract_public_identifier(url).lower(), "linkedin_url": url,
            "full_name": str(person.get("full_name") or ""), "headline": headline,
            "profile_pic_url": "", "experiences": experiences, "education": education,
            "location": place, "reason": reason,
            "has_profile": bool(person or positions or education_rows or place),
        },
    }


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
                                  db: Db,
                                  facts_dir: Path | None = None, raw_dir: Path | None = None,
                                  use_llm: bool = False, owner_block: str = "",
                                  model: str = "", effort: str = "medium",
                                  confirm_threshold: float = DEFAULT_CONFIRM,
                                  timeout: int = 120, max_retries: int = 6,
                                  heartbeat: Callable[[int, int], None] | None = None,
                                  profile_cache_dir: Path | None = None) -> dict[str, Any]:
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
    cache_dir = Path(profile_cache_dir) if profile_cache_dir is not None else DEFAULT_PROFILE_CACHE_DIR
    # Prefer cache, always retrieve — the research payload carries the URL but often
    # no positions, and judging a blank profile rejects LinkedIns that are correct
    # (75 of 92 such rejections on a real store had a rich profile available). Same
    # policy as the attached-link judge; keyless installs fall back to the payload.
    proposed = [
        (extract_public_identifier(evidence["url"]).lower(), evidence["url"])
        for evidence in (
            _research_evidence(_read_json(
                out_dir / (row.get("parent_slug") or "") / "01_research_parallel.json"
            ))
            for row in subset
        ) if evidence["url"]
    ]
    if proposed:
        hydrate_profiles(proposed, cache_dir)
    del overrides_csv
    existing = views.link_decision_state(db)
    proposals: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    cached = grandfathered = 0
    for r in subset:
        handle = r.get("parent_slug", "")
        profile = _read_json(out_dir / handle / "01_research_parallel.json")
        evidence = _research_evidence(profile)
        new_url = evidence["url"]
        old_pub = (r.get("candidate_key") or extract_public_identifier((r.get("linkedin") or {}).get("linkedin_url", ""))).lower()
        if not new_url or not old_pub:
            continue
        confidence = evidence["confidence"]
        unverified = evidence["unverified"]
        person_ids = r.get("person_ids") or []
        dossier = dossier_view(person_ids, facts_dir, raw_dir)
        li_view = evidence["profile"]
        cached_view = linkedin_view({"linkedin_url": new_url}, cache_dir)
        # THE REAL LINKEDIN WINS. The judge's question is "is this LinkedIn profile
        # the same person as my contact?", so it must see the profile itself; the
        # research payload is web findings ABOUT someone, not profile content. A
        # LinkedIn listing fewer roles than the web turned up is still the profile
        # being judged. Fall back to the research view only when the cache holds no
        # real profile content (an empty shell or a failed fetch). The research
        # write-up's `reason` is kept either way.
        if cached_view.get("experiences") or cached_view.get("education"):
            li_view = {**cached_view, "reason": li_view.get("reason", "")}
        fingerprint = proposal_fingerprint(old_pub, new_url, dossier, li_view)
        proposal = {
            "old_public_identifier": old_pub, "new_linkedin_url": new_url,
            "linkedin_url": (r.get("linkedin") or {}).get("linkedin_url", ""),
            "match_emails": r.get("match_emails") or [], "match_phones": r.get("match_phones") or [],
            "person_id": (person_ids or [""])[0], "confidence": confidence,
            "reason": evidence["reason"], "source": "deep-research",
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

    result = upsert_retargets(db, proposals)
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


class ReconcileDeepResearchManifest(StageManifest):
    """The ENRICH_MANIFEST receipt body — the exact keys `persist()` used to hand
    `write_enrichment_manifest` (plus `source`, which `write_manifest` injected
    from the manifest's directory name). Branch-only keys are `| None` so
    `to_payload()` drops them exactly where the raw dict never set them (the
    invalid-budget receipt is only stage/status/counts/error). NOTE this is the
    RECEIPT shape, not the emitted CLI result — the two have always differed
    (receipt statuses needs_approval/research_complete/failed vs result statuses
    noop/dry_run/reused/ran/...); the result dict rides on `node.result`."""
    source: str | None = None
    stage: str = "enrich"
    counts: dict[str, int] | None = None
    selection: dict[str, Any] | None = None
    eligible: int | None = None
    eligible_candidates: int | None = None
    candidates_skipped_not_added: int | None = None
    would_submit: int | None = None
    reused_completed: int | None = None
    duplicate_handles: int | None = None
    processor: str | None = None
    cost_per_person_usd: float | None = None
    estimated_usd: float | None = None
    budget_usd: float | None = None
    input: dict[str, str] | None = None
    outputs: dict[str, str] | None = None
    privacy: dict[str, bool] | None = None
    result_status: str | None = None
    error: str | None = None
    artifacts: list[dict[str, Any]] | None = None


class ReconcileDeepResearch:
    """Cost-gated deep research for wrong-LinkedIn detaches (and opted-in
    candidates): builds the queue, estimates Parallel.ai spend, enforces the
    --approve --budget gate, shells out to the research primitive, and judges
    the proposed retargets. The browser polls ENRICH_MANIFEST while this runs —
    mid-run receipts (running / judging heartbeat) are written in-flow and only
    the TERMINAL receipt goes through the Node template."""

    def __init__(
        self,
        *,
        verdicts_jsonl: Path | None = None,
        overrides_csv: Path | None = None,
        people_csv: Path | None = None,
        facts_dir: Path | None = None,
        index_json: Path | None = None,
        raw_dir: Path | None = None,
        manifest: str | Path | None = None,
        processor: str = DEFAULT_PROCESSOR,
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        budget: float = DEFAULT_BUDGET,
        approve: bool = False,
        dry_run: bool = False,
        include_plausibly_absent: bool = False,
        include_candidates: bool = False,
        no_llm: bool = False,
        # Same default as `--model`: the judge call passes `self.model or ""`
        # straight through, and `judge_research_proposal`'s own DEFAULT_MODEL
        # cannot fill an explicitly-empty argument — so a caller that
        # constructs this node directly (the review app does) would have sent
        # an empty model to a paid judge.
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        out_dir: Path | None = None,
        queue_csv: Path | None = None,
        on_progress: Any = None,
        db: Db,
    ) -> None:
        del verdicts_jsonl, people_csv, index_json
        self.overrides_csv = Path(overrides_csv or LINKEDIN_OVERRIDES_CSV)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.out_dir = Path(out_dir or DR_OUT_DIR)
        self.queue_csv = Path(queue_csv or QUEUE_CSV)
        # In-process progress channel (the review server holds live counts in
        # memory; manifest writes stay the durable record). None = CLI, no listener.
        self.on_progress = on_progress
        self.db = db
        # None = the CLI default (the fixed Enrich Contacts receipt); "" disables
        # every receipt write, exactly like `--manifest ""` always has.
        manifest_text = str(ENRICH_MANIFEST) if manifest is None else str(manifest).strip()
        self.manifest_path = Path(manifest_text) if manifest_text else None
        if self.manifest_path is not None and self.manifest_path.name != "manifest.json":
            # Same guard write_enrichment_manifest applies on every mid-run write,
            # moved to construction so no path can dodge it.
            raise ValueError("enrichment manifest path must end in manifest.json")
        self.processor = processor
        self.confirm_threshold = confirm_threshold
        self.budget = budget
        self.approve = approve
        self.dry_run = dry_run
        self.include_plausibly_absent = include_plausibly_absent
        self.include_candidates = include_candidates
        self.no_llm = no_llm
        self.model = model
        self.reasoning_effort = reasoning_effort
        # The emitted CLI result (`main()` prints it) — same dict, same keys as
        # the old `run(args)` return value.
        self.result: dict[str, Any] = {}

    def _write(self, payload: StageManifest) -> None:
        if self.manifest_path is None:
            return
        write_enrichment_manifest(payload.to_payload(), self.manifest_path)
        project_manifest(self.db, self.manifest_path)

    def run(self) -> ReconcileDeepResearchManifest:
        payload = self.execute()
        self._write(payload)
        return payload

    def execute(self) -> ReconcileDeepResearchManifest:
        started = time.monotonic()
        manifest_path = self.manifest_path
        if not math.isfinite(self.budget) or self.budget < 0:
            message = "--budget must be a finite, non-negative USD amount"
            self.result = {
                "source": "reconcile_deep_research",
                "status": STATUS_INVALID_BUDGET,
                "budget_usd": self.budget,
                "message": message,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "updated_at": now_iso(),
            }
            return ReconcileDeepResearchManifest(
                source=manifest_path.parent.name if manifest_path else None,
                status=STATUS_FAILED,
                counts=_manifest_counts(total=0, failed=0),
                error=message,
            )
        # Same authoritative digest the review UI stamps — a candidate promoted to a verified
        # LinkedIn parent leaves the worth pool for BOTH sides here, so they can't disagree by one.
        selection = views.workflow_state(self.db)["selection"]
        selection = {
            **selection,
            "fingerprint": str(selection.get("fingerprint") or selection.get("sha256") or ""),
        }
        subset = views.linkedin_review(
            self.db, "enrichment",
            include_plausibly_absent=self.include_plausibly_absent,
            include_candidates=self.include_candidates,
            confirm_threshold=self.confirm_threshold,
        )
        candidates = [row for row in subset if row.get("candidate_origin")]
        worth_skipped: list[str] = []
        queue = build_queue(subset, self.facts_dir, self.raw_dir)
        pending_queue, reused_completed = filter_already_done(queue, self.out_dir)
        duplicate_handles = max(0, len(queue) - len(pending_queue) - reused_completed)
        cost_per = PROCESSOR_PRICING_USD.get(self.processor, PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR])
        est_usd = round(len(pending_queue) * cost_per, 2)

        base = {
            "source": "reconcile_deep_research",
            "eligible": len(subset),
            "eligible_candidates": len(candidates),
            "candidates_skipped_not_added": len(worth_skipped),
            "would_submit": len(pending_queue),
            "reused_completed": reused_completed,
            "duplicate_handles": duplicate_handles,
            "processor": self.processor,
            "cost_per_person_usd": cost_per,
            "estimated_usd": est_usd,
            "budget_usd": self.budget,
            "selection": selection,
            "updated_at": now_iso(),
        }

        self.out_dir.mkdir(parents=True, exist_ok=True)
        with self.queue_csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS)
            w.writeheader()
            w.writerows(queue)

        projection_params = ResearchRunParams(
            input_csv=self.queue_csv,
            output_dir=self.out_dir,
            processor=self.processor,
            manifest=str(manifest_path) if manifest_path else "",
            db=self.db,
        )

        def inventory() -> list[dict[str, Any]] | None:
            return research_artifact_inventory(projection_params)

        def write_receipt(payload: dict[str, Any]) -> None:
            if not manifest_path:
                return
            payload = {**payload, "artifacts": inventory() or []}
            write_enrichment_manifest(payload, manifest_path)
            project_manifest(self.db, manifest_path)

        def receipt_body(status: str, result: dict[str, Any], *, completed: int = 0,
                         failed: int = 0) -> dict[str, Any]:
            """The one receipt shape — the dict `persist()` always wrote."""
            return {
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
                "processor": self.processor,
                "cost_per_person_usd": cost_per,
                "estimated_usd": est_usd,
                "budget_usd": self.budget,
                "input": {
                    "review_csv": str(self.overrides_csv),
                    "facts_dir": str(self.facts_dir),
                    "queue_csv": str(self.queue_csv),
                },
                "outputs": {
                    "research_dir": str(self.out_dir),
                    "review_csv": str(self.overrides_csv),
                },
                "privacy": {
                    "message_bodies_read": False,
                    "paid_provider_called": status in {STATUS_RUNNING, STATUS_RESEARCH_COMPLETE, STATUS_FAILED},
                },
                "result_status": result.get("status", ""),
                "error": (str(result.get("error") or result.get("research_error") or "")
                          if status == STATUS_FAILED else None),
                "artifacts": inventory(),
            }

        def finish(result: dict[str, Any], status: str, *, completed: int = 0,
                   failed: int = 0) -> ReconcileDeepResearchManifest:
            """Terminal: stash the emitted result; the Node template writes the receipt."""
            self.result = result
            return ReconcileDeepResearchManifest(
                source=manifest_path.parent.name if manifest_path else None,
                **receipt_body(status, result, completed=completed, failed=failed))

        # Judge each proposed retarget with the SAME identity judge attached links use, inside this
        # already-approved enrichment pass. --no-llm uses the deterministic fallback (never
        # auto-approves; rejects unverified / sub-threshold guesses). Owner background is a network
        # prior for the judge, same as reconcile_linkedin.
        use_llm = not self.no_llm
        owner_block = owner_background_block(load_owner()) if load_owner() else ""

        def heartbeat(done: int, total: int) -> None:
            if self.on_progress:
                self.on_progress({"status": "running", "phase": "judging_retargets",
                                  "counts": {"done": done, "total": total}})
            # Honest judging progress in the ONE fixed manifest (no new state files):
            # cheap per-completion writes the UI polls while a pass judges N proposals.
            if manifest_path:
                write_receipt({
                    "stage": "enrich", "status": STATUS_RUNNING,
                    "phase": "judging_retargets", "done": done, "total": total,
                    "counts": _manifest_counts(total=len(queue), completed=reused_completed),
                    "selection": selection,
                })

        def propose() -> dict[str, Any]:
            return propose_retargets_from_output(
                self.out_dir, subset, self.overrides_csv,
                db=self.db,
                facts_dir=self.facts_dir, raw_dir=self.raw_dir,
                use_llm=use_llm, owner_block=owner_block,
                model=self.model or "",
                effort=self.reasoning_effort or "medium",
                confirm_threshold=self.confirm_threshold, heartbeat=heartbeat)

        if not subset:
            return finish(
                {**base, "status": STATUS_NOOP, "queue_csv": str(self.queue_csv),
                 "reason": "no effective-Yes contacts need enrichment"},
                STATUS_RESEARCH_COMPLETE)

        if self.dry_run:
            return finish(
                {**base, "status": STATUS_DRY_RUN, "queue_csv": str(self.queue_csv),
                 "elapsed_ms": int((time.monotonic() - started) * 1000)},
                STATUS_NEEDS_APPROVAL, completed=reused_completed)

        if not pending_queue:
            proposals = propose()
            return finish(
                {**base, "status": STATUS_REUSED, "queue_csv": str(self.queue_csv),
                 "output_dir": str(self.out_dir),
                 "retargets_proposed": proposals.get("proposed", 0),
                 "judge_calls": proposals.get("judge_calls", 0),
                 "cached_verdicts": proposals.get("cached_verdicts", 0),
                 "grandfathered": proposals.get("grandfathered", 0),
                 "reason": "all eligible people already have completed Parallel research",
                 "elapsed_ms": int((time.monotonic() - started) * 1000)},
                STATUS_RESEARCH_COMPLETE, completed=len(queue))

        # Every paid run needs current-run approval, and the estimate must stay below
        # the ceiling the user approved. This gate returns BEFORE any Parallel.ai call
        # — the typed needs_approval payload (and its receipt) is the whole outcome.
        if not self.approve or est_usd > self.budget:
            return finish(
                {**base, "status": STATUS_NEEDS_APPROVAL, "queue_csv": str(self.queue_csv),
                 "message": f"deep research for {len(pending_queue)} net-new people is ~${est_usd:.2f} "
                            f"({reused_completed} completed reused, {duplicate_handles} duplicates skipped); "
                            f"get explicit approval, then re-run with --approve and "
                            f"an approved --budget at or above the estimate (current ${self.budget:.2f})",
                 "elapsed_ms": int((time.monotonic() - started) * 1000)},
                STATUS_NEEDS_APPROVAL, completed=reused_completed)

        # Delegate the spend to the existing Parallel.ai primitive, IN PROCESS
        # (import and call, branch on the returned payload — never a subprocess
        # of our own .py). Its progress prints go to stderr already, so the run
        # stays a live narrated stream and our stdout stays clean for the final
        # JSON manifest.
        print(f"[deep-research] researching {len(pending_queue)} net-new people via Parallel.ai ({self.processor}); "
              "this can take several minutes — live progress below:", file=sys.stderr, flush=True)
        # Mid-run receipt: the browser must see "running" while the research runs.
        if manifest_path:
            write_receipt(receipt_body(
                STATUS_RUNNING, {**base, "status": STATUS_RUNNING},
                completed=reused_completed))
        # The old subprocess boundary turned ANY crash into a failed receipt;
        # mirror that so an SDK/auth exception still lands as STATUS_FAILED
        # instead of killing the pass mid-manifest.
        try:
            research = run_research(ResearchRunParams(
                input_csv=self.queue_csv,
                output_dir=self.out_dir,
                processor=self.processor,
                manifest=str(manifest_path) if manifest_path else "",
                on_progress=self.on_progress,
                db=self.db,
            ))
        except SystemExit as exc:
            research = {"status": "failed", "error": f"SystemExit: {exc}"}
        except Exception as exc:
            research = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        research_status = str(research.get("status") or "failed")
        research_ok = research_status in RESEARCH_OK_STATUSES
        print(f"[deep-research] research finished ({research_status}).", file=sys.stderr, flush=True)
        # Propose retargets (pending) for any correct LinkedIn the research found.
        proposals = {"proposed": 0}
        if research_ok:
            proposals = propose()
        result = {
            **base, "status": STATUS_RAN if research_ok else STATUS_FAILED,
            "queue_csv": str(self.queue_csv), "output_dir": str(self.out_dir),
            "retargets_proposed": proposals.get("proposed", 0),
            "judge_calls": proposals.get("judge_calls", 0),
            "cached_verdicts": proposals.get("cached_verdicts", 0),
            "grandfathered": proposals.get("grandfathered", 0),
            "research_status": research_status,
            "research_error": research.get("error", ""),
            "progress": "streamed live to stderr",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        return finish(
            result,
            STATUS_RESEARCH_COMPLETE if research_ok else STATUS_FAILED,
            completed=len(queue) if research_ok else reused_completed,
            failed=0 if research_ok else len(pending_queue))


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
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--db", default=str(CANONICAL_DB),
                   help="Canonical Deep Context SQLite database")
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
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(
            f"Deep Context database is missing: {db_path}; "
            "run the explicit legacy import first"
        )
    try:
        db = Db(db_path)
    except StoreError as exc:
        raise SystemExit(f"Deep Context database is unsupported: {db_path}: {exc}") from exc
    node = ReconcileDeepResearch(
        verdicts_jsonl=Path(args.verdicts_jsonl),
        overrides_csv=Path(args.overrides_csv),
        people_csv=Path(args.people_csv),
        facts_dir=Path(args.facts_dir),
        index_json=Path(args.index_json),
        raw_dir=Path(args.raw_dir),
        manifest=args.manifest,
        processor=args.processor,
        confirm_threshold=args.confirm_threshold,
        budget=args.budget,
        approve=args.approve,
        dry_run=args.dry_run,
        include_plausibly_absent=args.include_plausibly_absent,
        include_candidates=args.include_candidates,
        no_llm=args.no_llm,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        db=db,
    )
    node.run()
    # The emitted result and the manifest receipt are different shapes by
    # existing contract (see ReconcileDeepResearchManifest); emit the result.
    emit(node.result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
