#!/usr/bin/env python3
"""Score the deterministic query router (route_query.classify) against labeled queries.

This is the ROUTING eval the search consolidation was missing: before folding search-network
into a `$search` that routes deep queries to `$recruit`, we need a measurable baseline for
"picks the right surface". Pure string rules, no LLM, no spend — runnable in CI.

Reads packs/search/evals/routing/cases.json (each: {query, expected, [acceptable], [subroute]}).
A prediction is correct if it equals `expected` or is in `acceptable` (genuinely ambiguous cases).
Prints strict accuracy (expected only), lenient accuracy (expected ∪ acceptable), a confusion
matrix, and every miss. Writes a JSON report next to the cases.

Usage:
  uv run --project . python packs/search/evals/run_routing_eval.py
  ... --cases <path> --report <path.json>
Exit code is non-zero if strict accuracy < --min-accuracy (default 0.0 = report only).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # packs/search
sys.path.insert(0, str(ROOT / "primitives" / "route_query"))
import route_query as rq  # noqa: E402

DEFAULT_CASES = ROOT / "evals" / "routing" / "cases.json"
DEFAULT_REPORT = ROOT / "evals" / "routing" / "report.json"
ROUTES = list(rq.ROUTES)


def run(cases: list[dict]) -> dict:
    misses: list[dict] = []
    sub_total = sub_correct = 0
    strict_correct = lenient_correct = 0
    # confusion[expected][predicted] = count
    confusion = {r: {p: 0 for p in ROUTES} for r in ROUTES}

    for c in cases:
        expected = c["expected"]
        acceptable = set(c.get("acceptable", [])) | {expected}
        d = rq.classify(c["query"])
        pred = d.route
        confusion[expected][pred] += 1
        strict = pred == expected
        lenient = pred in acceptable
        strict_correct += int(strict)
        lenient_correct += int(lenient)
        if not lenient:
            misses.append({"id": c.get("id"), "query": c["query"][:80], "expected": expected,
                           "acceptable": sorted(acceptable), "predicted": pred, "rule": d.rule})
        # subroute check (network local-vs-turbopuffer), only where labeled
        if c.get("subroute"):
            sub_total += 1
            sub_correct += int(d.subroute == c["subroute"])
            if d.subroute != c["subroute"]:
                misses.append({"id": c.get("id"), "kind": "subroute", "expected_subroute": c["subroute"],
                               "predicted_subroute": d.subroute})

    n = len(cases)
    return {
        "primitive": "run_routing_eval",
        "n_cases": n,
        "strict_accuracy": round(strict_correct / n, 4) if n else 0.0,
        "lenient_accuracy": round(lenient_correct / n, 4) if n else 0.0,
        "strict_correct": strict_correct,
        "lenient_correct": lenient_correct,
        "subroute_accuracy": round(sub_correct / sub_total, 4) if sub_total else None,
        "subroute_n": sub_total,
        "per_route": {
            r: {
                "n": sum(confusion[r].values()),
                "correct": confusion[r][r],
                "recall": round(confusion[r][r] / sum(confusion[r].values()), 4) if sum(confusion[r].values()) else 0.0,
            }
            for r in ROUTES
        },
        "confusion": confusion,
        "misses": misses,
    }


def _print_confusion(confusion: dict) -> None:
    hdr = "expected\\pred".ljust(10) + "".join(p[:5].rjust(9) for p in ROUTES)
    print(hdr)
    for r in ROUTES:
        row = r[:10].ljust(10) + "".join(str(confusion[r][p]).rjust(9) for p in ROUTES)
        print(row)


def main() -> None:
    ap = argparse.ArgumentParser(description="Score route_query.classify vs labeled queries.")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--min-accuracy", type=float, default=0.0, help="Exit non-zero if strict accuracy < this (CI gate).")
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    result = run(cases)

    print(f"routing eval: {result['n_cases']} cases")
    print(f"  strict accuracy  : {result['strict_accuracy']:.4f} ({result['strict_correct']}/{result['n_cases']})")
    print(f"  lenient accuracy : {result['lenient_accuracy']:.4f} ({result['lenient_correct']}/{result['n_cases']})  (expected ∪ acceptable)")
    if result["subroute_accuracy"] is not None:
        print(f"  subroute accuracy: {result['subroute_accuracy']:.4f} ({result['subroute_n']} network local/TP cases)")
    print("  per-route recall :", {r: result["per_route"][r]["recall"] for r in ROUTES})
    print("\nconfusion matrix (rows=expected, cols=predicted):")
    _print_confusion(result["confusion"])
    if result["misses"]:
        print("\nmisses:")
        for m in result["misses"]:
            print("  ", json.dumps(m))
    else:
        print("\nno misses.")

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nreport -> {args.report}")

    if result["strict_accuracy"] < args.min_accuracy:
        print(f"FAIL: strict accuracy {result['strict_accuracy']} < min {args.min_accuracy}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
