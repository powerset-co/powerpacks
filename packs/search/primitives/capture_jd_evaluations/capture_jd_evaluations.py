#!/usr/bin/env python3
"""Task 5a — Validate and persist candidate evaluations from the harness.

The harness (or sub-agents) scores candidates against the plan.json traits and
writes raw evaluation JSONL.  This primitive validates the evaluations against
the schema, sorts by rank, and writes the canonical artifacts:

  candidate_evaluations.json   — full evaluation document (schema-conforming)
  candidate_evaluations.jsonl  — one evaluation per line
  candidates.reranked.csv      — flat CSV ordered by rank

Usage:

    uv run --project . python packs/search/primitives/capture_jd_evaluations/capture_jd_evaluations.py \
        --run-dir .powerpacks/search-network-jd/<slug>/ \
        --raw-evaluations candidate_evaluations.raw.jsonl \
        --evaluator-mode harness_subagents

Or with explicit paths:

    ... --frontier-json candidate_frontier.json \
        --plan-json plan.json \
        --raw-evaluations candidate_evaluations.raw.jsonl \
        --evaluator-mode harness_single_agent \
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


VERDICTS = {"strong", "maybe", "weak", "out"}
SENIORITY_FIT = {"ideal", "acceptable", "too_senior", "too_junior", "wrong_track", "unknown"}
REQ_STATUS = {"strong", "partial", "weak", "missing", "unknown"}
EVALUATOR_MODES = {"harness_subagents", "harness_single_agent", "primitive"}

RERANKED_CSV_FIELDS = [
    "rank",
    "candidate_id",
    "person_id",
    "name",
    "linkedin_url",
    "current_role",
    "current_company",
    "location",
    "jd_score",
    "verdict",
    "seniority_fit",
    "matched_probe_count",
    "rationale",
    "caveats",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(obj, dict):
                raise RuntimeError(f"{path}:{line_number}: expected object")
            rows.append(obj)
    return rows


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_requirement(req: dict[str, Any], label: str, idx: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(req, dict):
        return [f"{label}[{idx}]: not an object"]
    if not req.get("trait"):
        errors.append(f"{label}[{idx}]: missing trait")
    if req.get("status") not in REQ_STATUS:
        errors.append(f"{label}[{idx}]: invalid status '{req.get('status')}'")
    if "evidence" not in req:
        errors.append(f"{label}[{idx}]: missing evidence")
    return errors


def validate_evaluation(ev: dict[str, Any], idx: int) -> list[str]:
    errors: list[str] = []
    prefix = f"evaluation[{idx}]"

    if not ev.get("candidate_id"):
        errors.append(f"{prefix}: missing candidate_id")
    if not isinstance(ev.get("rank"), int) or ev["rank"] < 1:
        errors.append(f"{prefix}: invalid rank")
    score = ev.get("jd_score")
    if not isinstance(score, (int, float)) or score < 0 or score > 1:
        errors.append(f"{prefix}: jd_score must be 0-1, got {score}")
    if ev.get("verdict") not in VERDICTS:
        errors.append(f"{prefix}: invalid verdict '{ev.get('verdict')}'")
    if ev.get("seniority_fit") not in SENIORITY_FIT:
        errors.append(f"{prefix}: invalid seniority_fit '{ev.get('seniority_fit')}'")
    if ev.get("verdict") in {"strong", "maybe"} and ev.get("seniority_fit") in {"too_senior", "too_junior", "wrong_track"}:
        errors.append(
            f"{prefix}: verdict '{ev.get('verdict')}' cannot be used with "
            f"seniority_fit '{ev.get('seniority_fit')}'"
        )

    for i, req in enumerate(ev.get("must_have") or []):
        errors.extend(validate_requirement(req, f"{prefix}.must_have", i))
    for i, req in enumerate(ev.get("nice_to_have") or []):
        errors.extend(validate_requirement(req, f"{prefix}.nice_to_have", i))

    ds = ev.get("duplicate_signal")
    if not isinstance(ds, dict):
        errors.append(f"{prefix}: missing duplicate_signal object")
    else:
        if not isinstance(ds.get("matched_probe_count"), int):
            errors.append(f"{prefix}: duplicate_signal.matched_probe_count must be int")
        if not isinstance(ds.get("matched_probe_ids"), list):
            errors.append(f"{prefix}: duplicate_signal.matched_probe_ids must be array")
        if not ds.get("interpretation"):
            errors.append(f"{prefix}: duplicate_signal.interpretation required")

    if not ev.get("rationale"):
        errors.append(f"{prefix}: missing rationale")
    if not isinstance(ev.get("caveats"), list):
        errors.append(f"{prefix}: caveats must be array")

    return errors


# ---------------------------------------------------------------------------
# Enrich evaluations with frontier data
# ---------------------------------------------------------------------------

def enrich_evaluations(
    evaluations: list[dict[str, Any]],
    frontier: dict[str, Any],
) -> list[dict[str, Any]]:
    """Add person_id, name, linkedin_url etc. from frontier to evaluations."""
    frontier_map: dict[str, dict[str, Any]] = {}
    for c in frontier.get("candidates", []):
        frontier_map[c["candidate_id"]] = c

    enriched = []
    for ev in evaluations:
        cid = ev["candidate_id"]
        cand = frontier_map.get(cid, {})
        # Ensure person_id is present if frontier has it
        if "person_id" not in ev or ev["person_id"] is None:
            ev["person_id"] = cand.get("person_id")
        # Carry display fields for CSV export (not part of schema, stripped
        # before writing evaluations JSON)
        ev["_name"] = cand.get("name")
        ev["_linkedin_url"] = cand.get("linkedin_url")
        ev["_current_role"] = cand.get("current_role")
        ev["_current_company"] = cand.get("current_company")
        ev["_location"] = cand.get("location")
        ev["_matched_probe_count"] = cand.get("duplicate_signal", {}).get("matched_probe_count", 1)
        enriched.append(ev)
    return enriched


def strip_internal_fields(ev: dict[str, Any]) -> dict[str, Any]:
    """Return a copy without underscore-prefixed display fields."""
    return {k: v for k, v in ev.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_reranked_csv(path: Path, evaluations: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RERANKED_CSV_FIELDS)
        writer.writeheader()
        for ev in evaluations:
            writer.writerow({
                "rank": ev.get("rank", ""),
                "candidate_id": ev.get("candidate_id", ""),
                "person_id": ev.get("person_id") or "",
                "name": ev.get("_name") or "",
                "linkedin_url": ev.get("_linkedin_url") or "",
                "current_role": ev.get("_current_role") or "",
                "current_company": ev.get("_current_company") or "",
                "location": ev.get("_location") or "",
                "jd_score": ev.get("jd_score", ""),
                "verdict": ev.get("verdict", ""),
                "seniority_fit": ev.get("seniority_fit", ""),
                "matched_probe_count": ev.get("_matched_probe_count", ""),
                "rationale": ev.get("rationale", ""),
                "caveats": "; ".join(ev.get("caveats") or []),
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir) if args.run_dir else None

    # Resolve paths
    raw_path = Path(args.raw_evaluations) if args.raw_evaluations else (
        (run_dir / "candidate_evaluations.raw.jsonl") if run_dir else None
    )
    frontier_path = Path(args.frontier_json) if args.frontier_json else (
        (run_dir / "candidate_frontier.json") if run_dir else None
    )
    plan_path = Path(args.plan_json) if args.plan_json else (
        (run_dir / "plan.json") if run_dir else None
    )
    out_dir = Path(args.out_dir) if args.out_dir else (run_dir or Path("."))

    for label, p in [("raw_evaluations", raw_path), ("frontier_json", frontier_path), ("plan_json", plan_path)]:
        if not p or not p.exists():
            print(f"error: {label} not found: {p}", file=sys.stderr)
            sys.exit(1)

    # Load inputs
    raw_evals = read_jsonl(raw_path)  # type: ignore[arg-type]
    frontier = read_json(frontier_path)  # type: ignore[arg-type]

    if not raw_evals:
        print("error: no evaluations in raw file", file=sys.stderr)
        sys.exit(1)

    # Validate
    all_errors: list[str] = []
    for i, ev in enumerate(raw_evals):
        all_errors.extend(validate_evaluation(ev, i))

    if all_errors and not args.force:
        print(f"error: {len(all_errors)} validation errors:", file=sys.stderr)
        for err in all_errors[:20]:
            print(f"  {err}", file=sys.stderr)
        if len(all_errors) > 20:
            print(f"  ... and {len(all_errors) - 20} more", file=sys.stderr)
        sys.exit(1)
    elif all_errors:
        print(f"warn: {len(all_errors)} validation errors (--force, continuing)", file=sys.stderr)

    # Enrich with frontier display data
    evaluations = enrich_evaluations(raw_evals, frontier)

    # Sort by rank
    evaluations.sort(key=lambda e: e.get("rank", 9999))

    # Build evaluator metadata
    evaluator: dict[str, Any] = {"mode": args.evaluator_mode}
    if args.evaluator_model:
        evaluator["model"] = args.evaluator_model
    if args.evaluator_reasoning:
        evaluator["reasoning_effort"] = args.evaluator_reasoning

    # Build document (schema-conforming)
    clean_evals = [strip_internal_fields(ev) for ev in evaluations]
    doc = {
        "plan_json": str(plan_path),
        "candidate_frontier_json": str(frontier_path),
        "created_at": now_iso(),
        "evaluator": evaluator,
        "candidate_count": len(clean_evals),
        "evaluations": clean_evals,
    }

    # Write outputs
    evals_json_path = out_dir / "candidate_evaluations.json"
    evals_jsonl_path = out_dir / "candidate_evaluations.jsonl"
    reranked_csv_path = out_dir / "candidates.reranked.csv"
    debug_json_path = out_dir / "candidates.reranked.debug.json"

    write_json(evals_json_path, doc)
    write_jsonl(evals_jsonl_path, clean_evals)
    write_reranked_csv(reranked_csv_path, evaluations)

    # Debug JSON: evaluations with enriched display fields for inspection
    write_json(debug_json_path, {
        "created_at": now_iso(),
        "candidate_count": len(evaluations),
        "evaluations": evaluations,
    })

    summary = {
        "created_at": now_iso(),
        "candidate_count": len(evaluations),
        "strong": sum(1 for e in evaluations if e.get("verdict") == "strong"),
        "maybe": sum(1 for e in evaluations if e.get("verdict") == "maybe"),
        "weak": sum(1 for e in evaluations if e.get("verdict") == "weak"),
        "out": sum(1 for e in evaluations if e.get("verdict") == "out"),
        "validation_errors": len(all_errors),
        "outputs": {
            "candidate_evaluations_json": str(evals_json_path),
            "candidate_evaluations_jsonl": str(evals_jsonl_path),
            "candidates_reranked_csv": str(reranked_csv_path),
            "candidates_reranked_debug_json": str(debug_json_path),
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and persist candidate evaluations"
    )
    parser.add_argument("--run-dir", help="JD run directory")
    parser.add_argument("--raw-evaluations", help="Path to candidate_evaluations.raw.jsonl")
    parser.add_argument("--frontier-json", help="Path to candidate_frontier.json")
    parser.add_argument("--plan-json", help="Path to plan.json")
    parser.add_argument("--out-dir", help="Output directory (defaults to run-dir)")
    parser.add_argument("--evaluator-mode", required=True, choices=sorted(EVALUATOR_MODES))
    parser.add_argument("--evaluator-model", help="Model used for evaluation")
    parser.add_argument("--evaluator-reasoning", help="Reasoning effort level")
    parser.add_argument("--force", action="store_true", help="Continue despite validation errors")

    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
