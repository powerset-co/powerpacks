"""Run-artifact IO shared by every harness module.

Owns the repo/shared import bootstrap the rest of the harness relies on, so a
module that needs ``openai_client``, ``search_common``, ``usage_pricing``, or a
``packs.*`` import imports from here first. Also owns JSON read/write,
artifact-path resolution, the last-JSON-object reader for primitive stdout, and
the usage-log pricing/cost helpers.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[5]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
LIB_DIR = Path(__file__).resolve().parents[2] / "lib"
for shared_path in (SHARED_DIR, LIB_DIR):
    if str(shared_path) not in sys.path:
        sys.path.insert(0, str(shared_path))
from usage_pricing import load_prices, row_cost_usd  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_artifact_path(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _last_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    values: list[dict[str, Any]] = []
    offset = 0
    while offset < len(text):
        try:
            value, offset = decoder.raw_decode(text, offset)
            if isinstance(value, dict):
                values.append(value)
        except json.JSONDecodeError:
            offset += 1
    if not values:
        raise ValueError("search primitive returned no JSON result")
    return values[-1]


def _usage_cost(path: Path) -> float:
    if not path.is_file():
        return 0.0
    return round(sum(float(json.loads(line).get("cost_usd") or 0)
                     for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), 6)


def _price_usage_log(path: Path) -> None:
    if not path.is_file():
        return
    prices = load_prices()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        cost = row_cost_usd(row, prices)
        if cost is not None:
            row["cost_usd"] = cost
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _response_usage(response: Any) -> dict[str, Any]:
    usage = response.usage
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "model": str(getattr(response, "model", "")),
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "cached_tokens": int(getattr(prompt_details, "cached_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(completion_details, "reasoning_tokens", 0) or 0),
        "service_tier": str(getattr(response, "service_tier", "") or ""),
    }
