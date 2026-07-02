"""The `$search` deep-mode convergence loop: source -> judge -> expand-from-anchor -> ... until converged.

expand-from-anchor is NOT a cleanup afterthought — it is the core Phase-2 hill-climb. The JD is a
lossy proxy for "what good looks like"; once the judge confirms strong candidates, THEIR profiles
are the highest-signal query for "find more like this", reaching the adjacent region the JD wording
never names (the barely-reachable stragglers that more JD-decompose rounds can't surface).

  epoch 0  (Phase 1, seed from the JD):
    robust_source(JD) -> build_eval_inputs -> judge -> consensus  => strong set S0
  epoch k>=1 (Phase 2, expand from our OWN judged-strong):
    pick DIVERSE anchors from S(k-1) (dedup by company so we don't echo-chamber one archetype)
    expand_from_anchor -> run_wide_search -> build_eval_inputs(--plan reuse) -> judge ONLY new pids
    consensus over everything judged so far => S(k)
  stop when a Phase-2 epoch adds NO new strong (converged) or --max-epochs hit (default 3).

Self-limiting give-up: if the judge returns ~0 strong there are no anchors, so Phase 2 no-ops and
the loop ends with an (almost) empty shortlist — correct behavior when the set has nobody.

Judging is INCREMENTAL (only candidates not yet judged) so the free `codex_judge` stays tractable
across epochs. Everything chains the existing deep-search primitives as subprocesses. See SKILL.md.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

try:  # direct script execution
    from subprocess_utils import CommandError, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.deep_search.deep_search_loop
    from .subprocess_utils import CommandError, run_checked

ROOT = Path(__file__).resolve().parents[4]
P = ROOT / "packs/search/primitives/deep_search"
FETCH_JD = P / "fetch_jd.py"
ROBUST = P / "robust_source.py"
BUILD = P / "build_eval_inputs.py"
EXPAND = P / "expand_from_anchor.py"
WIDE_SEARCH = P / "run_wide_search.py"
CODEX_JUDGE = P / "codex_judge.py"
GPT_JUDGE = ROOT / "packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py"
CONSENSUS = P / "judge_consensus.py"

# A fetched JD below this many chars is almost certainly a JS-rendered page that yielded no real
# text; decomposing it produces a garbage plan. Mirrors fetch_jd._THIN_CHARS (fetch_jd flags "thin"
# but exits 0, so the loop guards it explicitly before spending on sourcing).
_MIN_JD_CHARS = 400


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


def run(cmd: list[object], *, expected_paths: list[Path] | None = None, description: str | None = None) -> None:
    run_checked(cmd, expected_paths=expected_paths, description=description)


def stage_judge_input(edir: Path, candidates: list[dict[str, Any]]) -> Path:
    """Create a new-only judge run dir while leaving canonical frontier files untouched."""
    jdir = edir / "judge_input"
    jdir.mkdir(parents=True, exist_ok=True)
    for name in ("plan.json", "probe_summaries.json"):
        src = edir / name
        if src.exists():
            shutil.copyfile(src, jdir / name)
    with (jdir / "candidate_frontier.jsonl").open("w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    return jdir


def judge(edir: Path, candidates: list[dict[str, Any]], judge_kind: str, effort: str, concurrency: int) -> None:
    jdir = stage_judge_input(edir, candidates)
    raw = jdir / "candidate_evaluations.raw.jsonl"
    if judge_kind == "gpt":
        # gpt-5.4 rerank on the FLEX tier (~50% cheaper batch tier); flex is slower + can 429, so
        # give it a generous timeout (the judge retries transient errors internally).
        run([sys.executable, GPT_JUDGE, "--run-dir", jdir, "--concurrency", concurrency,
             "--reasoning-effort", effort, "--service-tier", "flex", "--timeout", 600])
    else:
        run([sys.executable, CODEX_JUDGE, "--run-dir", jdir, "--concurrency", concurrency, "--reasoning-effort", effort])
    if not raw.exists():
        raise CommandError(["judge", judge_kind], missing=[raw], description=f"{judge_kind} judge")
    shutil.copyfile(raw, edir / "candidate_evaluations.raw.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description="The $search deep-mode convergence loop (source -> judge -> expand) until converged.")
    ap.add_argument("--jd-file", default=None, help="Path to JD text. Provide this OR --jd-url.")
    ap.add_argument("--jd-url", default=None, help="Job-posting URL; fetched to <run-dir>/jd.txt via fetch_jd before sourcing.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--backend", choices=("powerset", "local"), default="powerset", help="Sourcing backend threaded through robust_source/run_wide_search; local = the local DuckDB index (no set scoping, no pinned seniority bands)")
    ap.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb", help="Local DuckDB path (used only with --backend local)")
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
    ap.add_argument("--approved-plan", default=None, help="Reviewed plan.json to use without calling the plan LLM")
    ap.add_argument("--plan-approved", action="store_true", help="Resume with the existing <run-dir>/epoch0/plan.json after human review")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir

    # JD input: exactly one of --jd-file / --jd-url. A URL is fetched to <run-dir>/jd.txt first
    # (the URL intake via fetch_jd), then treated as an ordinary --jd-file from here on.
    if bool(args.jd_file) == bool(args.jd_url):
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "provide exactly one of --jd-file or --jd-url"}, indent=2))
        raise SystemExit(2)
    if args.jd_url:
        run_dir.mkdir(parents=True, exist_ok=True)
        jd_txt = run_dir / "jd.txt"
        run([sys.executable, FETCH_JD, "--url", args.jd_url, "--out", jd_txt], expected_paths=[jd_txt], description="fetch_jd URL->JD")
        jd_text = jd_txt.read_text(encoding="utf-8").strip()
        if len(jd_text) < _MIN_JD_CHARS:
            print(json.dumps({"primitive": "deep_search_loop", "status": "failed",
                              "error": "fetched JD is too thin (likely a JS-rendered page); paste the JD text and rerun with --jd-file",
                              "jd_url": args.jd_url, "jd_chars": len(jd_text)}, indent=2))
            raise SystemExit(1)
        args.jd_file = str(jd_txt)

    judges_dir = run_dir / "judges"
    judges_dir.mkdir(parents=True, exist_ok=True)
    master_judge = judges_dir / "loop.jsonl"      # accumulated verdicts (one growing judge file)
    master_union_path = run_dir / "master_union.jsonl"

    master_union: dict[str, dict[str, Any]] = {}
    judged_pids: set[str] = set()
    strong_pids: set[str] = set()
    epoch0_dir = run_dir / "epoch0"
    plan_path = Path(args.approved_plan) if args.approved_plan else epoch0_dir / "plan.json"
    if args.approved_plan and not plan_path.exists():
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "approved plan not found", "plan": str(plan_path)}, indent=2))
        raise SystemExit(1)
    if args.plan_approved and args.approved_plan:
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "use only one of --plan-approved or --approved-plan"}, indent=2))
        raise SystemExit(1)
    if args.plan_approved and not plan_path.exists():
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "--plan-approved requires existing epoch0/plan.json", "plan": str(plan_path)}, indent=2))
        raise SystemExit(1)
    # Retry/resume safety: if a previous approved run already judged some candidates, do not
    # rejudge them blindly on process restart. First gate resume has no such files, so this is a no-op.
    master_union = {r["person_id"]: r for r in _jsonl(master_union_path) if r.get("person_id")}
    for v in _jsonl(master_judge):
        pid = v.get("person_id") or v.get("candidate_id")
        if pid:
            judged_pids.add(pid)
    existing_shortlist = run_dir / "shortlist" / "ground_truth_ranked.json"
    if existing_shortlist.exists():
        try:
            strong_pids = {r["person_id"] for r in json.loads(existing_shortlist.read_text()) if r.get("person_id")}
        except (json.JSONDecodeError, OSError):
            strong_pids = set()
    history: list[dict[str, Any]] = []

    try:
        for epoch in range(args.max_epochs):
            edir = run_dir / f"epoch{epoch}"
            edir.mkdir(parents=True, exist_ok=True)

            if epoch == 0:
                required = [edir / "union.jsonl", edir / "plan.json", edir / "candidate_frontier.jsonl", edir / "candidate_frontier.json", edir / "probe_summaries.json"]
                if not args.plan_approved and not args.approved_plan and (edir / "plan.json").exists():
                    history.append({"epoch": 0, "status": "awaiting_plan_approval", "plan": str(edir / "plan.json"), "existing_plan": True})
                    (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
                    print(json.dumps({"primitive": "deep_search_loop", "status": "awaiting_plan_approval", "plan": str(edir / "plan.json"),
                                      "existing_plan": True, "next": "review/edit the plan, then rerun with --plan-approved"}, indent=2))
                    return
                if args.plan_approved:
                    missing = [str(p) for p in required if not p.exists()]
                    if missing:
                        raise CommandError(["deep_search_loop", "--plan-approved"], missing=[Path(p) for p in missing], description="resume preflight")
                else:
                    if not (edir / "union.jsonl").exists():
                        run([sys.executable, ROBUST, "--jd-file", args.jd_file, "--run-dir", edir, "--env-file", args.env_file,
                             "--n", args.n, "--keep", args.keep, "--max-rounds", 2]
                            + (["--backend", "local", "--db", args.db] if args.backend == "local" else [])
                            + (["--set-id", args.set_id] if args.set_id else []),
                            expected_paths=[edir / "union.jsonl"], description="epoch0 robust_source")
                    build_cmd: list[object] = [sys.executable, BUILD, "--run-dir", edir, "--created-at", args.created_at]
                    if args.approved_plan:
                        build_cmd += ["--plan", plan_path]
                    else:
                        build_cmd += ["--jd-file", args.jd_file]
                    if args.set_id:
                        build_cmd += ["--set-id", args.set_id]
                    run(build_cmd, expected_paths=[edir / "plan.json", edir / "candidate_frontier.jsonl", edir / "candidate_frontier.json", edir / "probe_summaries.json"],
                        description="epoch0 build_eval_inputs")
                    if not args.approved_plan:
                        history.append({"epoch": 0, "status": "awaiting_plan_approval", "plan": str(edir / "plan.json")})
                        (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
                        print(json.dumps({"primitive": "deep_search_loop", "status": "awaiting_plan_approval", "plan": str(edir / "plan.json"),
                                          "next": "review/edit the plan, then rerun with --plan-approved"}, indent=2))
                        return
                    plan_path = Path(args.approved_plan)
            else:
                sr = run_dir / "shortlist" / "ground_truth_ranked.json"
                strong = json.loads(sr.read_text()) if sr.exists() else []
                anchors = diverse_anchors(strong, master_union, args.anchors)
                if not anchors:
                    history.append({"epoch": epoch, "stopped": "no_anchors_giveup"})
                    break
                (edir / "anchors.json").write_text(json.dumps(anchors, indent=2))
                run([sys.executable, EXPAND, "--anchors", edir / "anchors.json", "--top-k", len(anchors), "--out", edir / "anchor_seeds.json"],
                    expected_paths=[edir / "anchor_seeds.json"], description=f"epoch{epoch} expand_from_anchor")
                run([sys.executable, WIDE_SEARCH, "--seeds", edir / "anchor_seeds.json", "--run-dir", edir, "--env-file", args.env_file,
                     "--limit", args.keep]
                    + (["--backend", "local", "--db", args.db] if args.backend == "local" else [])
                    + (["--set-id", args.set_id] if args.set_id else []),
                    expected_paths=[edir / "union.jsonl"], description=f"epoch{epoch} run_wide_search")
                build_cmd = [sys.executable, BUILD, "--run-dir", edir, "--plan", plan_path, "--created-at", args.created_at]
                if args.set_id:
                    build_cmd += ["--set-id", args.set_id]
                run(build_cmd, expected_paths=[edir / "plan.json", edir / "candidate_frontier.jsonl", edir / "candidate_frontier.json", edir / "probe_summaries.json"],
                    description=f"epoch{epoch} build_eval_inputs")

            # accumulate union; judge ONLY new pids without mutating canonical frontier artifacts
            for r in _jsonl(edir / "union.jsonl"):
                master_union.setdefault(r["person_id"], r)
            frontier = _jsonl(edir / "candidate_frontier.jsonl")
            new = [c for c in frontier if (c.get("person_id") or c.get("candidate_id")) not in judged_pids]
            (edir / "candidate_frontier.to_judge.jsonl").write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in new))
            new_judged = 0
            if new:
                judge(edir, new, args.judge, args.reasoning_effort, args.concurrency)
                verds = _jsonl(edir / "candidate_evaluations.raw.jsonl")
                with master_judge.open("a") as fh:
                    for v in verds:
                        fh.write(json.dumps(v) + "\n")
                judged_pids |= {c.get("person_id") or c.get("candidate_id") for c in new}
                new_judged = len(verds)
            master_union_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in master_union.values()))

            # consensus over everything judged so far
            run([sys.executable, CONSENSUS, "--judges-dir", judges_dir, "--union", master_union_path,
                 "--out-dir", run_dir / "shortlist", "--min-inband-votes", 1, "--score-threshold", args.score_threshold,
                 "--plan", plan_path],
                expected_paths=[run_dir / "shortlist" / "consensus.json", run_dir / "shortlist" / "ground_truth_ranked.json"],
                description=f"epoch{epoch} consensus")  # core-gate the shortlist on the plan's core domain must-haves
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
    except CommandError as exc:
        history.append({"status": "failed", "error": str(exc), "details": exc.to_dict()})
        (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": str(exc), "details": exc.to_dict(), "history": history}, indent=2))
        raise SystemExit(1) from exc

    (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
    print(json.dumps({"primitive": "deep_search_loop", "status": "completed", "epochs": len(history),
                      "strong_total": len(strong_pids), "shortlist": str(run_dir / "shortlist" / "ground_truth_ranked.json"),
                      "history": history}, indent=2))


if __name__ == "__main__":
    main()
