"""Score one harness epoch against the ground-truth set, and track convergence.

Ground truth = the trusted gold set built by the full agentic + mixture-of-judges method
(see packs/search/docs/agentic-search.md). Each cheaper/tuned harness run is an "epoch"; this
primitive measures the gap so successive epochs can converge toward ground truth.

Inputs:
  --ground-truth  ground_truth_ranked.json (list of {person_id, name, mean_score, ...})
  --epoch-candidates  the epoch's candidates as JSONL or JSON list, each with person_id and
                      (optionally) a rank/score; order is taken as rank if no rank field.
Outputs (under the epoch dir):
  gaps.json   recall@k, precision@k, missed GT ids (+ their GT rank), net-new finds
And appends one row to convergence.csv (created if absent).

No network, no spend — pure set math.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


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


def recall_at_k(gt_ids: set[str], epoch_ids: list[str], k: int) -> float:
    if not gt_ids:
        return 0.0
    topk = set(epoch_ids[:k])
    return round(len(gt_ids & topk) / len(gt_ids), 4)


def precision_at_k(gt_ids: set[str], epoch_ids: list[str], k: int) -> float:
    if not epoch_ids:
        return 0.0
    topk = epoch_ids[:k]
    return round(sum(1 for pid in topk if pid in gt_ids) / min(k, len(topk)), 4)


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a harness epoch vs ground truth + track convergence.")
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--epoch-candidates", required=True)
    ap.add_argument("--epoch-dir", required=True, help="Per-epoch output dir (gaps.json written here)")
    ap.add_argument("--epoch-label", required=True, help="e.g. epoch-01 (row key in convergence.csv)")
    ap.add_argument("--convergence-csv", required=True, help="Appended one row per epoch")
    ap.add_argument("--ks", default="10,25,50", help="Comma-separated K values for recall/precision")
    ap.add_argument("--cost-usd", type=float, default=0.0, help="Optional: epoch spend, for the convergence row")
    args = ap.parse_args()

    gt = load_records(Path(args.ground_truth))
    gt_rank = {r["person_id"]: i + 1 for i, r in enumerate(gt)}
    gt_name = {r["person_id"]: r.get("name") for r in gt}
    gt_ids = set(gt_rank)

    epoch = load_records(Path(args.epoch_candidates))
    epoch_ids = ranked_ids(epoch)
    epoch_set = set(epoch_ids)

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    recall = {f"recall@{k}": recall_at_k(gt_ids, epoch_ids, k) for k in ks}
    precision = {f"precision@{k}": precision_at_k(gt_ids, epoch_ids, k) for k in ks}

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
        "missed_count": len(missed),
        "missed": missed,
        "net_new_count": len(net_new),
        "cost_usd": args.cost_usd,
    }

    epoch_dir = Path(args.epoch_dir)
    epoch_dir.mkdir(parents=True, exist_ok=True)
    (epoch_dir / "gaps.json").write_text(json.dumps(gaps, indent=2) + "\n", encoding="utf-8")

    # append convergence row
    conv = Path(args.convergence_csv)
    conv.parent.mkdir(parents=True, exist_ok=True)
    fields = ["epoch", "gt_size", "epoch_n", "overall_recall", *recall.keys(), *precision.keys(), "missed", "net_new", "cost_usd"]
    row = {
        "epoch": args.epoch_label, "gt_size": len(gt_ids), "epoch_n": len(epoch_ids),
        "overall_recall": overall_recall, **recall, **precision,
        "missed": len(missed), "net_new": len(net_new), "cost_usd": args.cost_usd,
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

    print(json.dumps({k: gaps[k] for k in ("epoch", "ground_truth_size", "overall_recall", *recall.keys(), *precision.keys(), "missed_count", "net_new_count")}, indent=2))


if __name__ == "__main__":
    main()
