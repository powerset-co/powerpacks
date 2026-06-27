"""[Phase 3, escalation] Re-research people whose attached LinkedIn was WRONG.

Recovers the correct identity for detaches from BOTH sources: a high-confidence
`wrong_person` the judge flagged, AND a link the user marked DETACH in the review table
(`review.csv` action=detach, approved=yes) — so running this AFTER you review picks up
your decisions too. It leans on local history first (the dossier we already built), then
hands the still-unresolved subset to the existing Parallel.ai deep-research primitive.

Cost-gated, NOT opt-in: estimate the Parallel.ai spend; if it is within the budget
(default $25) it runs automatically; above the budget it stops and asks for approval
(pass a higher --budget). People the judge flagged `linkedin_plausibly_absent` are
EXCLUDED — some people legitimately have no LinkedIn and "no profile exists" is a valid
final answer; we never force a match.

Reuses `packs/messages/primitives/deep_research_contacts` (Parallel.ai core2x) — this
step only builds the research queue, estimates cost, enforces the gate, and shells out.

Outputs (under .powerpacks/deep-context/reconcile/deep-research/):
  research_queue.csv     queue handed to deep_research_contacts
  manifest.json          subset size, estimated cost, gate decision, run status
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    RAW_DIR,
    RECONCILE_DIR,
    VERDICTS_JSONL,
    emit,
    now_iso,
    parse_list,
    write_json,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import load_override_rows, upsert_retargets
from packs.ingestion.schemas.people_schema import extract_public_identifier
# Reuse the canonical pricing from the deep-research primitive (don't mirror/drift).
from packs.messages.primitives.deep_research_contacts.deep_research_contacts import PROCESSOR_PRICING_USD

DEFAULT_PROCESSOR = "core2x"
DEFAULT_BUDGET = 25.0
DR_OUT_DIR = RECONCILE_DIR / "deep-research"
QUEUE_CSV = DR_OUT_DIR / "research_queue.csv"
QUEUE_FIELDS = ["handle", "display_name", "bio", "known_info", "primary_email",
                "phone_e164", "area_code", "source_channel", "retarget_hint"]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


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


def eligible_subset(verdicts: list[dict[str, Any]], threshold: float,
                    overrides: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Detaches that external research could resolve — from BOTH the model and the user.

    Two ways a link becomes eligible:
      * MODEL: a high-confidence `wrong_person` detach the judge flagged `recommend_deep_research`,
        whose PARENT did not already keep a confirmed link (conflict-resolved siblings already know
        the correct LinkedIn — re-researching them is wasteful).
      * USER: a link the user marked DETACH in the review table (`action=detach, approved=yes`).
        We honor that regardless of the judge's recommendation — if you say it's the wrong person,
        we try to find the right one.

    Skipped either way: a link the user `exclude`d (don't want this person at all — never
    re-attach them), one that already has a `retarget` (we know the correct one), or one the
    judge flagged `linkedin_plausibly_absent` (some people legitimately have no profile)."""
    overrides = overrides or {}
    has_retarget = {pub for pub, r in overrides.items()
                    if (r.get("action") or "").strip().lower() == "retarget"}
    excluded = {pub for pub, r in overrides.items()
                if (r.get("action") or "").strip().lower() == "exclude"}
    user_detached = {pub for pub, r in overrides.items()
                     if (r.get("action") or "").strip().lower() == "detach"
                     and (r.get("approved") or "").strip().lower() == "yes"}
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
        if v.get("linkedin_plausibly_absent") or (pub and (pub in seen or pub in has_retarget or pub in excluded)):
            continue
        model_ok = (v.get("verdict") == "wrong_person"
                    and float(v.get("confidence") or 0) >= threshold
                    and v.get("recommend_deep_research")
                    and r.get("parent_slug") not in parents_with_kept)
        if model_ok or (pub and pub in user_detached):
            if pub:
                seen.add(pub)
            out.append(r)
    return out


def _dossier_bio(child_pids: list[str], facts_dir: Path, raw_dir: Path) -> str:
    records: list[dict[str, Any]] = []
    for pid in child_pids:
        path = facts_dir / f"{pid}.jsonl"
        if path.exists():
            records.extend(json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    merged = compose.merge_facts(records) if records else {}
    parts = []
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
    return ". ".join(parts)


def build_queue(subset: list[dict[str, Any]], people: dict[str, dict[str, str]],
                facts_dir: Path, raw_dir: Path) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    for r in subset:
        pids = r.get("person_ids") or []
        row = next((people[p] for p in pids if p in people), {})
        emails = [row.get("primary_email", "")] + parse_list(row.get("all_emails"))
        phones = [row.get("primary_phone", "")] + parse_list(row.get("all_phones"))
        email = next((e for e in emails if e and "@" in e), "")
        phone = next((p for p in phones if p), "")
        rejected = (r.get("linkedin") or {}).get("linkedin_url", "")
        hint = (f"The previously attached LinkedIn {rejected} was judged WRONG "
                f"({(r.get('verdict') or {}).get('reason', '')}). Find the correct person.")
        queue.append({
            "handle": r.get("parent_slug", ""),
            "display_name": r.get("name", ""),
            "bio": _dossier_bio(pids, facts_dir, raw_dir),
            "known_info": hint,
            "primary_email": email,
            "phone_e164": phone,
            "area_code": "",
            "source_channel": "email" if email else "phone",
            "retarget_hint": hint,
        })
    return queue


def _dig(profile: dict[str, Any], key: str) -> str:
    """Defensively pull a field from the deep-research result (top-level or nested)."""
    if not isinstance(profile, dict):
        return ""
    for loc in (profile, profile.get("research") or {}, profile.get("profile") or {}):
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
            return f"deep research: {val}"[:300]
    return "deep research found a correct LinkedIn"


def propose_retargets_from_output(out_dir: Path, subset: list[dict[str, Any]],
                                  overrides_csv: Path) -> dict[str, Any]:
    """After deep research, propose a `retarget` (pending) for each detached person whose
    research found a correct LinkedIn — into the same decisions table (sticky upsert)."""
    proposals = []
    for r in subset:
        handle = r.get("parent_slug", "")
        profile = _read_json(out_dir / handle / "01_research_parallel.json")
        new_url = _find_linkedin(profile)
        old_pub = (r.get("candidate_key") or extract_public_identifier((r.get("linkedin") or {}).get("linkedin_url", ""))).lower()
        if not new_url or not old_pub:
            continue
        proposals.append({
            "old_public_identifier": old_pub, "new_linkedin_url": new_url,
            "linkedin_url": (r.get("linkedin") or {}).get("linkedin_url", ""),
            "match_emails": r.get("match_emails") or [], "match_phones": r.get("match_phones") or [],
            "person_id": (r.get("person_ids") or [""])[0], "confidence": 0.0,
            "reason": _find_reason(profile), "source": "deep-research",
        })
    return upsert_retargets(overrides_csv, proposals)


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    verdicts = _read_jsonl(Path(args.verdicts_jsonl))
    overrides = load_override_rows(Path(args.overrides_csv))
    subset = eligible_subset(verdicts, args.confirm_threshold, overrides)
    user_detached_eligible = sum(
        1 for r in subset
        if (overrides.get((r.get("candidate_key") or "").strip().lower(), {}).get("action") or "").lower() == "detach"
        and (overrides.get((r.get("candidate_key") or "").strip().lower(), {}).get("approved") or "").lower() == "yes")
    people = load_people_rows(Path(args.people_csv))
    cost_per = PROCESSOR_PRICING_USD.get(args.processor, PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR])
    est_usd = round(len(subset) * cost_per, 2)

    base = {
        "source": "reconcile_deep_research",
        "eligible": len(subset),
        "eligible_from_user_detach": user_detached_eligible,
        "processor": args.processor,
        "cost_per_person_usd": cost_per,
        "estimated_usd": est_usd,
        "budget_usd": args.budget,
        "updated_at": now_iso(),
    }

    if not subset:
        return {**base, "status": "noop", "reason": "no wrong_person detaches to research (model or user)"}

    DR_OUT_DIR.mkdir(parents=True, exist_ok=True)
    queue = build_queue(subset, people, Path(args.facts_dir), Path(args.raw_dir))
    with QUEUE_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS)
        w.writeheader()
        w.writerows(queue)

    # $25 gate: auto-run within budget; above it, stop and ask for approval.
    if est_usd > args.budget and not args.approve:
        return {**base, "status": "needs_approval", "queue_csv": str(QUEUE_CSV),
                "message": f"deep research for {len(subset)} people is ~${est_usd:.2f} (> ${args.budget:.0f}); "
                           f"re-run with --approve (or raise --budget) to proceed",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    if args.dry_run:
        return {**base, "status": "dry_run", "queue_csv": str(QUEUE_CSV),
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    # Delegate the spend to the existing Parallel.ai primitive (reuse, don't rebuild).
    # STREAM its output live to our stderr (the primitive prints `[deep_research_contacts]
    # poll status ...` every poll) so the run isn't a silent black box — Parallel.ai jobs can
    # take minutes. Keep our own stdout clean for the final JSON manifest.
    print(f"[deep-research] researching {len(subset)} people via Parallel.ai ({args.processor}); "
          "this can take several minutes — live progress below:", file=sys.stderr, flush=True)
    cmd = [sys.executable, "-m", "packs.messages.primitives.deep_research_contacts.deep_research_contacts",
           "run", "--input", str(QUEUE_CSV), "--output-dir", str(DR_OUT_DIR), "--processor", args.processor]
    proc = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, text=True)
    print(f"[deep-research] research process exited ({proc.returncode}).", file=sys.stderr, flush=True)
    # Propose retargets (pending) for any correct LinkedIn the research found.
    proposals = {"proposed": 0}
    if proc.returncode == 0:
        proposals = propose_retargets_from_output(DR_OUT_DIR, subset, Path(args.overrides_csv))
    return {
        **base, "status": "ran" if proc.returncode == 0 else "failed",
        "queue_csv": str(QUEUE_CSV), "output_dir": str(DR_OUT_DIR),
        "retargets_proposed": proposals.get("proposed", 0),
        "returncode": proc.returncode, "progress": "streamed live to stderr",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deep-research the correct identity for wrong_person detaches (cost-gated).")
    p.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    p.add_argument("--overrides-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--processor", default=DEFAULT_PROCESSOR, choices=sorted(PROCESSOR_PRICING_USD))
    p.add_argument("--confirm-threshold", type=float, default=0.85)
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="Auto-approve ceiling (USD)")
    p.add_argument("--approve", action="store_true", help="Approve spend above --budget")
    p.add_argument("--dry-run", action="store_true", help="Build the queue + estimate only; no Parallel.ai spend")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
