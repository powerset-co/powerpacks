"""Centralized LLM configuration for indexing enrichment stages.

All enrichment primitives (roles, companies) should use these defaults
so model, reasoning, and pricing stay consistent with prod.

Concurrency and checkpoint sizes come from the OpenAI usage-tier
profile in ``openai_usage_tiers.py`` (default: tier_5 = 256 concurrency).
"""
from __future__ import annotations

import os
from typing import Any

from packs.indexing.lib.openai_usage_tiers import (
    env_or_profile_int,
    openai_usage_tier_profile,
)

# Default model for company enrichment — matches prod combined_enrichment.py.
DEFAULT_MODEL = "gpt-5.2"
# Role enrichment model — gpt-5.2 has structured output bugs with the
# 180-item role_ids enum (picks random values like investment_banker for
# Instructor). gpt-5.1 classifies correctly.
DEFAULT_ROLE_MODEL = "gpt-5.1"
# Prod parity (combined_enrichment.py): reasoning calls cap at 2000.
DEFAULT_MAX_COMPLETION_TOKENS = 2000
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60
# "flex" bills gpt-5.x chat tokens at ~50% but is a server-side queue: calls
# routinely take minutes regardless of client concurrency. Interactive paths
# (Modal onboarding) set POWERPACKS_OPENAI_SERVICE_TIER=default for speed.
DEFAULT_SERVICE_TIER = "flex"

# Pull concurrency from tier profile (tier_5 default = 256).
_profile = openai_usage_tier_profile()
DEFAULT_OPENAI_CONCURRENCY = int(_profile.get("openai_concurrency", 256))
DEFAULT_CHECKPOINT_EVERY = int(_profile.get("paid_checkpoint_every", 512))

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


def openai_service_tier() -> str:
    """Service tier sent on reasoning-model calls (``flex`` or ``default``)."""
    tier = os.getenv("POWERPACKS_OPENAI_SERVICE_TIER", DEFAULT_SERVICE_TIER).strip().lower()
    return tier or DEFAULT_SERVICE_TIER


def openai_price_multiplier() -> float:
    """Multiplier on CHAT_MODEL_PRICES (standard-tier) for the active tier."""
    return 0.5 if openai_service_tier() == "flex" else 1.0


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
        kwargs["reasoning_effort"] = "low"
        kwargs["service_tier"] = openai_service_tier()
    else:
        kwargs["temperature"] = 0
    return kwargs
