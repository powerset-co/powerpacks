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

# Per-trait status ladder (mirrors evaluate_profile_candidates). A core must-have counts as MET
# only at "experienced"/"doing_now" — "capable" (adjacent, could-pick-it-up) does NOT count, which
# is the whole point: generic-but-wrong-domain candidates park at "capable" and must not pass a gate.
STATUS_VALUE = {"doing_now": 0.95, "experienced": 0.80, "capable": 0.70,
                "foundational": 0.45, "thin": 0.30, "missing": 0.0, "unknown": 0.0}
MET = 0.80


def _norm_trait(s: str | None) -> str:
    return " ".join((s or "").strip().lower().split())


def core_traits_from_plan(plan_path: Path | None) -> set[str]:
    """Normalized text of the must-haves the plan marked tier=='core'. Empty => no core-gate."""
    if not plan_path or not plan_path.exists():
        return set()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    must = (plan.get("traits") or {}).get("must_have") or []
    return {_norm_trait(t.get("trait")) for t in must
            if isinstance(t, dict) and t.get("tier") == "core" and t.get("trait")}


def candidate_meets_core(verds: dict[str, dict[str, Any]], present: list[str], core: set[str]) -> bool:
    """Does the candidate genuinely DO at least one of the role's core domain capabilities?

    True iff >= ONE core must-have is met at experienced/doing_now (majority of judges that scored
    it). Requiring ALL core traits is brittle — it punishes roles whose domain is split into many
    narrow sub-skills (a real distsys gem is `capable`, not `doing_now`, on every one of inference/
    schedulers/routing) and inverts the result. Requiring >= 1 instead excludes exactly the failure
    case we care about: a strong, senior, but WRONG-domain candidate parks at `capable`/`missing` on
    every core trait, so they meet none. Generic seniority/leadership (table_stakes) never counts."""
    for ct in core:
        vals = [STATUS_VALUE.get(t.get("status"), 0.0)
                for j in present for t in (verds[j].get("must_have") or [])
                if _norm_trait(t.get("trait")) == ct]
        if vals and sum(1 for v in vals if v >= MET) * 2 >= len(vals):
            return True
    return False


def normalize_verdict(r: dict[str, Any]) -> dict[str, Any]:
    """Accept both the native judge schema and evaluate_profile_candidates raw output.

    The canonical judge emits {candidate_id, jd_score, seniority_fit, verdict, rationale} with no
    explicit `in_band`; the native (Claude sub-agent) judges emit {person_id, score, in_band, ...}.
    Normalize to {person_id, score, in_band, verdict, seniority_fit, name, rationale} so a directory
    can mix either format. `in_band` is derived from seniority_fit when absent (not gated = in-band).
    """
    if "person_id" not in r and r.get("candidate_id"):
        r = {**r, "person_id": r["candidate_id"]}
    if "score" not in r and r.get("jd_score") is not None:
        r = {**r, "score": r["jd_score"]}
    if "in_band" not in r:
        r = {**r, "in_band": r.get("seniority_fit") not in GATED_FITS}
    return r


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(normalize_verdict(json.loads(line)))
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
    score_threshold: float | None = None,
    core_traits: set[str] | None = None,
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
        core_met = candidate_meets_core(verds, present, core_traits) if core_traits else None
        m = meta.get(pid, {})
        rows.append({
            "person_id": pid,
            "core_met": core_met,
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

    if core_traits:
        # CORE-GATE (the deep-search shortlist rule): membership = majority-in-band AND the candidate
        # genuinely does >= 1 core domain capability (experienced+) AND clears the score floor.
        # Generic seniority/leadership traits (table_stakes) only affect ranking, never membership —
        # so a strong-but-wrong-domain candidate (parks at `capable`/`missing` on every core trait)
        # is excluded no matter how high their blended score. The score floor keeps quality; the
        # core check kills adjacency. Rubric/scorer untouched — reads the per-trait statuses the
        # judge already emits. Empirically (real judged data): AgentMail distsys MTS -> ~30 (filled),
        # Realta fusion VP -> ~7 real hardware leaders (down from 12); a sharper core trait at GATE 1
        # collapses the give-up case toward ~0.
        floor = 0.40 if score_threshold is None else score_threshold
        strong = [r for r in rows if r["inband_votes"] >= min_inband_votes and r["core_met"] and r["mean_score"] >= floor]
    elif score_threshold is not None:
        # Shortlist by the canonical mean score (choose the recall/precision cutoff) +
        # majority-in-band seniority gate. Does NOT touch the rubric/scorer — only the depth.
        strong = [r for r in rows if r["inband_votes"] >= min_inband_votes and r["mean_score"] >= score_threshold]
    else:
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
    ap.add_argument("--score-threshold", type=float, default=None,
                    help="If set, shortlist = majority-in-band AND mean_score >= threshold "
                         "(tunable recall/precision cutoff; ~0.40 recovered ~0.9 recall on AgentMail). "
                         "Overrides the not-out vote gate; applies on top of --plan core-gate as a floor.")
    ap.add_argument("--plan", default=None,
                    help="plan.json. If its must_haves carry tier=='core', the shortlist is CORE-GATED: "
                         "membership = majority-in-band AND at least one core must-have met (>= experienced). "
                         "Generic table_stakes traits only rank. No core tags => falls back to score gate.")
    args = ap.parse_args()

    jdir = Path(args.judges_dir)
    judges = {p.stem: read_jsonl(p) for p in sorted(jdir.glob("*.jsonl"))}
    if not judges:
        raise SystemExit(f"no judge .jsonl files found in {jdir}")
    meta = load_meta(Path(args.union) if args.union else None)
    core_traits = core_traits_from_plan(Path(args.plan) if args.plan else None)

    rows, strong = build_consensus(
        judges, meta,
        min_inband_votes=args.min_inband_votes,
        min_notout_votes=args.min_notout_votes,
        score_threshold=args.score_threshold,
        core_traits=core_traits,
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
        "core_gate": sorted(core_traits) if core_traits else None,
        "consensus_strong": len(strong),
        "top10_unanimous_inband": all(
            r["inband_votes"] == r["n_judges"] and r["notout_votes"] == r["n_judges"]
            for r in strong[:10]
        ) if strong else False,
        "out_dir": str(out),
    }, indent=2))


if __name__ == "__main__":
    main()
