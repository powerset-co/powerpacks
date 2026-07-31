#!/usr/bin/env python3
"""Task 5b — Export a sendable shortlist CSV from captured evaluations.

Reads candidate_evaluations.json and candidate-frontier.json, filters to
top_tier/high_potential verdicts, and writes a clean shortlist.csv suitable
for sharing with hiring managers.

Usage:

    uv run --project . python packs/search/primitives/export_candidate_shortlist/export_candidate_shortlist.py \
        --run-dir .powerpacks/search-network-jd/<slug>/

Or with explicit paths:

    ... --evaluations-json candidate_evaluations.json \
        --frontier-json candidate-frontier.json \
        --out-dir .powerpacks/search-network-jd/<slug>/
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Legacy standalone sendable-shortlist shape. Do not add debug fields (JD Score,
# Verdict, Caveats, Matched Probes, Person ID, ...); the automatic deep loop emits
# JSON shortlist/bench artifacts and does not invoke this exporter.
SHORTLIST_FIELDS = [
    "Rank",
    "Name",
    "LinkedIn URL",
    "Current Role",
    "Current Company",
    "Source",
    "Channel",
    "Rationale",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def req_summary(reqs: list[dict[str, Any]]) -> str:
    """Compact one-line summary of requirement assessments."""
    parts = []
    for r in reqs:
        status = r.get("status", "?")
        trait = r.get("trait", "?")
        icon = {
            "doing_now": "★", "experienced": "✓", "capable": "+", "foundational": "~",
            "thin": "✗", "missing": "—", "unknown": "?",
            # legacy
            "strong": "✓", "partial": "~", "weak": "✗",
        }.get(status, "?")
        parts.append(f"{icon} {trait}")
    return "; ".join(parts)


def shortlist_row(rank: int, evaluation: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    profile = candidate.get("hydrated_profile") or {}
    structured = candidate.get("structured") or {}
    return {
        "Rank": rank,
        "Name": profile.get("name") or profile.get("full_name") or candidate.get("name") or "",
        "LinkedIn URL": (
            profile.get("linkedin_url")
            or profile.get("public_profile_url")
            or structured.get("linkedin_url")
            or structured.get("public_profile_url")
            or candidate.get("linkedin_url")
            or ""
        ),
        "Current Role": profile.get("current_title") or structured.get("position_title") or candidate.get("current_role") or candidate.get("current_title") or "",
        "Current Company": profile.get("current_company") or structured.get("company_id") or candidate.get("current_company") or "",
        "Source": candidate.get("backend") or candidate.get("source_operator") or "",
        "Channel": "|".join(candidate.get("source_lanes") or []) or candidate.get("source_channel") or "",
        "Rationale": evaluation.get("rationale", ""),
    }


def run(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir) if args.run_dir else None

    evals_path = Path(args.evaluations_json) if args.evaluations_json else (
        (run_dir / "candidate_evaluations.json") if run_dir else None
    )
    frontier_path = Path(args.frontier_json) if args.frontier_json else (
        (run_dir / "candidate-frontier.json") if run_dir else None
    )
    if args.frontier_json is None and frontier_path is not None and not frontier_path.exists():
        frontier_path = run_dir / "candidate_frontier.json"  # type: ignore[operator]
    out_dir = Path(args.out_dir) if args.out_dir else (run_dir or Path("."))

    for label, p in [("evaluations_json", evals_path), ("frontier_json", frontier_path)]:
        if not p or not p.exists():
            print(f"error: {label} not found: {p}", file=sys.stderr)
            sys.exit(1)

    evals_doc = read_json(evals_path)  # type: ignore[arg-type]
    frontier = read_json(frontier_path)  # type: ignore[arg-type]

    # Build frontier lookup
    frontier_map: dict[str, dict[str, Any]] = {}
    for candidate in frontier.get("candidates", []):
        person_id = candidate.get("person_id")
        if person_id:
            frontier_map[str(person_id)] = candidate

    # Filter evaluations. Legacy verdicts map onto the new ladder so old
    # run dirs still export: strong->top_tier, maybe/weak->high_potential.
    min_verdict = args.min_verdict
    include_verdicts = {"top_tier", "strong"}
    if min_verdict in ("high_potential", "out"):
        include_verdicts.update({"high_potential", "maybe"})
    if min_verdict == "out":
        include_verdicts.update({"weak", "out"})

    evaluations = evals_doc.get("evaluations", [])
    filtered = [e for e in evaluations if e.get("verdict") in include_verdicts]

    if not filtered:
        print("warn: no candidates match the verdict filter; writing an empty shortlist", file=sys.stderr)

    # Sort by rank
    filtered.sort(key=lambda e: e.get("rank", 9999))

    # Write shortlist CSV
    shortlist_path = out_dir / "shortlist.csv"
    shortlist_path.parent.mkdir(parents=True, exist_ok=True)

    with shortlist_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SHORTLIST_FIELDS)
        writer.writeheader()
        for rank, ev in enumerate(filtered, start=1):
            cid = ev.get("candidate_id", "")
            writer.writerow(shortlist_row(rank, ev, frontier_map.get(str(cid), {})))

    # Write shortlist manifest
    manifest = {
        "created_at": now_iso(),
        "evaluations_json": str(evals_path),
        "frontier_json": str(frontier_path),
        "min_verdict": min_verdict,
        "total_evaluated": len(evaluations),
        "shortlisted": len(filtered),
        "shortlist_csv": str(shortlist_path),
        "verdict_breakdown": {
            "top_tier": sum(1 for e in filtered if e.get("verdict") in ("top_tier", "strong")),
            "high_potential": sum(1 for e in filtered if e.get("verdict") in ("high_potential", "maybe")),
            "out": sum(1 for e in filtered if e.get("verdict") in ("weak", "out")),
        },
    }
    manifest_path = out_dir / "shortlist_manifest.json"
    write_json(manifest_path, manifest)

    print(json.dumps(manifest, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export sendable shortlist CSV from captured evaluations"
    )
    parser.add_argument("--run-dir", help="JD run directory")
    parser.add_argument("--evaluations-json", help="Path to candidate_evaluations.json")
    parser.add_argument("--frontier-json", help="Path to canonical candidate-frontier.json")
    parser.add_argument("--out-dir", help="Output directory (defaults to run-dir)")
    parser.add_argument("--min-verdict", default="high_potential",
                        choices=["top_tier", "high_potential", "out"],
                        help="Minimum verdict to include (default: high_potential)")

    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
