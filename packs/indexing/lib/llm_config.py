"""Centralized LLM configuration for indexing enrichment stages.

All enrichment primitives (roles, companies) should use these defaults
so model, reasoning, and pricing stay consistent with prod.
"""
from __future__ import annotations

import os
from typing import Any

# Default model — matches prod combined_enrichment.py.
DEFAULT_MODEL = "gpt-5.2"
DEFAULT_MAX_COMPLETION_TOKENS = 2500
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60
DEFAULT_OPENAI_CONCURRENCY = 64

# Known pricing per 1K tokens (USD).
CHAT_MODEL_PRICES_PER_1K_USD: dict[str, dict[str, float]] = {
    "gpt-5.2": {"input": 0.00175, "output": 0.01400},
    "gpt-5.2-chat-latest": {"input": 0.00175, "output": 0.01400},
    "gpt-5.1": {"input": 0.00125, "output": 0.01000},
    "gpt-5.1-chat-latest": {"input": 0.00125, "output": 0.01000},
    "gpt-5": {"input": 0.00125, "output": 0.01000},
    "gpt-5-chat-latest": {"input": 0.00125, "output": 0.01000},
    "gpt-5-mini": {"input": 0.00025, "output": 0.00200},
    "gpt-5-nano": {"input": 0.00005, "output": 0.00040},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "gpt-4o-mini-2024-07-18": {"input": 0.00015, "output": 0.00060},
}


def is_reasoning_model(model: str) -> bool:
    """Return True if *model* is a reasoning model (gpt-5.x, o1, o3)."""
    return any(x in model.lower() for x in ["o1", "o3", "gpt-5", "5.1", "5.2"])


def api_call_kwargs(model: str) -> dict[str, Any]:
    """Return the non-message kwargs for ``client.chat.completions.create``.

    For reasoning models: no temperature, reasoning effort low, flex tier.
    For non-reasoning: temperature=0.
    """
    kwargs: dict[str, Any] = {
        "max_completion_tokens": int(
            os.getenv("POWERPACKS_LLM_MAX_COMPLETION_TOKENS", str(DEFAULT_MAX_COMPLETION_TOKENS))
        ),
    }
    if is_reasoning_model(model):
        kwargs["reasoning"] = {"effort": "low"}
        kwargs["service_tier"] = "flex"
    else:
        kwargs["temperature"] = 0
    return kwargs
