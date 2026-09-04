#!/usr/bin/env python3
"""Compare the old rerank and JD-fit ordering against human fit labels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Repo-root bootstrap for direct script execution.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.search.primitives.deep_search.fit_contract import TraitStatus


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _ranking(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, float | int]:
    review = [row for row in rows if row["human"]["overall"] == "review"]
    passed = [row for row in rows if row["human"]["overall"] == "pass"]
    comparisons = []
    for good in review:
        for bad in passed:
            left = float(good["model"][key])
            right = float(bad["model"][key])
            comparisons.append(1.0 if left > right else .5 if left == right else 0.0)
    ordered = sorted(rows, key=lambda row: float(row["model"][key]), reverse=True)[:20]
    precision = _mean([row["human"]["overall"] == "review" for row in ordered])
    return {"pairwise_accuracy": _mean(comparisons), "pairs": len(comparisons),
            "precision_at_20": precision}


def _traits(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    ladder = list(TraitStatus)
    exact = []
    within_one = []
    for row in rows:
        model = {value["trait"]: value["status"]
                 for value in row["model"]["jd_fit"]["traits"]}
        for human in row["human"]["traits"]:
            predicted = TraitStatus(model[human["trait"]])
            actual = TraitStatus(human["status"])
            exact.append(predicted is actual)
            within_one.append(abs(ladder.index(predicted) - ladder.index(actual)) <= 1)
    return {"exact_agreement": _mean(exact), "within_one_rung": _mean(within_one)}


def evaluate_label_files(paths: Iterable[Path]) -> dict[str, Any]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                latest[(row["run_id"], row["person_id"])] = row
    by_run: dict[str, list[dict[str, Any]]] = {}
    for row in latest.values():
        by_run.setdefault(row["run_id"], []).append(row)
    run_reports = {
        run_id: {
            "labels": len(rows),
            "rerank": _ranking(rows, "rerank_score"),
            "jd_fit": _ranking([
                {**row, "model": {**row["model"],
                                   "coverage": row["model"]["jd_fit"]["coverage"]}}
                for row in rows
            ], "coverage"),
        }
        for run_id, rows in sorted(by_run.items())
    }
    rerank_pairs = [report["rerank"]["pairwise_accuracy"]
                    for report in run_reports.values() if report["rerank"]["pairs"]]
    fit_pairs = [report["jd_fit"]["pairwise_accuracy"]
                 for report in run_reports.values() if report["jd_fit"]["pairs"]]
    rerank_precision = [report["rerank"]["precision_at_20"]
                        for report in run_reports.values()]
    fit_precision = [report["jd_fit"]["precision_at_20"]
                     for report in run_reports.values()]
    rows = list(latest.values())
    return {
        "counts": {
            "runs": len(by_run), "labels": len(rows),
            "review": sum(row["human"]["overall"] == "review" for row in rows),
            "pass": sum(row["human"]["overall"] == "pass" for row in rows),
            "trait_labels": sum(len(row["human"]["traits"]) for row in rows),
        },
        "ranking": {
            "rerank": {"pairwise_accuracy": _mean(rerank_pairs),
                       "precision_at_20": _mean(rerank_precision)},
            "jd_fit": {"pairwise_accuracy": _mean(fit_pairs),
                       "precision_at_20": _mean(fit_precision)},
        },
        "traits": _traits(rows),
        "runs": run_reports,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    paths = sorted(args.root.glob("*/fit-labels.jsonl"))
    print(json.dumps(evaluate_label_files(paths), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
