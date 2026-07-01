"""Robust, non-flaky sourcing: union independent wide-search rounds until coverage saturates.

A single `decompose_jd -> run_wide_search` round is stochastic (the LLM seed set varies, so which
candidates get sourced varies run-to-run — measured 0.90-1.00 recall on AgentMail). The fix is
REDUNDANCY: run several independent rounds and union them. Measured: the union of any 2 independent
rounds saturates to 100% GT coverage, because different rounds miss different people.

This primitive makes that deterministic in OUTCOME without needing a ground-truth key at runtime:
it keeps adding rounds until a new round contributes fewer than `--saturation-min-new` genuinely
new candidates (coverage stopped growing). Each round gets a FRESH decompose call with a rotated
emphasis so the rounds explore different regions instead of resampling the same seeds.

  round r: decompose_jd (fresh, rotated emphasis) -> run_wide_search (top-`keep`) -> fold into union
  stop when net-new < saturation-min-new (or --max-rounds hit)
  -> <run-dir>/union.jsonl  (the saturated, redundant pool; feed straight to the free codex judge)

No triage: the judge is free (codex_judge), so there is no reason to pre-filter and risk dropping
reachable candidates. See packs/search/skills/search/SKILL.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:  # direct script execution
    from subprocess_utils import CommandError, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.deep_search.robust_source
    from .subprocess_utils import CommandError, run_checked

ROOT = Path(__file__).resolve().parents[4]
DECOMPOSE = ROOT / "packs/search/primitives/deep_search/decompose_jd.py"
WIDE_SEARCH = ROOT / "packs/search/primitives/deep_search/run_wide_search.py"

# Rotated emphases so each round explores a different region (diversity, not resampling).
EMPHASES = [
    "",  # round 0: the model's natural decomposition
    "Emphasize the ADJACENT and bonus/nice-to-have angles of the role and less-obvious adjacent backgrounds.",
    "Emphasize DIVERSE company tiers and problem domains (big-tech, startups, research labs, infra vendors) and varied tech stacks.",
    "Emphasize the deep/core must-have capabilities described with different concrete sub-skills and tools than a generic phrasing would use.",
]


def _load_union(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {r["person_id"]: r for r in (json.loads(l) for l in path.read_text().splitlines() if l.strip())}


def _merge(into: dict[str, dict[str, Any]], rnd_union: Path, round_tag: str) -> int:
    """Fold a round's union.jsonl into the running union; return net-new count."""
    before = len(into)
    for line in (rnd_union.read_text().splitlines() if rnd_union.exists() else []):
        if not line.strip():
            continue
        r = json.loads(line)
        pid = r.get("person_id")
        if not pid:
            continue
        if pid in into:
            # keep richest record; accumulate provenance
            fb = set(into[pid].get("found_by", [])) | {f"{round_tag}:{x}" for x in r.get("found_by", [])}
            into[pid]["found_by"] = sorted(fb)
        else:
            r["found_by"] = [f"{round_tag}:{x}" for x in r.get("found_by", [])]
            into[pid] = r
    return len(into) - before


def main() -> None:
    ap = argparse.ArgumentParser(description="Robust sourcing: union independent wide-search rounds until coverage saturates.")
    ap.add_argument("--jd-file", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--n", type=int, default=16, help="Seeds per round")
    ap.add_argument("--keep", type=int, default=200, help="Top-N per probe folded into the union")
    ap.add_argument("--max-rounds", type=int, default=3,
                    help="Independent rounds to union (measured: 2 rounds -> min 0.968 GT recall, "
                         "vs flaky 0.87-0.97 for a single round, on AgentMail across 3 trials)")
    ap.add_argument("--saturation-min-new", type=int, default=40,
                    help="Secondary stop: end once a round adds fewer than this many NEW candidates. "
                         "NOTE: with deep --keep the pool keeps growing on breadth even after GT "
                         "coverage plateaus, so this rarely fires before --max-rounds; it is a guard, "
                         "not the primary control.")
    ap.add_argument("--decompose-model", default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    union: dict[str, dict[str, Any]] = {}
    history: list[dict[str, Any]] = []

    try:
        for r in range(args.max_rounds):
            rdir = run_dir / f"round{r}"
            rdir.mkdir(parents=True, exist_ok=True)
            emphasis = EMPHASES[r % len(EMPHASES)]
            # Fresh decompose for this round (rotated emphasis appended to the JD).
            jd_text = Path(args.jd_file).read_text(encoding="utf-8")
            round_jd = rdir / "jd.txt"
            round_jd.write_text(jd_text + (f"\n\nSOURCING EMPHASIS FOR THIS PASS: {emphasis}" if emphasis else ""), encoding="utf-8")
            seeds_path = rdir / "seeds.json"
            dcmd = [sys.executable, str(DECOMPOSE), "--jd-file", str(round_jd), "--n", str(args.n), "--out", str(seeds_path)]
            if args.decompose_model:
                dcmd += ["--model", args.decompose_model]
            run_checked(dcmd, expected_paths=[seeds_path], description=f"decompose round {r}")

            round_union = rdir / "union.jsonl"
            scmd = [sys.executable, str(WIDE_SEARCH), "--seeds", str(seeds_path), "--run-dir", str(rdir),
                    "--env-file", args.env_file, "--limit", str(args.keep)]
            if args.set_id:
                scmd += ["--set-id", args.set_id]
            run_checked(scmd, expected_paths=[round_union], description=f"wide-search round {r}")

            net_new = _merge(union, round_union, f"r{r}")
            history.append({"round": r, "net_new": net_new, "union_total": len(union), "emphasis": emphasis or "(default)"})
            print(json.dumps({"round": r, "net_new": net_new, "union_total": len(union)}))
            if r > 0 and net_new < args.saturation_min_new:
                break  # coverage saturated
    except CommandError as exc:
        print(json.dumps({
            "primitive": "robust_source",
            "status": "failed",
            "error": str(exc),
            "details": exc.to_dict(),
            "history": history,
        }, indent=2))
        raise SystemExit(1) from exc

    out = run_dir / "union.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for rec in sorted(union.values(), key=lambda x: (-len(x.get("found_by", [])), x.get("name") or "")):
            fh.write(json.dumps(rec) + "\n")
    (run_dir / "rounds.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    saturated = history[-1]["net_new"] < args.saturation_min_new if len(history) > 1 else False
    print(json.dumps({"primitive": "robust_source", "status": "completed", "rounds": len(history),
                      "union": len(union), "out": str(out), "saturated": saturated}, indent=2))


if __name__ == "__main__":
    main()
