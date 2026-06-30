"""The `$recruit` convergence loop: source -> judge -> expand-from-anchor -> ... until converged.

expand-from-anchor is NOT a cleanup afterthought — it is the core Phase-2 hill-climb. The JD is a
lossy proxy for "what good looks like"; once the judge confirms strong candidates, THEIR profiles
are the highest-signal query for "find more like this", reaching the adjacent region the JD wording
never names (the barely-reachable stragglers that more JD-decompose rounds can't surface).

  epoch 0  (Phase 1, seed from the JD):
    robust_source(JD) -> build_eval_inputs -> judge -> consensus  => strong set S0
  epoch k>=1 (Phase 2, expand from our OWN judged-strong):
    pick DIVERSE anchors from S(k-1) (dedup by company so we don't echo-chamber one archetype)
    expand_from_anchor -> run_shotgun -> build_eval_inputs(--plan reuse) -> judge ONLY new pids
    consensus over everything judged so far => S(k)
  stop when a Phase-2 epoch adds NO new strong (converged) or --max-epochs hit (default 3).

Self-limiting give-up: if the judge returns ~0 strong there are no anchors, so Phase 2 no-ops and
the loop ends with an (almost) empty shortlist — correct behavior when the set has nobody.

Judging is INCREMENTAL (only candidates not yet judged) so the free `codex_judge` stays tractable
across epochs. Everything chains the existing recruit primitives as subprocesses. See SKILL.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
P = ROOT / "packs/search/primitives/recruit"
ROBUST = P / "robust_source.py"
BUILD = P / "build_eval_inputs.py"
EXPAND = P / "expand_from_anchor.py"
SHOTGUN = P / "run_shotgun.py"
CODEX_JUDGE = P / "codex_judge.py"
GPT_JUDGE = ROOT / "packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py"
CONSENSUS = P / "judge_consensus.py"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def diverse_anchors(strong: list[dict[str, Any]], union: dict[str, dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Top strong picks, one per current_company (spread archetypes, avoid echo-chamber), enriched
    with the union profile (positions/skills) so expand_from_anchor builds rich seeds."""
    ranked = sorted(strong, key=lambda r: -float(r.get("mean_score") or 0))
    out, seen_co = [], set()
    for r in ranked:
        co = (r.get("current_company") or "").strip().lower()
        if co and co in seen_co:
            continue
        seen_co.add(co)
        out.append({**union.get(r["person_id"], {}), **r})  # union profile + consensus fields
        if len(out) >= k:
            break
    return out


def run(cmd: list[str]) -> None:
    subprocess.run([str(c) for c in cmd], capture_output=True)


def judge(edir: Path, judge_kind: str, effort: str, concurrency: int) -> None:
    if judge_kind == "gpt":
        # gpt-5.4 rerank on the FLEX tier (~50% cheaper batch tier); flex is slower + can 429, so
        # give it a generous timeout (the judge retries transient errors internally).
        run([sys.executable, GPT_JUDGE, "--run-dir", edir, "--concurrency", concurrency,
             "--reasoning-effort", effort, "--service-tier", "flex", "--timeout", 600])
    else:
        run([sys.executable, CODEX_JUDGE, "--run-dir", edir, "--concurrency", concurrency, "--reasoning-effort", effort])


def main() -> None:
    ap = argparse.ArgumentParser(description="The $recruit convergence loop (source -> judge -> expand) until converged.")
    ap.add_argument("--jd-file", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--created-at", required=True, help="ISO timestamp for the plan")
    ap.add_argument("--max-epochs", type=int, default=3, help="Total epochs incl. epoch 0 (converge-capped)")
    ap.add_argument("--score-threshold", type=float, default=0.40, help="Shortlist cutoff on the canonical score")
    ap.add_argument("--judge", choices=["codex", "gpt"], default="codex", help="codex = free; gpt = paid gpt-5.4")
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--n", type=int, default=16, help="seeds per robust_source round (epoch 0)")
    ap.add_argument("--keep", type=int, default=200)
    ap.add_argument("--anchors", type=int, default=6, help="diverse anchors expanded per Phase-2 epoch")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    judges_dir = run_dir / "judges"
    judges_dir.mkdir(parents=True, exist_ok=True)
    master_judge = judges_dir / "loop.jsonl"      # accumulated verdicts (one growing judge file)
    master_union_path = run_dir / "master_union.jsonl"

    master_union: dict[str, dict[str, Any]] = {}
    judged_pids: set[str] = set()
    strong_pids: set[str] = set()
    plan_path = run_dir / "epoch0" / "plan.json"
    history: list[dict[str, Any]] = []

    for epoch in range(args.max_epochs):
        edir = run_dir / f"epoch{epoch}"
        edir.mkdir(parents=True, exist_ok=True)

        if epoch == 0:
            run([sys.executable, ROBUST, "--jd-file", args.jd_file, "--run-dir", edir, "--env-file", args.env_file,
                 "--n", args.n, "--keep", args.keep, "--max-rounds", 2] + (["--set-id", args.set_id] if args.set_id else []))
            run([sys.executable, BUILD, "--run-dir", edir, "--jd-file", args.jd_file, "--created-at", args.created_at]
                + (["--set-id", args.set_id] if args.set_id else []))
        else:
            sr = run_dir / "shortlist" / "ground_truth_ranked.json"
            strong = json.loads(sr.read_text()) if sr.exists() else []
            anchors = diverse_anchors(strong, master_union, args.anchors)
            if not anchors:
                history.append({"epoch": epoch, "stopped": "no_anchors_giveup"})
                break
            (edir / "anchors.json").write_text(json.dumps(anchors, indent=2))
            run([sys.executable, EXPAND, "--anchors", edir / "anchors.json", "--top-k", len(anchors), "--out", edir / "anchor_seeds.json"])
            run([sys.executable, SHOTGUN, "--seeds", edir / "anchor_seeds.json", "--run-dir", edir, "--env-file", args.env_file,
                 "--limit", args.keep] + (["--set-id", args.set_id] if args.set_id else []))
            run([sys.executable, BUILD, "--run-dir", edir, "--plan", plan_path] + (["--set-id", args.set_id] if args.set_id else []))

        # accumulate union; judge ONLY new pids
        for r in _jsonl(edir / "union.jsonl"):
            master_union.setdefault(r["person_id"], r)
        frontier = _jsonl(edir / "candidate_frontier.jsonl")
        new = [c for c in frontier if (c.get("person_id") or c.get("candidate_id")) not in judged_pids]
        (edir / "candidate_frontier.jsonl").write_text("".join(json.dumps(c) + "\n" for c in new))
        new_judged = 0
        if new:
            judge(edir, args.judge, args.reasoning_effort, args.concurrency)
            verds = _jsonl(edir / "candidate_evaluations.raw.jsonl")
            with master_judge.open("a") as fh:
                for v in verds:
                    fh.write(json.dumps(v) + "\n")
            judged_pids |= {c.get("person_id") or c.get("candidate_id") for c in new}
            new_judged = len(verds)
        master_union_path.write_text("".join(json.dumps(r) + "\n" for r in master_union.values()))

        # consensus over everything judged so far
        run([sys.executable, CONSENSUS, "--judges-dir", judges_dir, "--union", master_union_path,
             "--out-dir", run_dir / "shortlist", "--min-inband-votes", 1, "--score-threshold", args.score_threshold,
             "--plan", plan_path])  # core-gate the shortlist on the plan's core domain must-haves
        strong_now = json.loads((run_dir / "shortlist" / "ground_truth_ranked.json").read_text())
        now_pids = {r["person_id"] for r in strong_now}
        new_strong = now_pids - strong_pids
        history.append({"epoch": epoch, "phase": "jd" if epoch == 0 else "anchor",
                        "new_judged": new_judged, "judged_total": len(judged_pids),
                        "strong_total": len(now_pids), "new_strong": len(new_strong)})
        print(json.dumps(history[-1]))
        strong_pids = now_pids
        if epoch > 0 and len(new_strong) == 0:
            history[-1]["stopped"] = "converged"
            break

    (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
    print(json.dumps({"primitive": "recruit_loop", "status": "completed", "epochs": len(history),
                      "strong_total": len(strong_pids), "shortlist": str(run_dir / "shortlist" / "ground_truth_ranked.json"),
                      "history": history}, indent=2))


if __name__ == "__main__":
    main()
