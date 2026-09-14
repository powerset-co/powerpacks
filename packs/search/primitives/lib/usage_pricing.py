"""Single home for the committed model price table and usage-row cost math.

A usage row is one LLM call as captured by the shared client's POWERPACKS_USAGE_LOG
hook: {model, stage, prompt_tokens, cached_tokens, completion_tokens,
reasoning_tokens, latency_ms}.
Prices live in packs/search/data/model-prices.json as USD per 1M tokens; a null table
for a model means "unpriced" and cost math reports tokens-only for it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PRICES_PATH = Path(__file__).resolve().parents[2] / "data" / "model-prices.json"


def load_prices(path: Path | None = None) -> dict[str, Any]:
    target = path or PRICES_PATH
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def price_table_for(model: str, prices: dict[str, Any]) -> Any:
    """Exact match, else longest '-'-boundary prefix — the API reports dated ids
    (gpt-4.1-2025-04-14) while the committed table keys bare aliases (gpt-4.1);
    longest-prefix keeps gpt-4.1-mini-* from matching the gpt-4.1 row."""
    if model in prices:
        return prices[model]
    for key in sorted(prices, key=len, reverse=True):
        if model.startswith(key + "-"):
            return prices[key]
    return None


def row_cost_usd(row: dict[str, Any], prices: dict[str, Any]) -> float | None:
    """USD for one usage row, or None when the model has no usable price table."""
    table = price_table_for(str(row.get("model") or ""), prices)
    if not isinstance(table, dict):
        return None
    input_price = float(table.get("input_per_1m") or 0.0)
    cached_input_price = table.get("cached_input_per_1m")
    cached_input_price = input_price if cached_input_price is None else float(cached_input_price)
    prompt_tokens = int(row.get("prompt_tokens") or 0)
    cached_tokens = int(row.get("cached_tokens") or 0)
    output_price = float(table.get("output_per_1m") or 0.0)
    cost = ((prompt_tokens - cached_tokens) / 1e6) * input_price
    cost += (cached_tokens / 1e6) * cached_input_price
    cost += (int(row.get("completion_tokens") or 0) / 1e6) * output_price
    cost += (int(row.get("reasoning_tokens") or 0) / 1e6) * float(table.get("reasoning_per_1m") or output_price)
    if row.get("service_tier") == "flex":
        cost *= 0.5  # OpenAI flex tier bills all tokens at half the standard rate
    return cost
