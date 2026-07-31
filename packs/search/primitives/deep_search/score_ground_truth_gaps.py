"""Score one harness epoch against the ground-truth set, and track convergence.

Ground truth = the trusted gold set built by the full agentic + mixture-of-judges method
(see packs/search/docs/agentic-search.md). Each cheaper/tuned harness run is an "epoch"; this
primitive measures the gap so successive epochs can converge toward ground truth.

Inputs:
  --ground-truth  ground_truth_ranked.json (list of {person_id, name, mean_score, ...}),
                  or a tiered {"tiers": {"A_...": [...], ...}} dict (names resolved against
                  the epoch candidates; REMOVED_* tiers excluded) for graded NDCG gains.
  --epoch-candidates  the epoch's candidates as JSONL or JSON list, each with person_id and
                      (optionally) a rank/score; order is taken as rank if no rank field.
  --usage-log     optional usage.jsonl (one row per LLM call, from the shared client's
                  POWERPACKS_USAGE_LOG capture); fills cost_usd from the committed
                  model-prices table when present. --cost-usd stays as a manual override.
Outputs (under the epoch dir):
  gaps.json   recall@k, precision@k, ndcg@k, missed GT ids (+ their GT rank), net-new finds
And appends one row to convergence.csv (created if absent).

No network, no spend — pure set math.

Changelog:
  2026-07-30  ndcg@k (graded gains from tiered GT: A=3/B=2/C=1, binary otherwise);
              --usage-log auto-cost via packs/search/data/model-prices.json.
"""
from __future__ import annotations

import argparse
import csv
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

TIER_GAINS = {"A": 3.0, "B": 2.0, "C": 1.0}
PRICES_PATH = DEFAULT_PRICES_PATH


def validate_reflect_ground_truth(path: Path, raw: Any | None = None) -> None:
    raw = raw if raw is not None else json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema_version") == "reflect.ground_truth.v1":
        document = validate_file("reflect-ground-truth", path)
        validate_ground_truth_semantics(document)


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text[0] == "[":
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def ranked_ids(records: list[dict[str, Any]]) -> list[str]:
    """Return person_ids in rank order. Honor an explicit 'rank' field if present,
    else honor 'score' (desc), else keep file order."""
    if records and any("rank" in r for r in records):
        records = sorted(records, key=lambda r: (r.get("rank") if r.get("rank") is not None else 1e9))
    elif records and any("score" in r or "mean_score" in r for r in records):
        records = sorted(records, key=lambda r: -float(r.get("score") or r.get("mean_score") or 0.0))
    out = []
    for r in records:
        pid = r.get("person_id")
        if pid and pid not in out:
            out.append(pid)
    return out


def load_ground_truth(path: Path, epoch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Return (gt_records_in_rank_order, gains_by_person_id).

    Flat list -> gain from an optional per-record 'tier' (A/B/C), else 1.0.
    Tiered dict -> names resolved against the epoch candidates; REMOVED_* excluded.
    Unresolved tiered names still count in the GT denominator (person_id = name-keyed
    sentinel) — they were not found, which is exactly what recall should say.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    validate_reflect_ground_truth(path, raw)
    if isinstance(raw, list):
        gains = {}
        for rec in raw:
            tier = str(rec.get("tier") or "")[:1].upper()
            gains[rec["person_id"]] = TIER_GAINS.get(tier, 1.0)
        return raw, gains

    if raw.get("schema_version") == "reflect.ground_truth.v1":
        records = []
        gains = {}
        for label in raw.get("labels") or []:
            decision = label.get("decision")
            if decision not in {"eligible_strong", "eligible_bench"}:
                continue
            person_id = label["person_id"]
            gain = 3.0 if decision == "eligible_strong" else 2.0
            records.append({"person_id": person_id, "tier": "A" if gain == 3.0 else "B"})
            gains[person_id] = gain
        return records, gains

    name_to_pid = {" ".join((r.get("name") or "").lower().split()): r.get("person_id") for r in epoch}
    records: list[dict[str, Any]] = []
    gains: dict[str, float] = {}
    unresolved = 0
    for tier_key, entries in (raw.get("tiers") or {}).items():
        letter = tier_key[:1].upper()
        if letter not in TIER_GAINS:
            continue
        for rec in entries:
            pid = rec.get("person_id") or name_to_pid.get(" ".join((rec.get("name") or "").lower().split()))
            if not pid:
                pid = f"unresolved:{rec.get('name')}"
                unresolved += 1
            records.append({"person_id": pid, "name": rec.get("name"), "tier": letter})
            gains[pid] = TIER_GAINS[letter]
    if unresolved:
        print(f"score_ground_truth_gaps: {unresolved} tiered GT name(s) unresolved against epoch candidates", file=sys.stderr)
    return records, gains


def ndcg_at_k(gains: dict[str, float], epoch_ids: list[str], k: int) -> float:
    dcg = sum(gains.get(pid, 0.0) / math.log2(i + 2) for i, pid in enumerate(epoch_ids[:k]))
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


def recall_at_k(gt_ids: set[str], epoch_ids: list[str], k: int) -> float:
    if not gt_ids:
        return 0.0
    topk = set(epoch_ids[:k])
    return round(len(gt_ids & topk) / len(gt_ids), 4)


def precision_at_k(gt_ids: set[str], epoch_ids: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    topk = epoch_ids[:k]
    return round(sum(1 for pid in topk if pid in gt_ids) / k, 4)


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a harness epoch vs ground truth + track convergence.")
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--epoch-candidates", required=True)
    ap.add_argument("--epoch-dir", required=True, help="Per-epoch output dir (gaps.json written here)")
    ap.add_argument("--epoch-label", required=True, help="e.g. epoch-01 (row key in convergence.csv)")
    ap.add_argument("--convergence-csv", required=True, help="Appended one row per epoch")
    ap.add_argument("--ks", default="10,25,50", help="Comma-separated K values for recall/precision")
    ap.add_argument("--cost-usd", type=float, default=0.0, help="Optional: epoch spend, for the convergence row")
    ap.add_argument("--usage-log", default=None, help="Optional usage.jsonl; fills cost_usd from model-prices.json")
    args = ap.parse_args()

    epoch = load_records(Path(args.epoch_candidates))
    epoch_ids = ranked_ids(epoch)
    epoch_set = set(epoch_ids)

    gt, gains = load_ground_truth(Path(args.ground_truth), epoch)
    raw_gt = json.loads(Path(args.ground_truth).read_text(encoding="utf-8"))
    reviewed_pool = set((raw_gt.get("review_pool_evidence_hashes") or {}).keys()) if isinstance(raw_gt, dict) and raw_gt.get("schema_version") == "reflect.ground_truth.v1" else None
    insufficient_ids = {
        row["person_id"] for row in (raw_gt.get("labels") or []) if row.get("decision") == "insufficient_evidence"
    } if reviewed_pool is not None else set()
    scored_epoch_ids = [pid for pid in epoch_ids if pid not in insufficient_ids]
    unreviewed = [pid for pid in epoch_ids if reviewed_pool is not None and pid not in reviewed_pool]
    if unreviewed:
        scored_epoch_ids = [pid for pid in scored_epoch_ids if pid not in set(unreviewed)]
    gt_rank = {r["person_id"]: i + 1 for i, r in enumerate(gt)}
    gt_name = {r["person_id"]: r.get("name") for r in gt}
    gt_ids = set(gt_rank)

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    recall = {f"recall@{k}": recall_at_k(gt_ids, scored_epoch_ids, k) for k in ks}
    precision = {f"precision@{k}": precision_at_k(gt_ids, scored_epoch_ids, k) for k in ks}
    ndcg = {f"ndcg@{k}": ndcg_at_k(gains, scored_epoch_ids, k) for k in ks}

    usage = usage_cost(Path(args.usage_log)) if args.usage_log and Path(args.usage_log).exists() else None
    cost_usd = args.cost_usd if args.cost_usd else (usage or {}).get("cost_usd", 0.0)

    missed = [
        {"person_id": pid, "name": gt_name.get(pid), "gt_rank": gt_rank[pid]}
        for pid in sorted(gt_ids - epoch_set, key=lambda p: gt_rank[p])
    ]
    net_new = [pid for pid in epoch_ids if pid not in gt_ids]
    overall_recall = round(len(gt_ids & epoch_set) / len(gt_ids), 4) if gt_ids else 0.0

    gaps = {
        "primitive": "score_ground_truth_gaps",
        "epoch": args.epoch_label,
        "ground_truth_size": len(gt_ids),
        "epoch_candidate_count": len(epoch_ids),
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

    epoch_dir = Path(args.epoch_dir)
    epoch_dir.mkdir(parents=True, exist_ok=True)
    (epoch_dir / "gaps.json").write_text(json.dumps(gaps, indent=2) + "\n", encoding="utf-8")

    # append convergence row
    conv = Path(args.convergence_csv)
    conv.parent.mkdir(parents=True, exist_ok=True)
    fields = ["epoch", "gt_size", "epoch_n", "overall_recall", *recall.keys(), *precision.keys(), *ndcg.keys(), "missed", "net_new", "cost_usd"]
    row = {
        "epoch": args.epoch_label, "gt_size": len(gt_ids), "epoch_n": len(epoch_ids),
        "overall_recall": overall_recall, **recall, **precision, **ndcg,
        "missed": len(missed), "net_new": len(net_new), "cost_usd": cost_usd,
    }
    existing = []
    if conv.exists():
        existing = [r for r in csv.DictReader(conv.open()) if r.get("epoch") != args.epoch_label]
    with conv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in fields})
        w.writerow(row)

    print(json.dumps({k: gaps[k] for k in ("epoch", "ground_truth_size", "overall_recall", *recall.keys(), *precision.keys(), *ndcg.keys(), "missed_count", "net_new_count", "cost_usd")}, indent=2))


if __name__ == "__main__":
    main()
