"""Ground-truth survival funnel + per-probe attribution for one typed search run.

Turns a run's single recall number into per-stage attribution: for every ground-truth
person, which stage lost them (never sourced, triage drop, judge verdict, core gate,
...) and which probes found them. Pure set math over existing run artifacts — no
network, no spend.

Flow:
  load GT -> load canonical typed stage frontiers + final candidate frontier ->
  classify each GT member into one disposition -> aggregate the funnel and
  per-probe GT yield -> write <run-dir>/reflect/funnel.json.

Canonical artifacts are required and fail closed. The scorer does not reconstruct
deleted epoch, judge-panel, consensus, or shortlist directory layouts.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from score_ground_truth_gaps import validate_reflect_ground_truth  # noqa: E402
from packs.search.pipeline.frontier import CANDIDATE_FRONTIER_NAME, CandidateFrontier  # noqa: E402
from packs.search.pipeline.stage_membership import (  # noqa: E402
    STAGE_MEMBERSHIP_NAME,
    SearchStageMembership,
    gate_disposition,
)

DISPOSITIONS = (
    "shortlisted",
    "gate_passed_not_shortlisted",
    "core_gated",
    "location_gated",
    "founder_c_suite_gated",
    "seniority_gated",
    "judge_out",
    "below_floor",
    "never_judged",
    "triage_dropped",
    "hydration_missing",
    "hard_filter_quarantined",
    "never_sourced",
)


def load_ground_truth(path: Path) -> list[dict[str, Any]]:
    """Return finalized eligible Reflect labels in review order."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    document = validate_reflect_ground_truth(path, raw)
    members: list[dict[str, Any]] = []
    rank = 0
    for label in document["labels"]:
        if label["decision"] not in {"eligible_strong", "eligible_bench"}:
            continue
        rank += 1
        members.append({
            "person_id": label["person_id"],
            "name": None,
            "gt_rank": rank,
            "tier": "A" if label["decision"] == "eligible_strong" else "B",
        })
    return members


def classify(
    pid: str,
    *,
    memberships: dict[str, Any],
) -> tuple[str, str]:
    """Return (disposition, detail) for one GT person."""
    member = memberships.get(pid)
    if member is None:
        return "never_sourced", "no probe returned this person"
    return member.disposition, member.detail


def probe_attribution(sourced_map: dict[str, list[str]], gt_ids: set[str]) -> list[dict[str, Any]]:
    """Per-probe totals, GT yield, and GT-exclusive finds from master-union found_by tags."""
    stats: dict[str, dict[str, Any]] = {}
    for pid, probes in sourced_map.items():
        for probe in probes:
            row = stats.setdefault(probe, {"probe": probe, "sourced": 0, "gt_sourced": 0, "gt_exclusive": []})
            row["sourced"] += 1
            if pid in gt_ids:
                row["gt_sourced"] += 1
                if len(probes) == 1:
                    row["gt_exclusive"].append(pid)
    out = sorted(stats.values(), key=lambda r: (-r["gt_sourced"], -r["sourced"], r["probe"]))
    for row in out:
        row["gt_exclusive_count"] = len(row.pop("gt_exclusive"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="GT survival funnel + per-probe attribution for a typed search run.")
    ap.add_argument("--stage-membership", required=True)
    ap.add_argument("--candidate-frontier", required=True)
    ap.add_argument("--ground-truth", required=True, help="Finalized reflect.ground_truth.v1")
    ap.add_argument("--out", default=None, help="Default: <run-dir>/reflect/funnel.json")
    args = ap.parse_args()

    membership_path = Path(args.stage_membership)
    frontier_path = Path(args.candidate_frontier)
    membership = SearchStageMembership.read(membership_path)
    candidate_frontier = CandidateFrontier.read(frontier_path)
    if candidate_frontier.truncated:
        raise ValueError("strict scoring rejects a truncated candidate frontier")
    memberships = {row.person_id: row for row in membership.candidates}
    ranked_ids = tuple(row.person_id for row in candidate_frontier.candidates)
    triage_survivors = {row.person_id for row in membership.candidates if row.triage_survived}
    if set(ranked_ids) != triage_survivors:
        raise ValueError("candidate frontier IDs must exactly match triage-survived stage memberships")
    frontier_shortlist = {
        row.person_id
        for index, row in enumerate(candidate_frontier.candidates)
        if index < membership.frontier_limit and row.deterministic_gates.get("shortlist")
    }
    membership_shortlist = {row.person_id for row in membership.candidates if row.shortlisted}
    expected_dispositions = {}
    for index, candidate in enumerate(candidate_frontier.candidates):
        member = memberships[candidate.person_id]
        hydrated = candidate.hydration_disposition == "hydrated"
        hard_filter_passed = candidate.hard_filter_evidence.get("disposition") == "accepted"
        judge_status = str((candidate.judge or {}).get("status") or "not_run")
        shortlisted = index < membership.frontier_limit and bool(candidate.deterministic_gates.get("shortlist"))
        gate_result, _detail = gate_disposition(
            candidate,
            score_floor=membership.score_floor,
            sendable_score=membership.sendable_score,
        )
        expected_disposition = "shortlisted" if shortlisted else gate_result
        expected_dispositions[candidate.person_id] = expected_disposition
        if (
            not member.hydrated
            or not hydrated
            or not member.hard_filter_passed
            or not hard_filter_passed
            or not member.triage_survived
            or member.judge_status != judge_status
        ):
            raise ValueError(f"ranked candidate does not match canonical stage membership: {candidate.person_id}")
    if membership_shortlist != frontier_shortlist:
        raise ValueError("stage membership shortlist must equal the eligible ranked prefix")
    for person_id, expected_disposition in expected_dispositions.items():
        if memberships[person_id].disposition != expected_disposition:
            raise ValueError(f"ranked candidate does not match canonical stage membership: {person_id}")

    sourced_map = {
        row.person_id: list(dict.fromkeys(
            match.probe_id or match.probe_family or match.lane for match in row.found_by
        ))
        for row in membership.candidates
    }
    names = {row.person_id: row.name for row in membership.candidates if row.name}
    sourced = set(memberships)
    hydrated = {row.person_id for row in membership.candidates if row.hydrated}
    hard_filtered = {row.person_id for row in membership.candidates if row.hard_filter_passed}
    triaged = triage_survivors
    judged = {row.person_id for row in membership.candidates if row.judge_status == "judged"}
    shortlist_ids = membership_shortlist

    gt_members = load_ground_truth(Path(args.ground_truth))
    gt_ids = {m["person_id"] for m in gt_members}

    for member in gt_members:
        disposition, detail = classify(
            member["person_id"],
            memberships=memberships,
        )
        member["disposition"] = disposition
        member["detail"] = detail
        if member["name"] is None:
            member["name"] = names.get(member["person_id"])
    all_members = gt_members
    histogram = {d: sum(1 for m in all_members if m["disposition"] == d) for d in DISPOSITIONS}
    histogram = {k: v for k, v in histogram.items() if v}

    def survived(stage: set[str]) -> int:
        return len(gt_ids & stage)

    funnel_stages = [
        {"stage": "ground_truth", "total": len(all_members), "gt_survived": len(all_members)},
        {"stage": "sourced", "total": len(sourced), "gt_survived": survived(sourced)},
        {"stage": "hydrated", "total": len(hydrated), "gt_survived": survived(hydrated)},
        {"stage": "hard_filter_survived", "total": len(hard_filtered), "gt_survived": survived(hard_filtered)},
        {"stage": "triage_survived", "total": len(triaged), "gt_survived": survived(triaged)},
        {"stage": "judged", "total": len(judged), "gt_survived": survived(judged)},
        {"stage": "shortlisted", "total": len(shortlist_ids), "gt_survived": survived(shortlist_ids)},
    ]
    funnel_line = " → ".join(
        f"{funnel_stages[0]['gt_survived']} GT" if s["stage"] == "ground_truth" else f"{s['gt_survived']} {s['stage']}"
        for s in funnel_stages
    )

    payload = {
        "primitive": "score_funnel",
        "stage_membership": str(membership_path),
        "candidate_frontier": str(frontier_path),
        "ground_truth": str(args.ground_truth),
        "gt_size": len(all_members),
        "thresholds": {"score_floor": membership.score_floor, "sendable_score": membership.sendable_score},
        "shortlist_source": CANDIDATE_FRONTIER_NAME,
        "convergence": {"status": membership.status, "epochs": membership.epochs},
        "funnel": funnel_stages,
        "funnel_line": funnel_line,
        "dispositions": histogram,
        "gt_members": all_members,
        "probe_attribution": probe_attribution(sourced_map, gt_ids),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    out_path = Path(args.out) if args.out else membership_path.parent / "reflect" / "funnel.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"funnel: {funnel_line} | {json.dumps(histogram)}", file=sys.stderr)
    summary = {k: payload[k] for k in ("primitive", "gt_size", "funnel_line", "dispositions", "shortlist_source")}
    summary["funnel_json"] = str(out_path)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
