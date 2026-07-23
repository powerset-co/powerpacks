"""Migrate legacy Parallel LinkedIn resolutions into the reviewable retarget format.

The retired gmail orchestrator web-researched contacts email-by-email and attached
the found LinkedIn (accepted at >=0.75) directly to their people rows — no judge,
no review queue. Today's import replays those stored links forever, while
deep-context never audits them (the people are outside its population), so a
wrong-person link stays silently wrong.

This migrator turns each still-unverified legacy link into the NEW format: a
pending `retarget` proposal in overrides/review.csv — the exact shape
deep-research proposals use — so the EXISTING machinery takes over: the retarget
judge verifies the profile against the person's message-derived dossier facts,
auto-stand rules absorb the confident verdicts, the Check-LinkedIn queue shows the
ambiguous rest, and an approved row flows through apply_retargets (cache-first
enrichment) into the fan-in. Nothing new to operate; the legacy links simply
enter the loop instead of bypassing it.

Scope per person (all conditions):
  - gmail people row with enrichment_provider=parallel_linkedin_resolution
  - NOT already in merged/people.csv (those were admitted via enrichment and are
    corroborated by the LinkedIn-import lane)
  - has a facts file (the judge's evidence; no facts -> nothing to verify against)
  - no user decision and no already-judged retarget on the row (sticky upsert
    preserves user rows regardless — the skip just keeps counts honest)

Default is a dry run (counts only, no writes, no spend). `--apply` writes pending
proposals. `--apply --judge` additionally judges each proposal against the CACHED
RapidAPI profile (profile_cache_v2) through the same judge as deep-research
proposals — spend-bearing unless `--no-llm` (deterministic fallback, tests only).

Run: uv run --project . python -m packs.ingestion.primitives.deep_context.migrate_legacy_resolutions
"""
from __future__ import annotations

import argparse
import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    emit,
    now_iso,
)
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    judge_concurrency,
    proposal_fingerprint,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    dossier_view,
    judge_research_proposal,
    research_proposal_task,
    research_reject_fields,
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.review_store import (
    RESEARCH_CONFIRM_THRESHOLD,
    load_override_rows,
)

LEGACY_PROVIDER = "parallel_linkedin_resolution"
GMAIL_PEOPLE_CSV = Path(".powerpacks/network-import/import/gmail/people.csv")
DIRECTORY_CSV = Path(".powerpacks/network-import/directory.csv")


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_json(path: Path) -> dict[str, Any]:
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def cache_profile_view(record: dict[str, Any]) -> dict[str, Any]:
    """Judge-facing identity view of a CACHED RapidAPI profile.

    Mirrors the role of the deep-research profile view (name/headline/positions/
    education/location evidence for the retarget judge) but is built from
    profile_cache_v2 — the legacy links' profiles are already cached, so the
    judge pass costs no provider calls. Fingerprints stay internally consistent
    because migration and its judge both use this builder."""
    prof = record.get("normalized_profile") or {}
    if not isinstance(prof, dict) or not prof.get("success"):
        return {}
    positions = []
    for exp in prof.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        label = " — ".join(part for part in (
            str(exp.get("title") or "").strip(), str(exp.get("company") or "").strip()) if part)
        if label:
            positions.append(label)
    education = []
    for row in prof.get("education") or []:
        if not isinstance(row, dict):
            continue
        label = ", ".join(part for part in (
            str(row.get("degree") or "").strip(), str(row.get("school") or "").strip()) if part)
        if label:
            education.append(label)
    return {
        "name": str(prof.get("full_name") or ""),
        "headline": str(prof.get("headline") or ""),
        "location": str(prof.get("location_str") or ""),
        "positions": positions[:8],
        "education": education[:4],
        "summary": str(record.get("simple_summary") or prof.get("summary") or "")[:400],
        "source": "profile_cache_v2",
    }


def legacy_provenance(directory_csv: Path) -> dict[str, dict[str, str]]:
    """pub -> best {confidence, reasoning, email} the legacy era recorded in directory.csv."""
    best: dict[str, dict[str, str]] = {}
    for row in _read_rows(directory_csv):
        pub = (row.get("public_identifier") or "").strip().lower()
        if not pub or (row.get("status") or "").strip().lower() != "found":
            continue
        try:
            conf = float(row.get("confidence") or 0)
        except ValueError:
            conf = 0.0
        prior = best.get(pub)
        if prior is None or conf > float(prior.get("confidence") or 0):
            best[pub] = {
                "confidence": f"{conf:.2f}",
                "reasoning": (row.get("reasoning") or "").strip(),
                "email": (row.get("email") or "").strip().lower(),
            }
    return best


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    facts_dir = Path(args.facts_dir)
    raw_dir = Path(args.raw_dir)
    cache_dir = Path(args.cache_dir)
    overrides_csv = Path(args.overrides)

    merged_ids = {(r.get("id") or "").strip().lower()
                  for r in _read_rows(Path(args.merged_people))} - {""}
    overrides = load_override_rows(overrides_csv)
    provenance = legacy_provenance(Path(args.directory_csv))

    counts = {
        "legacy_rows": 0, "eligible": 0,
        "skipped_in_merged": 0, "skipped_no_facts": 0,
        "skipped_user_decided": 0, "skipped_already_judged": 0,
        "no_cached_profile": 0, "judged": 0,
    }
    proposals: list[dict[str, Any]] = []
    seen_pubs: set[str] = set()
    for row in _read_rows(Path(args.gmail_people)):
        if (row.get("enrichment_provider") or "").strip().lower() != LEGACY_PROVIDER:
            continue
        pub = (row.get("public_identifier") or "").strip().lower()
        url = (row.get("linkedin_url") or "").strip()
        pid = (row.get("id") or "").strip()
        if not pub or not url or not pid or pub in seen_pubs:
            continue
        seen_pubs.add(pub)
        counts["legacy_rows"] += 1
        if pid.lower() in merged_ids:
            counts["skipped_in_merged"] += 1
            continue
        prior = overrides.get(pub) or {}
        if (prior.get("approved") or "").strip().lower() in {"yes", "no"}:
            counts["skipped_user_decided"] += 1
            continue
        if ((prior.get("action") or "").strip().lower() == "retarget"
                and (prior.get("llm_judge_fingerprint") or "").strip()):
            counts["skipped_already_judged"] += 1
            continue
        if not (facts_dir / f"{pid}.jsonl").exists():
            counts["skipped_no_facts"] += 1
            continue
        counts["eligible"] += 1
        prov = provenance.get(pub) or {}
        confidence = float(prov.get("confidence") or 0)
        reasoning = prov.get("reasoning") or ""
        email = prov.get("email") or ""
        proposal: dict[str, Any] = {
            "old_public_identifier": pub,
            "new_linkedin_url": url,
            "linkedin_url": url,
            "match_emails": [email] if email else [],
            "match_phones": [],
            "person_id": pid,
            "confidence": confidence,
            "reason": (f"migrated legacy parallel resolution "
                       f"(legacy conf {confidence:.2f}): {reasoning[:200]}").strip().rstrip(":"),
            "source": "legacy-migration",
        }
        view = cache_profile_view(_read_json(cache_dir / f"{pub}.json"))
        if not view:
            counts["no_cached_profile"] += 1
        # Judging is APPLY-only: a dry run must stay $0 (it reports the would-judge
        # count + cost estimate instead of calling the provider).
        if args.judge and view and args.apply:
            dossier = dossier_view([pid], facts_dir, raw_dir)
            proposal["_judge_task"] = research_proposal_task(
                dossier, view, name=(row.get("full_name") or "").strip(),
                match_emails=proposal["match_emails"], confidence=confidence,
                unverified=True)  # legacy links skipped verification by construction
            proposal["judge_fingerprint"] = proposal_fingerprint(pub, url, dossier, view)
        proposals.append(proposal)
        if args.limit and len(proposals) >= args.limit:
            break

    pending = [p for p in proposals if "_judge_task" in p]
    if pending and args.apply:
        # Bounded fan-out, mirroring propose_retargets_from_output: each judge call is a
        # self-contained sync wrapper, so a thread pool keeps its retry/timeout semantics.
        with ThreadPoolExecutor(max_workers=min(judge_concurrency(), len(pending))) as pool:
            futures = {pool.submit(
                judge_research_proposal, p.pop("_judge_task"), use_llm=not args.no_llm,
                model=args.model, effort=args.reasoning_effort, timeout=args.timeout,
                max_retries=args.max_retries): p for p in pending}
            for future in as_completed(futures):
                futures[future].update(research_reject_fields(future.result(), args.confirm_threshold))
                counts["judged"] += 1

    manifest: dict[str, Any] = {
        "source": "migrate_legacy_resolutions",
        "status": "dry_run" if not args.apply else "completed",
        **counts,
        "proposals": len(proposals),
        "overrides_csv": str(overrides_csv),
        "judge": bool(args.judge),
        "confirm_threshold": args.confirm_threshold,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
    if not args.apply:
        if args.judge:
            per_lo, per_hi = 0.004, 0.02
            would = counts["eligible"] - counts["no_cached_profile"]
            manifest["estimated_judge_cost_usd_low"] = round(would * per_lo, 2)
            manifest["estimated_judge_cost_usd_high"] = round(would * per_hi, 2)
        return manifest
    manifest.update(upsert_retargets(overrides_csv, proposals))
    manifest["status"] = "completed"
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Migrate legacy Parallel LinkedIn resolutions into pending retarget proposals.")
    p.add_argument("--gmail-people", default=str(GMAIL_PEOPLE_CSV))
    p.add_argument("--merged-people", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--directory-csv", default=str(DIRECTORY_CSV))
    p.add_argument("--overrides", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD)
    p.add_argument("--limit", type=int, default=0, help="Cap migrated people (0 = all)")
    p.add_argument("--apply", action="store_true", help="Write proposals (default: dry run)")
    p.add_argument("--judge", action="store_true",
                   help="Also judge each proposal against its CACHED profile (spend unless --no-llm)")
    p.add_argument("--no-llm", action="store_true", help="Deterministic judge fallback (tests only)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=6)
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
