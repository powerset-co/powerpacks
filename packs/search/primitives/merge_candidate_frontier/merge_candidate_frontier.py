#!/usr/bin/env python3
"""Task 4 — Merge and dedupe candidates across probe CSVs into a candidate frontier.

Reads probe_summaries.json + the individual probe result CSVs.  Deduplicates by
person_id (primary) and linkedin_url (secondary).  Writes:

  candidate_frontier.json   — schema-conforming frontier
  candidate_frontier.jsonl  — one JSON object per candidate (streaming-friendly)
  candidates.debug.csv      — flat CSV for quick eyeballing
  merge_summary.json        — counts, overlap stats, per-probe yield

Usage:

    uv run --project . python packs/search/primitives/merge_candidate_frontier/merge_candidate_frontier.py \
        --run-dir .powerpacks/search-network-jd/<slug>/ \
        --plan-json plan.json

Or specify probe_summaries.json explicitly:

    ... --probe-summaries .powerpacks/search-network-jd/<slug>/probe_summaries.json \
        --plan-json .powerpacks/search-network-jd/<slug>/plan.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def normalize_linkedin_url(url: str | None) -> str | None:
    """Return a canonical linkedin.com/in/<slug> or None."""
    if not url:
        return None
    url = url.strip().rstrip("/")
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.IGNORECASE)
    if m:
        return f"https://linkedin.com/in/{m.group(1).lower()}"
    return url.lower() if "linkedin.com" in url.lower() else None


def public_identifier_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.IGNORECASE)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def read_probe_csv(csv_path: Path, probe_id: str) -> list[dict[str, Any]]:
    """Read a single probe result CSV and return enriched row dicts."""
    rows: list[dict[str, Any]] = []
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row_num, row in enumerate(reader, start=1):
            rows.append({
                "probe_id": probe_id,
                "csv": str(csv_path),
                "row_number": row_num,
                "rank": _int_or_none(row.get("rank")),
                "score": _float_or_none(row.get("final_score")),
                "person_id": (row.get("person_id") or "").strip() or None,
                "linkedin_url": (row.get("linkedin_url") or "").strip() or None,
                "name": (row.get("name") or "").strip() or None,
                "headline": (row.get("headline") or "").strip() or None,
                "location": (row.get("location") or "").strip() or None,
                "current_titles": (row.get("current_titles") or "").strip() or None,
                "current_companies": (row.get("current_companies") or "").strip() or None,
                "source_operator": (row.get("source_operator") or "").strip() or None,
                "source_channel": (row.get("source_channel") or "").strip() or None,
                "overall_reasoning": (row.get("overall_reasoning") or "").strip() or None,
                "hydrated": row.get("hydrated", "").strip().lower() == "true",
                "source_run": (row.get("source_run") or "").strip() or None,
            })
    return rows


def _int_or_none(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _float_or_none(val: str | None) -> float | None:
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Merge / dedupe
# ---------------------------------------------------------------------------

def merge_candidates(
    probe_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dedupe rows by person_id (primary) then linkedin_url (fallback).

    Returns a list of frontier candidate dicts conforming to the schema.
    """
    # Two-pass dedup: person_id first, then linkedin_url for rows without person_id
    by_person_id: dict[str, list[dict[str, Any]]] = {}
    by_linkedin: dict[str, list[dict[str, Any]]] = {}
    no_key: list[dict[str, Any]] = []

    for row in probe_rows:
        pid = row.get("person_id")
        url = normalize_linkedin_url(row.get("linkedin_url"))
        if pid:
            by_person_id.setdefault(pid, []).append(row)
        elif url:
            by_linkedin.setdefault(url, []).append(row)
        else:
            no_key.append(row)

    # Merge linkedin groups that share a person_id from any row
    # (in case one probe had person_id and another didn't)
    linkedin_to_person: dict[str, str] = {}
    for pid, rows in by_person_id.items():
        for r in rows:
            url = normalize_linkedin_url(r.get("linkedin_url"))
            if url:
                linkedin_to_person[url] = pid

    for url, rows in list(by_linkedin.items()):
        pid = linkedin_to_person.get(url)
        if pid:
            by_person_id.setdefault(pid, []).extend(rows)
            del by_linkedin[url]

    candidates: list[dict[str, Any]] = []

    # Process person_id groups
    for pid, rows in by_person_id.items():
        candidates.append(_build_candidate(rows, person_id=pid))

    # Process linkedin-only groups
    for url, rows in by_linkedin.items():
        candidates.append(_build_candidate(rows, person_id=None))

    # Process orphan rows (no person_id, no linkedin_url)
    for row in no_key:
        candidates.append(_build_candidate([row], person_id=None))

    # Sort: highest best-score first, then by number of probe matches desc
    candidates.sort(key=lambda c: (
        -len(c["matched_probe_ids"]),
        -(c.get("_best_score") or 0),
        c["name"] or "",
    ))

    # Assign stable candidate_id and strip internal sort key
    for cand in candidates:
        cand.pop("_best_score", None)

    return candidates


def _build_candidate(rows: list[dict[str, Any]], person_id: str | None) -> dict[str, Any]:
    """Build a single frontier candidate from one or more source rows."""
    # Pick best row for display fields (highest score)
    best = max(rows, key=lambda r: r.get("score") or 0)

    probe_ids = sorted(set(r["probe_id"] for r in rows))
    linkedin_url = normalize_linkedin_url(best.get("linkedin_url"))
    # Source attribution is the same person across probes; take the first
    # non-empty operator/channel seen.
    source_operator = next((r.get("source_operator") for r in rows if r.get("source_operator")), None)
    source_channel = next((r.get("source_channel") for r in rows if r.get("source_channel")), None)

    source_rows = []
    for r in rows:
        sr: dict[str, Any] = {
            "probe_id": r["probe_id"],
            "csv": r["csv"],
            "row_number": r["row_number"],
        }
        if r.get("rank") is not None:
            sr["rank"] = r["rank"]
        if r.get("score") is not None:
            sr["score"] = r["score"]
        source_rows.append(sr)

    # profile_context_ref: point to the hydrated profile JSONL row via
    # source_run id.  The harness can load the profile from there.
    profile_ref = None
    for r in rows:
        if r.get("hydrated") and r.get("source_run"):
            profile_ref = r["source_run"]
            break

    candidate_id = person_id or f"anon-{uuid4()}"

    return {
        "candidate_id": candidate_id,
        "person_id": person_id,
        "public_identifier": public_identifier_from_url(linkedin_url),
        "linkedin_url": linkedin_url,
        "name": best.get("name"),
        "current_role": best.get("current_titles"),
        "current_company": best.get("current_companies"),
        "location": best.get("location"),
        "source_operator": source_operator,
        "source_channel": source_channel,
        "matched_probe_ids": probe_ids,
        "duplicate_signal": {
            "matched_probe_count": len(probe_ids),
            "matched_probe_ids": probe_ids,
        },
        "source_rows": source_rows,
        "profile_context_ref": profile_ref,
        "_best_score": best.get("score"),
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

DEBUG_CSV_FIELDS = [
    "candidate_id",
    "person_id",
    "name",
    "current_role",
    "current_company",
    "location",
    "linkedin_url",
    "matched_probe_count",
    "matched_probe_ids",
    "source_operator",
    "source_channel",
    "best_score",
    "source_row_count",
    "profile_context_ref",
]


def write_debug_csv(path: Path, candidates: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DEBUG_CSV_FIELDS)
        writer.writeheader()
        for c in candidates:
            best_score = max(
                (sr.get("score") or 0 for sr in c.get("source_rows", [])),
                default=0,
            )
            writer.writerow({
                "candidate_id": c["candidate_id"],
                "person_id": c.get("person_id") or "",
                "name": c.get("name") or "",
                "current_role": c.get("current_role") or "",
                "current_company": c.get("current_company") or "",
                "location": c.get("location") or "",
                "linkedin_url": c.get("linkedin_url") or "",
                "source_operator": c.get("source_operator") or "",
                "source_channel": c.get("source_channel") or "",
                "matched_probe_count": c["duplicate_signal"]["matched_probe_count"],
                "matched_probe_ids": "; ".join(c["matched_probe_ids"]),
                "best_score": f"{best_score:.2f}" if best_score else "",
                "source_row_count": len(c.get("source_rows", [])),
                "profile_context_ref": c.get("profile_context_ref") or "",
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir) if args.run_dir else None

    # Resolve probe_summaries
    if args.probe_summaries:
        summaries_path = Path(args.probe_summaries)
    elif run_dir:
        summaries_path = run_dir / "probe_summaries.json"
    else:
        print("error: --run-dir or --probe-summaries required", file=sys.stderr)
        sys.exit(1)

    if not summaries_path.exists():
        print(f"error: probe_summaries not found: {summaries_path}", file=sys.stderr)
        sys.exit(1)

    # Resolve plan.json
    if args.plan_json:
        plan_path = Path(args.plan_json)
    elif run_dir:
        plan_path = run_dir / "plan.json"
    else:
        print("error: --run-dir or --plan-json required", file=sys.stderr)
        sys.exit(1)

    if not plan_path.exists():
        print(f"error: plan.json not found: {plan_path}", file=sys.stderr)
        sys.exit(1)

    # Output dir
    out_dir = Path(args.out_dir) if args.out_dir else (run_dir or summaries_path.parent)

    # Load probe summaries
    summaries = json.loads(summaries_path.read_text())
    if isinstance(summaries, dict):
        probe_list = summaries.get("probes") or summaries.get("probe_summaries") or []
    elif isinstance(summaries, list):
        probe_list = summaries
    else:
        print("error: probe_summaries must be a list or object with probes key", file=sys.stderr)
        sys.exit(1)

    # Collect all rows from completed probe CSVs
    all_rows: list[dict[str, Any]] = []
    input_probe_runs: list[str] = []
    skipped = 0

    for probe in probe_list:
        probe_id = probe.get("id", "unknown")
        status = probe.get("status", "")
        csv_path_str = probe.get("csv")

        if status != "completed" or not csv_path_str:
            skipped += 1
            continue

        csv_path = Path(csv_path_str)
        if not csv_path.exists():
            print(f"warn: CSV not found for probe {probe_id}: {csv_path}", file=sys.stderr)
            skipped += 1
            continue

        rows = read_probe_csv(csv_path, probe_id)
        all_rows.extend(rows)
        input_probe_runs.append(probe_id)

    if not all_rows:
        print("error: no candidate rows found across probe CSVs", file=sys.stderr)
        sys.exit(1)

    # Merge / dedupe
    candidates = merge_candidates(all_rows)

    # Build frontier document
    frontier = {
        "plan_json": str(plan_path),
        "created_at": now_iso(),
        "input_probe_runs": input_probe_runs,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    # Write outputs
    frontier_json_path = out_dir / "candidate_frontier.json"
    frontier_jsonl_path = out_dir / "candidate_frontier.jsonl"
    debug_csv_path = out_dir / "candidates.debug.csv"
    summary_path = out_dir / "merge_summary.json"

    write_json(frontier_json_path, frontier)
    write_jsonl(frontier_jsonl_path, candidates)
    write_debug_csv(debug_csv_path, candidates)

    # Per-probe yield
    probe_yield: dict[str, int] = {}
    for row in all_rows:
        pid = row["probe_id"]
        probe_yield[pid] = probe_yield.get(pid, 0) + 1

    # Overlap: candidates appearing in >1 probe
    multi_probe = [c for c in candidates if len(c["matched_probe_ids"]) > 1]

    summary = {
        "created_at": now_iso(),
        "plan_json": str(plan_path),
        "probe_summaries_json": str(summaries_path),
        "input_probe_count": len(input_probe_runs),
        "skipped_probe_count": skipped,
        "total_source_rows": len(all_rows),
        "unique_candidates": len(candidates),
        "multi_probe_candidates": len(multi_probe),
        "per_probe_yield": probe_yield,
        "outputs": {
            "candidate_frontier_json": str(frontier_json_path),
            "candidate_frontier_jsonl": str(frontier_jsonl_path),
            "candidates_debug_csv": str(debug_csv_path),
            "merge_summary_json": str(summary_path),
        },
    }

    write_json(summary_path, summary)

    # Print summary to stdout for harness
    print(json.dumps(summary, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge and dedupe probe CSVs into a candidate frontier"
    )
    parser.add_argument("--run-dir", help="JD run directory containing probe_summaries.json and plan.json")
    parser.add_argument("--probe-summaries", help="Explicit path to probe_summaries.json")
    parser.add_argument("--plan-json", help="Explicit path to plan.json")
    parser.add_argument("--out-dir", help="Output directory (defaults to run-dir)")

    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
