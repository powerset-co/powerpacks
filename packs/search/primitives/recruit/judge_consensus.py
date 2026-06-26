"""Combine independent judge verdicts into a consensus stack-rank.

Input: a directory of judge JSONL files (one per judge). Each line is one verdict:
  {"person_id","name","seniority_fit","in_band","verdict","score","rationale"}
  verdict in {top_tier, high_potential, out}; seniority_fit in
  {in_band, too_senior, too_junior, wrong_track, ideal}.

Output: consensus.json (all candidates, with per-judge detail + consensus fields) and
ground_truth_ranked.json (the consensus-strong subset, stack-ranked).

Consensus-strong (the default ground-truth gate): a majority of judges mark the candidate
in-band AND a majority give a non-`out` verdict. Ranked by mean judge score, then breadth of
agreement, then number of probe families that surfaced them.

No network, no spend — pure aggregation. See packs/search/docs/agentic-search.md.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

TIER = {"top_tier": 2, "high_potential": 1, "out": 0}
GATED_FITS = {"too_senior", "too_junior", "wrong_track"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_meta(union_path: Path | None) -> dict[str, dict[str, Any]]:
    if not union_path or not union_path.exists():
        return {}
    return {r["person_id"]: r for r in read_jsonl(union_path)}


def build_consensus(
    judges: dict[str, list[dict[str, Any]]],
    meta: dict[str, dict[str, Any]],
    *,
    min_inband_votes: int,
    min_notout_votes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    judge_names = sorted(judges)
    by_pid: dict[str, dict[str, dict[str, Any]]] = {}
    for jname, rows in judges.items():
        for r in rows:
            pid = r.get("person_id")
            if pid:
                by_pid.setdefault(pid, {})[jname] = r

    rows: list[dict[str, Any]] = []
    for pid, verds in by_pid.items():
        present = [j for j in judge_names if j in verds]
        scores = [float(verds[j].get("score") or 0.0) for j in present]
        tiers = [TIER.get(verds[j].get("verdict"), 0) for j in present]
        inband_votes = sum(1 for j in present if verds[j].get("in_band"))
        notout_votes = sum(1 for j in present if verds[j].get("verdict") != "out")
        gated_votes = sum(1 for j in present if verds[j].get("seniority_fit") in GATED_FITS)
        m = meta.get(pid, {})
        rows.append({
            "person_id": pid,
            "name": m.get("name") or (verds[present[0]].get("name") if present else None),
            "current_title": m.get("current_title"),
            "current_company": m.get("current_company"),
            "linkedin_url": m.get("linkedin_url"),
            "location": m.get("location"),
            "found_by": m.get("found_by", []),
            "n_judges": len(present),
            "mean_score": round(statistics.mean(scores), 4) if scores else 0.0,
            "min_score": round(min(scores), 4) if scores else 0.0,
            "mean_tier": round(statistics.mean(tiers), 4) if tiers else 0.0,
            "inband_votes": inband_votes,
            "notout_votes": notout_votes,
            "gated_votes": gated_votes,
            "per_judge": {
                j: {
                    "verdict": verds[j].get("verdict"),
                    "seniority_fit": verds[j].get("seniority_fit"),
                    "score": verds[j].get("score"),
                }
                for j in present
            },
        })

    strong = [r for r in rows if r["inband_votes"] >= min_inband_votes and r["notout_votes"] >= min_notout_votes]
    strong.sort(key=lambda r: (-r["mean_score"], -r["notout_votes"], -r["inband_votes"], -len(r["found_by"])))
    rows.sort(key=lambda r: -r["mean_score"])
    return rows, strong


def main() -> None:
    ap = argparse.ArgumentParser(description="Combine judge verdicts into consensus stack-rank.")
    ap.add_argument("--judges-dir", required=True, help="Directory of <judge>.jsonl verdict files")
    ap.add_argument("--union", help="candidates_union.jsonl for candidate metadata (optional)")
    ap.add_argument("--out-dir", required=True, help="Where to write consensus.json + ground_truth_ranked.json")
    ap.add_argument("--min-inband-votes", type=int, default=2)
    ap.add_argument("--min-notout-votes", type=int, default=2)
    args = ap.parse_args()

    jdir = Path(args.judges_dir)
    judges = {p.stem: read_jsonl(p) for p in sorted(jdir.glob("*.jsonl"))}
    if not judges:
        raise SystemExit(f"no judge .jsonl files found in {jdir}")
    meta = load_meta(Path(args.union) if args.union else None)

    rows, strong = build_consensus(
        judges, meta,
        min_inband_votes=args.min_inband_votes,
        min_notout_votes=args.min_notout_votes,
    )

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "consensus.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    (out / "ground_truth_ranked.json").write_text(json.dumps(strong, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "primitive": "judge_consensus",
        "status": "completed",
        "judges": sorted(judges),
        "candidates": len(rows),
        "consensus_strong": len(strong),
        "top10_unanimous_inband": all(
            r["inband_votes"] == r["n_judges"] and r["notout_votes"] == r["n_judges"]
            for r in strong[:10]
        ) if strong else False,
        "out_dir": str(out),
    }, indent=2))


if __name__ == "__main__":
    main()
