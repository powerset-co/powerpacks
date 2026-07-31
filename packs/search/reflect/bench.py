"""Reflect bench CLI: score a deep-search run against GT, aggregate a suite report, gate changes.

The maintainer-side half of the Reflect family (see README.md in this directory). Three
subcommands, all pure local file math over existing run artifacts — no network, no spend:

  score   one run dir + one GT file -> funnel + gaps(+ndcg) + cost -> \
          .powerpacks/reflect/results/<slug>/result.json (fixed path, overwritten in place)
  report  all results + suite metas -> .powerpacks/reflect/report.json (per-JD rows,
          per-job-family aggregates; suite entries without a result are listed gt-pending)
  gate    current report vs a baseline report -> per-JD regression verdicts. WARN-ONLY by
          default (prints verdicts, exits 0); --enforce makes failures exit 1 once floors
          have been ratified from measured variance.

Scorer primitives are invoked in-process (argv-driven main()), never as subprocesses.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEEP_SEARCH_DIR = ROOT / "packs" / "search" / "primitives" / "deep_search"
SUITE_DIR = Path(__file__).resolve().parent / "suite"
REFLECT_STATE = ROOT / ".powerpacks" / "reflect"
RESULTS_DIR = REFLECT_STATE / "results"
REPORT_PATH = REFLECT_STATE / "report.json"

GATE_METRICS = ("overall_recall", "ndcg@10", "ndcg@22")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_cli_main(mod, argv: list[str]) -> None:
    saved = sys.argv
    sys.argv = argv
    try:
        mod.main()
    finally:
        sys.argv = saved


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    slug = args.slug or run_dir.name
    shortlist_dir = run_dir / "shortlist"

    sf = _load_module("score_funnel", DEEP_SEARCH_DIR / "score_funnel.py")
    _run_cli_main(sf, ["score_funnel", "--run-dir", str(run_dir), "--ground-truth", args.gt,
                       "--score-threshold", str(args.score_threshold)])
    funnel = _read_json(shortlist_dir / "funnel.json")

    epoch_candidates = None
    for name in ("ranked_final.json", "shortlist_ranked.json", "ground_truth_ranked.json"):
        if (shortlist_dir / name).exists():
            epoch_candidates = shortlist_dir / name
            break
    gaps = None
    if epoch_candidates is not None:
        sg = _load_module("score_ground_truth_gaps", DEEP_SEARCH_DIR / "score_ground_truth_gaps.py")
        argv = ["score_ground_truth_gaps", "--ground-truth", args.gt,
                "--epoch-candidates", str(epoch_candidates), "--epoch-dir", str(shortlist_dir),
                "--epoch-label", "bench-score", "--convergence-csv", str(run_dir / "convergence.csv"),
                "--ks", args.ks]
        usage_log = Path(args.usage_log) if args.usage_log else run_dir / "usage.jsonl"
        if usage_log.exists():
            argv += ["--usage-log", str(usage_log)]
        _run_cli_main(sg, argv)
        gaps = _read_json(shortlist_dir / "gaps.json")

    cost = None
    usage_log = Path(args.usage_log) if args.usage_log else run_dir / "usage.jsonl"
    if usage_log.exists():
        cr = _load_module("cost_report", Path(__file__).resolve().parent / "cost_report.py")
        cost = cr.build_report(cr.load_rows(usage_log), cr.load_prices(cr.DEFAULT_PRICES_PATH))

    meta_path = SUITE_DIR / slug / "meta.json"
    result = {
        "slug": slug,
        "run_dir": str(run_dir),
        "ground_truth": args.gt,
        "meta": _read_json(meta_path) if meta_path.exists() else None,
        "funnel": {k: funnel[k] for k in ("gt_size", "funnel_line", "dispositions", "thresholds", "shortlist_source")},
        "gaps": gaps and {k: v for k, v in gaps.items() if k != "missed"},
        "missed_count": gaps and gaps.get("missed_count"),
        "cost": cost and cost["totals"],
        "generated_at": _now(),
    }
    out = RESULTS_DIR / slug / "result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"score[{slug}]: {funnel['funnel_line']}", file=sys.stderr)
    print(json.dumps({"slug": slug, "result_json": str(out), "funnel_line": funnel["funnel_line"],
                      "dispositions": funnel["dispositions"],
                      "overall_recall": (gaps or {}).get("overall_recall"),
                      "cost_usd": (cost or {}).get("totals", {}).get("cost_usd")}, indent=2))
    return 0


def _metric_row(result: dict[str, Any]) -> dict[str, Any]:
    gaps = result.get("gaps") or {}
    meta = result.get("meta") or {}
    return {
        "slug": result["slug"],
        "job_family": meta.get("job_family", "unknown"),
        "expected_size_class": meta.get("expected_size_class"),
        "gt_size": (result.get("funnel") or {}).get("gt_size"),
        "funnel_line": (result.get("funnel") or {}).get("funnel_line"),
        "dispositions": (result.get("funnel") or {}).get("dispositions"),
        "overall_recall": gaps.get("overall_recall"),
        **{k: v for k, v in gaps.items() if k.startswith(("recall@", "precision@", "ndcg@"))},
        "cost_usd": (result.get("cost") or {}).get("cost_usd"),
        "generated_at": result.get("generated_at"),
    }


def cmd_report(args: argparse.Namespace) -> int:
    rows = []
    for result_path in sorted(RESULTS_DIR.glob("*/result.json")):
        rows.append(_metric_row(_read_json(result_path)))
    scored = {r["slug"] for r in rows}
    pending = []
    if SUITE_DIR.exists():
        for meta_path in sorted(SUITE_DIR.glob("*/meta.json")):
            slug = meta_path.parent.name
            if slug not in scored:
                meta = _read_json(meta_path)
                pending.append({"slug": slug, "job_family": meta.get("job_family", "unknown"),
                                "gt": meta.get("gt", "pending")})

    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        fam = families.setdefault(row["job_family"], {"jds": 0, "recalls": [], "costs": []})
        fam["jds"] += 1
        if row.get("overall_recall") is not None:
            fam["recalls"].append(row["overall_recall"])
        if row.get("cost_usd") is not None:
            fam["costs"].append(row["cost_usd"])
    aggregates = {
        fam: {
            "jds": data["jds"],
            "mean_recall": round(sum(data["recalls"]) / len(data["recalls"]), 4) if data["recalls"] else None,
            "min_recall": min(data["recalls"]) if data["recalls"] else None,
            "total_cost_usd": round(sum(data["costs"]), 4) if data["costs"] else None,
        }
        for fam, data in sorted(families.items())
    }

    report = {"generated_at": _now(), "jds": rows, "gt_pending": pending, "by_job_family": aggregates}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for row in rows:
        print(f"report: {row['slug']} [{row['job_family']}] recall={row.get('overall_recall')} "
              f"cost=${row.get('cost_usd') or 0}", file=sys.stderr)
    print(json.dumps({"report_json": str(REPORT_PATH), "jds_scored": len(rows),
                      "gt_pending": len(pending), "by_job_family": aggregates}, indent=2))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    baseline = {r["slug"]: r for r in _read_json(Path(args.baseline)).get("jds", [])}
    current = {r["slug"]: r for r in _read_json(Path(args.current)).get("jds", [])}

    verdicts = []
    for slug, base_row in sorted(baseline.items()):
        cur_row = current.get(slug)
        if cur_row is None:
            verdicts.append({"slug": slug, "verdict": "fail", "reason": "JD present in baseline but not in current report"})
            continue
        reasons = []
        for metric in GATE_METRICS:
            base_v, cur_v = base_row.get(metric), cur_row.get(metric)
            if base_v is not None and cur_v is not None and cur_v < base_v - args.epsilon:
                reasons.append(f"{metric} regressed {base_v} -> {cur_v} (epsilon {args.epsilon})")
        if args.min_recall is not None and (cur_row.get("overall_recall") or 0) < args.min_recall:
            reasons.append(f"overall_recall {cur_row.get('overall_recall')} below floor {args.min_recall}")
        if args.max_cost is not None and (cur_row.get("cost_usd") or 0) > args.max_cost:
            reasons.append(f"cost_usd {cur_row.get('cost_usd')} above ceiling {args.max_cost}")
        verdicts.append({"slug": slug, "verdict": "fail" if reasons else "pass", "reasons": reasons})

    failures = [v for v in verdicts if v["verdict"] == "fail"]
    mode = "enforce" if args.enforce else "warn-only"
    payload = {"mode": mode, "epsilon": args.epsilon, "jds_checked": len(verdicts),
               "failures": len(failures), "verdicts": verdicts}
    for v in failures:
        print(f"gate[{mode}]: FAIL {v['slug']}: {'; '.join(v.get('reasons') or [v.get('reason', '')])}", file=sys.stderr)
    print(json.dumps(payload, indent=2))
    return 1 if failures and args.enforce else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Reflect bench: score, report, and gate deep-search quality.")
    sub = ap.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="Score one run dir against a GT file")
    score.add_argument("--run-dir", required=True)
    score.add_argument("--gt", required=True)
    score.add_argument("--slug", default=None)
    score.add_argument("--ks", default="10,22,31")
    score.add_argument("--score-threshold", type=float, default=0.40)
    score.add_argument("--usage-log", default=None)

    sub.add_parser("report", help="Aggregate all results into report.json")

    gate = sub.add_parser("gate", help="Compare a current report against a baseline")
    gate.add_argument("--baseline", required=True)
    gate.add_argument("--current", default=str(REPORT_PATH))
    gate.add_argument("--epsilon", type=float, default=0.02)
    gate.add_argument("--min-recall", type=float, default=None)
    gate.add_argument("--max-cost", type=float, default=None)
    gate.add_argument("--enforce", action="store_true",
                      help="Exit 1 on failures (default: warn-only until floors are ratified)")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "score":
        raise SystemExit(cmd_score(args))
    if args.command == "report":
        raise SystemExit(cmd_report(args))
    raise SystemExit(cmd_gate(args))


if __name__ == "__main__":
    main()
