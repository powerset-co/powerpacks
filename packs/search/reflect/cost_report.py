"""Cost report over one usage.jsonl: per-stage and per-model tokens, calls, and USD.

Reads the JSONL the shared OpenAI client appends when POWERPACKS_USAGE_LOG is set
(one row per call: model, stage, prompt/completion/reasoning tokens, latency_ms) and
prices it with the committed packs/search/data/model-prices.json. Models with a null
price entry are reported tokens-only and flip fully_priced to false — the report
never invents a dollar figure. Pure local file math; no network, no spend.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

LIB_DIR = Path(__file__).resolve().parents[1] / "primitives" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
from usage_pricing import PRICES_PATH as DEFAULT_PRICES_PATH, load_prices, row_cost_usd  # noqa: E402

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens")


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def build_report(rows: list[dict[str, Any]], prices: dict[str, Any]) -> dict[str, Any]:
    def bucket() -> dict[str, Any]:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                "cost_usd": 0.0, "fully_priced": True}

    totals = bucket()
    by_stage: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    latency_total_ms = 0

    for row in rows:
        cost = row_cost_usd(row, prices)
        latency_total_ms += int(row.get("latency_ms") or 0)
        for group in (totals, by_stage.setdefault(row.get("stage") or "unknown", bucket()),
                      by_model.setdefault(row.get("model") or "unknown", bucket())):
            group["calls"] += 1
            for field in TOKEN_FIELDS:
                group[field] += int(row.get(field) or 0)
            if cost is None:
                group["fully_priced"] = False
            else:
                group["cost_usd"] = round(group["cost_usd"] + cost, 6)

    return {
        "primitive": "cost_report",
        "rows": len(rows),
        "latency_total_ms": latency_total_ms,
        "totals": totals,
        "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1]["cost_usd"])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_usd"])),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-stage/per-model token + USD report over a usage.jsonl.")
    ap.add_argument("--usage-log", required=True)
    ap.add_argument("--prices", default=None, help="Override the committed model-prices.json")
    ap.add_argument("--out", default=None, help="Also write the report JSON here")
    args = ap.parse_args()

    usage_path = Path(args.usage_log)
    if not usage_path.exists():
        print(json.dumps({"status": "failed", "error": f"usage log not found: {usage_path}"}))
        raise SystemExit(2)

    prices = load_prices(Path(args.prices) if args.prices else DEFAULT_PRICES_PATH)
    report = build_report(load_rows(usage_path), prices)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
