#!/usr/bin/env python3
"""Copy a saved search for local labeling without model judgments or API calls."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def create_preview(source: Path, destination: Path) -> Path:
    source, destination = source.resolve(), destination.resolve()
    results = json.loads((source / "results.json").read_text())
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source / "jd.txt", destination / "jd.txt")
    plan_path = source / "epoch0/plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text())
        plan["traits"] = []
        (destination / "epoch0").mkdir()
        (destination / "epoch0/plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    results["brief"]["defining_capability"] = None
    results["raw_model_responses"] = []
    results["pending_payload"] = None
    results["pending_query"] = None
    for index, iteration in enumerate(results["iterations"], start=1):
        arm = iteration["arm"]
        copied = {}
        artifact_dir = destination / "ponds" / f"iteration-{index:02d}"
        artifact_dir.mkdir(parents=True)
        for field in ("jsonl", "profiles_path"):
            if not arm["artifacts"].get(field):
                continue
            original = Path(arm["artifacts"][field])
            if not original.is_absolute():
                original = source.parents[2] / original
            target = artifact_dir / original.name
            shutil.copy2(original, target)
            copied[field] = str(target)
        arm["artifacts"] = copied
        arm.pop("ledger", None)
        arm.pop("payload_json", None)
        for candidate in iteration.get("shortlist_grades") or []:
            candidate.update({
                "fit_experts": {}, "applied_precedent_ids": [],
                "applied_fit_precedents": [], "group": "", "why": "",
                "jd_fit": {"coverage": 0.0, "traits": []}, "fit_annotation_source": "",
            })
            candidate.pop("fit_override", None)

    summary = results["summary"]
    summary["groups"] = {}
    summary["counts"] = {}
    summary["jd_fit_order"] = []
    summary["deduped_candidate_count"] = len({
        candidate["person"] for iteration in results["iterations"]
        for candidate in iteration.get("shortlist_grades") or []
    })
    summary["pond_chain"] = [
        {**pond, "run": destination.name} for pond in summary.get("pond_chain") or []
        if pond["run"] == source.name
    ]
    results_path = destination / "results.json"
    results_path.write_text(json.dumps(results, indent=2) + "\n")
    return results_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"results": str(create_preview(args.source, args.destination))}))


if __name__ == "__main__":
    main()
