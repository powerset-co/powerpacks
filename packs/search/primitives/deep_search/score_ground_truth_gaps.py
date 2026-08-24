"""Score one canonical typed candidate frontier against ground truth.

Ground truth is the finalized Reflect human review. This primitive scores one canonical
typed candidate frontier against it.

Inputs:
  --ground-truth  Finalized reflect.ground_truth.v1 human-reviewed labels.
  --candidate-frontier  the run's canonical candidate-frontier.json envelope.
  --usage-log     optional usage.jsonl (one row per LLM call, from the shared client's
                  POWERPACKS_USAGE_LOG capture); fills cost_usd from the committed
                  model-prices table when present.
Output:
  gaps.json   recall@k, precision@k, ndcg@k, missed GT ids (+ their GT rank), net-new finds

No network, no spend — pure set math.

Changelog:
  2026-07-30  ndcg@k with graded gains from finalized strong/bench labels;
              --usage-log auto-cost via packs/search/data/model-prices.json.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
from usage_pricing import PRICES_PATH as DEFAULT_PRICES_PATH, load_prices, row_cost_usd  # noqa: E402
from packs.search.primitives.validate_artifact.validate_artifact import validate_file  # noqa: E402
from packs.search.reflect.review import validate_ground_truth_semantics  # noqa: E402
from packs.search.pipeline.frontier import CandidateFrontier  # noqa: E402

PRICES_PATH = DEFAULT_PRICES_PATH


def validate_reflect_ground_truth(path: Path, raw: Any | None = None) -> dict[str, Any]:
    raw = raw if raw is not None else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "reflect.ground_truth.v1":
        raise ValueError("ground truth must use reflect.ground_truth.v1")
    document = validate_file("reflect-ground-truth", path)
    validate_ground_truth_semantics(document)
    return document


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text[0] == "[":
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_ground_truth(path: Path, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Return finalized eligible labels and gains in review order."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    document = validate_reflect_ground_truth(path, raw)
    records: list[dict[str, Any]] = []
    gains: dict[str, float] = {}
    for label in document["labels"]:
        decision = label["decision"]
        if decision not in {"eligible_strong", "eligible_bench"}:
            continue
        person_id = label["person_id"]
        gain = 3.0 if decision == "eligible_strong" else 2.0
        records.append({"person_id": person_id, "tier": "A" if gain == 3.0 else "B"})
        gains[person_id] = gain
    return records, gains


def ndcg_at_k(gains: dict[str, float], candidate_ids: list[str], k: int) -> float:
    dcg = sum(gains.get(pid, 0.0) / math.log2(i + 2) for i, pid in enumerate(candidate_ids[:k]))
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return round(dcg / idcg, 4) if idcg else 0.0


def usage_cost(usage_log: Path) -> dict[str, Any]:
    """Sum tokens from a usage.jsonl; price them via the shared usage_pricing table."""
    prices = load_prices(PRICES_PATH)
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    cost = 0.0
    priced = True
    for row in load_records(usage_log):
        totals["calls"] += 1
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            totals[field] += int(row.get(field) or 0)
        row_cost = row_cost_usd(row, prices)
        if row_cost is None:
            priced = False
            continue
        cost += row_cost
    totals["cost_usd"] = round(cost, 4)
    totals["fully_priced"] = priced
    return totals


def recall_at_k(gt_ids: set[str], candidate_ids: list[str], k: int) -> float:
    if not gt_ids:
        return 0.0
    topk = set(candidate_ids[:k])
    return round(len(gt_ids & topk) / len(gt_ids), 4)


def precision_at_k(gt_ids: set[str], candidate_ids: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    topk = candidate_ids[:k]
    return round(sum(1 for pid in topk if pid in gt_ids) / k, 4)


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a typed candidate frontier against ground truth.")
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--candidate-frontier", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ks", default="10,25,50", help="Comma-separated K values for recall/precision")
    ap.add_argument("--usage-log", default=None, help="Optional usage.jsonl; fills cost_usd from model-prices.json")
    args = ap.parse_args()

    frontier = CandidateFrontier.read(args.candidate_frontier)
    if frontier.truncated:
        raise ValueError("strict scoring rejects a truncated candidate frontier")
    candidates = [row.to_dict() for row in frontier.candidates]
    candidate_ids = [row.person_id for row in frontier.candidates]
    candidate_set = set(candidate_ids)

    gt, gains = load_ground_truth(Path(args.ground_truth), candidates)
    raw_gt = json.loads(Path(args.ground_truth).read_text(encoding="utf-8"))
    reviewed_pool = set((raw_gt.get("review_pool_evidence_hashes") or {}).keys()) if isinstance(raw_gt, dict) and raw_gt.get("schema_version") == "reflect.ground_truth.v1" else None
    insufficient_ids = {
        row["person_id"] for row in (raw_gt.get("labels") or []) if row.get("decision") == "insufficient_evidence"
    } if reviewed_pool is not None else set()
    scored_candidate_ids = [pid for pid in candidate_ids if pid not in insufficient_ids]
    unreviewed = [pid for pid in candidate_ids if reviewed_pool is not None and pid not in reviewed_pool]
    if unreviewed:
        scored_candidate_ids = [pid for pid in scored_candidate_ids if pid not in set(unreviewed)]
    gt_rank = {r["person_id"]: i + 1 for i, r in enumerate(gt)}
    gt_name = {r["person_id"]: r.get("name") for r in gt}
    gt_ids = set(gt_rank)

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    recall = {f"recall@{k}": recall_at_k(gt_ids, scored_candidate_ids, k) for k in ks}
    precision = {f"precision@{k}": precision_at_k(gt_ids, scored_candidate_ids, k) for k in ks}
    ndcg = {f"ndcg@{k}": ndcg_at_k(gains, scored_candidate_ids, k) for k in ks}

    usage = usage_cost(Path(args.usage_log)) if args.usage_log and Path(args.usage_log).exists() else None
    cost_usd = (usage or {}).get("cost_usd", 0.0)

    missed = [
        {"person_id": pid, "name": gt_name.get(pid), "gt_rank": gt_rank[pid]}
        for pid in sorted(gt_ids - candidate_set, key=lambda p: gt_rank[p])
    ]
    net_new = [pid for pid in candidate_ids if pid not in gt_ids]
    overall_recall = round(len(gt_ids & candidate_set) / len(gt_ids), 4) if gt_ids else 0.0

    gaps = {
        "primitive": "score_ground_truth_gaps",
        "ground_truth_size": len(gt_ids),
        "candidate_count": len(candidate_ids),
        "overall_recall": overall_recall,
        **recall,
        **precision,
        **ndcg,
        "missed_count": len(missed),
        "missed": missed,
        "net_new_count": len(net_new),
        "unreviewed_candidate_count": len(unreviewed),
        "cost_usd": cost_usd,
        "usage": usage,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gaps, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: gaps[k] for k in ("ground_truth_size", "overall_recall", *recall.keys(), *precision.keys(), *ndcg.keys(), "missed_count", "net_new_count", "cost_usd")}, indent=2))


if __name__ == "__main__":
    main()
