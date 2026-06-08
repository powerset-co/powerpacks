"""Local token accounting helpers for LLM prompt fan-out primitives."""

from __future__ import annotations

from typing import Iterable

import tiktoken


def encoding_name_for_model(model: str) -> str:
    try:
        return tiktoken.encoding_for_model(model).name
    except KeyError:
        return "o200k_base"


def encoding_for_model(model: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(encoding_name_for_model(model))


def count_chat_prompt_tokens(model: str, messages: list[dict[str, str]]) -> int:
    """Return a local estimate of chat prompt tokens sent to OpenAI.

    OpenAI rate limits are applied to server-side token accounting, but this
    gives a deterministic close estimate for throughput/headroom tracking.
    """
    encoding = encoding_for_model(model)
    tokens = 3
    for message in messages:
        tokens += 3
        tokens += len(encoding.encode(message.get("role", "")))
        tokens += len(encoding.encode(message.get("content", "")))
        if message.get("name"):
            tokens += 1 + len(encoding.encode(message["name"]))
    return tokens


def summarize_token_counts(
    counts: Iterable[int],
    *,
    model: str,
    elapsed_ms: int | None = None,
) -> dict[str, int | float | str]:
    values = sorted(int(count) for count in counts)
    total = sum(values)
    out: dict[str, int | float | str] = {
        "estimator": "tiktoken_chat_prompt",
        "model": model,
        "encoding": encoding_name_for_model(model),
        "request_count": len(values),
        "prompt_tokens_total": total,
        "prompt_tokens_min": values[0] if values else 0,
        "prompt_tokens_avg": round(total / len(values), 2) if values else 0,
        "prompt_tokens_p50": percentile(values, 0.50),
        "prompt_tokens_p95": percentile(values, 0.95),
        "prompt_tokens_max": values[-1] if values else 0,
    }
    if elapsed_ms and elapsed_ms > 0:
        out["elapsed_ms"] = elapsed_ms
        out["prompt_tokens_per_minute"] = round(total / (elapsed_ms / 60000), 2)
    return out


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, int(len(values) * q))
    return values[index]
