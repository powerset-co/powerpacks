"""Ground-truth survival funnel + per-probe attribution for one deep-search run dir.

Turns a run's single recall number into per-stage attribution: for every ground-truth
person, which stage lost them (never sourced, triage drop, judge verdict, core gate,
...) and which probes found them. Pure set math over existing run artifacts — no
network, no spend.

Flow:
  load GT (flat ranked list, or a tiered dict whose names resolve against run
  artifacts) -> load stage sets (master_union -> candidate_frontier.full ->
  candidate_frontier.to_judge -> judges/loop.jsonl -> shortlist/consensus.json ->
  ranked_final/shortlist_ranked) -> classify each GT member into one disposition ->
  aggregate the funnel + per-probe GT yield -> write <run-dir>/shortlist/funnel.json
  and print the JSON summary.

Stage sets are closed downstream-into-upstream (a judged person is by definition
sourced) so a missing intermediate artifact cannot misclassify a survivor.
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
from score_ground_truth_gaps import load_records  # noqa: E402

TIER_GAINS = {"A": 3, "B": 2, "C": 1}

DISPOSITIONS = (
    "shortlisted",
    "gate_passed_not_shortlisted",
    "core_gated",
    "seniority_gated",
    "judge_out",
    "below_floor",
    "never_judged",
    "triage_dropped",
    "lost_at_frontier",
    "never_sourced",
    "unresolved_identity",
)


def norm_name(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def load_ground_truth(path: Path, name_to_pid: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (gt_members, unresolved). Members carry person_id, name, gt_rank, tier.

    Flat list files keep their order as rank. Tiered dicts ({"tiers": {"A_...": [...]}}) carry
    names only, so ids resolve via name_to_pid built from run artifacts; REMOVED_* tiers are
    excluded from GT entirely.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    members: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for i, rec in enumerate(raw):
            members.append({
                "person_id": rec.get("person_id"),
                "name": rec.get("name"),
                "gt_rank": i + 1,
                "tier": rec.get("tier"),
            })
        return members, unresolved
    tiers = raw.get("tiers") or {}
    rank = 0
    for tier_key, entries in tiers.items():
        letter = tier_key[:1].upper()
        if letter not in TIER_GAINS:
            continue  # REMOVED_* and any non-A/B/C bucket are not GT
        for rec in entries:
            rank += 1
            pid = rec.get("person_id") or name_to_pid.get(norm_name(rec.get("name")))
            row = {"person_id": pid, "name": rec.get("name"), "gt_rank": rank, "tier": letter}
            if pid:
                members.append(row)
            else:
                unresolved.append(row)
    return members, unresolved


def jsonl_ids(paths: list[Path], key: str = "person_id", alt_key: str = "candidate_id") -> set[str]:
    out: set[str] = set()
    for p in paths:
        for rec in load_records(p):
            pid = rec.get(key) or rec.get(alt_key)
            if pid:
                out.add(pid)
    return out


def classify(
    pid: str,
    *,
    shortlist_ids: set[str],
    sourced: set[str],
    frontier: set[str],
    triaged: set[str],
    judged: set[str],
    consensus: dict[str, dict[str, Any]],
    score_threshold: float,
    min_inband: int,
    min_notout: int,
) -> tuple[str, str]:
    """Return (disposition, detail) for one GT person."""
    if pid in shortlist_ids:
        return "shortlisted", ""
    if pid not in sourced:
        return "never_sourced", "no probe returned this person"
    if pid not in frontier:
        return "lost_at_frontier", "sourced but absent from every candidate frontier"
    if pid not in triaged:
        return "triage_dropped", "in frontier but never in a to_judge file"
    rec = consensus.get(pid)
    if pid not in judged or rec is None:
        return "never_judged", "passed triage but has no judge/consensus record"
    inband = int(rec.get("inband_votes") or 0)
    notout = int(rec.get("notout_votes") or 0)
    gated = int(rec.get("gated_votes") or 0)
    score = float(rec.get("mean_score") or 0.0)
    core_met = bool(rec.get("core_met"))
    detail = f"mean_score={score:.2f} inband={inband} notout={notout} gated={gated} core_met={core_met}"
    # Priority ladder (pinned; mirrors the AgentMail REPORT.md hand taxonomy): the
    # PRIMARY reason lost is seniority band, then score floor, then the core gate —
    # an `out` verdict whose core traits are unmet is attributed to the core gate.
    if gated >= 1 or inband < min_inband:
        return "seniority_gated", detail
    if score < score_threshold:
        return "below_floor", detail
    if not core_met:
        return "core_gated", detail
    if notout < min_notout:
        return "judge_out", detail
    return "gate_passed_not_shortlisted", detail


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
    ap = argparse.ArgumentParser(description="GT survival funnel + per-probe attribution for a deep-search run dir.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--ground-truth", required=True, help="Flat ranked list, or a tiered {'tiers': ...} dict")
    ap.add_argument("--score-threshold", type=float, default=0.40)
    ap.add_argument("--min-inband-votes", type=int, default=1)
    ap.add_argument("--min-notout-votes", type=int, default=1)
    ap.add_argument("--out", default=None, help="Default: <run-dir>/shortlist/funnel.json")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(json.dumps({"status": "failed", "error": f"run dir not found: {run_dir}"}))
        raise SystemExit(2)

    master = run_dir / "master_union.jsonl"
    union_paths = [master] if master.exists() else sorted(run_dir.glob("epoch*/union.jsonl"))
    sourced_map: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for p in union_paths:
        for rec in load_records(p):
            pid = rec.get("person_id")
            if not pid:
                continue
            probes = rec.get("found_by") or []
            merged = sourced_map.setdefault(pid, [])
            for probe in probes:
                if probe not in merged:
                    merged.append(probe)
            if rec.get("name"):
                names[pid] = rec["name"]

    frontier = jsonl_ids(sorted(run_dir.glob("epoch*/candidate_frontier.full.jsonl")))
    triaged = jsonl_ids(sorted(run_dir.glob("epoch*/candidate_frontier.to_judge.jsonl")))
    judged = jsonl_ids(sorted((run_dir / "judges").glob("*.jsonl")))

    consensus: dict[str, dict[str, Any]] = {}
    consensus_path = run_dir / "shortlist" / "consensus.json"
    if consensus_path.exists():
        for rec in load_records(consensus_path):
            pid = rec.get("person_id")
            if pid:
                consensus[pid] = rec
                if rec.get("name"):
                    names.setdefault(pid, rec["name"])

    shortlist_ids: set[str] = set()
    shortlist_source = None
    for candidate in ("ranked_final.json", "shortlist_ranked.json", "ground_truth_ranked.json"):
        path = run_dir / "shortlist" / candidate
        if path.exists():
            shortlist_ids = {r.get("person_id") for r in load_records(path) if r.get("person_id")}
            shortlist_source = candidate
            break

    # Downstream implies upstream: a judged person was sourced even if an artifact is missing.
    judged_or_consensus = judged | set(consensus)
    triaged_eff = triaged | judged_or_consensus | shortlist_ids
    frontier_eff = frontier | triaged_eff
    sourced_eff = set(sourced_map) | frontier_eff

    name_to_pid = {norm_name(n): pid for pid, n in names.items()}
    gt_members, unresolved = load_ground_truth(Path(args.ground_truth), name_to_pid)
    gt_ids = {m["person_id"] for m in gt_members}

    for member in gt_members:
        disposition, detail = classify(
            member["person_id"],
            shortlist_ids=shortlist_ids,
            sourced=sourced_eff,
            frontier=frontier_eff,
            triaged=triaged_eff,
            judged=judged_or_consensus,
            consensus=consensus,
            score_threshold=args.score_threshold,
            min_inband=args.min_inband_votes,
            min_notout=args.min_notout_votes,
        )
        member["disposition"] = disposition
        member["detail"] = detail
        member.setdefault("name", names.get(member["person_id"]))
    for member in unresolved:
        member["disposition"] = "unresolved_identity"
        member["detail"] = "tiered GT name not found in run artifacts"

    all_members = gt_members + unresolved
    histogram = {d: sum(1 for m in all_members if m["disposition"] == d) for d in DISPOSITIONS}
    histogram = {k: v for k, v in histogram.items() if v}

    def survived(stage: set[str]) -> int:
        return len(gt_ids & stage)

    funnel_stages = [
        {"stage": "ground_truth", "total": len(all_members), "gt_survived": len(all_members)},
        {"stage": "sourced", "total": len(sourced_eff), "gt_survived": survived(sourced_eff)},
        {"stage": "frontier", "total": len(frontier_eff), "gt_survived": survived(frontier_eff)},
        {"stage": "triage_survived", "total": len(triaged_eff), "gt_survived": survived(triaged_eff)},
        {"stage": "judged", "total": len(judged_or_consensus), "gt_survived": survived(judged_or_consensus)},
        {"stage": "shortlisted", "total": len(shortlist_ids), "gt_survived": survived(shortlist_ids)},
    ]
    funnel_line = " → ".join(
        f"{funnel_stages[0]['gt_survived']} GT" if s["stage"] == "ground_truth" else f"{s['gt_survived']} {s['stage']}"
        for s in funnel_stages
    )

    payload = {
        "primitive": "score_funnel",
        "run_dir": str(run_dir),
        "ground_truth": str(args.ground_truth),
        "gt_size": len(all_members),
        "thresholds": {
            "score_threshold": args.score_threshold,
            "min_inband_votes": args.min_inband_votes,
            "min_notout_votes": args.min_notout_votes,
        },
        "shortlist_source": shortlist_source,
        "funnel": funnel_stages,
        "funnel_line": funnel_line,
        "dispositions": histogram,
        "gt_members": all_members,
        "probe_attribution": probe_attribution(sourced_map, gt_ids),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    out_path = Path(args.out) if args.out else run_dir / "shortlist" / "funnel.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"funnel: {funnel_line} | {json.dumps(histogram)}", file=sys.stderr)
    summary = {k: payload[k] for k in ("primitive", "gt_size", "funnel_line", "dispositions", "shortlist_source")}
    summary["funnel_json"] = str(out_path)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
