"""Powerpacks OpenAI throughput profiles keyed by OpenAI usage tier.

OpenAI's public usage-tier docs define paid-spend qualification and monthly
usage budget. Per-model RPM/TPM limits are org/model-specific in the OpenAI
limits dashboard, so the TPM values here are Powerpacks operating budgets, not
official universal OpenAI limits.
"""

from __future__ import annotations

import os
from typing import Any


DEFAULT_OPENAI_USAGE_TIER = "tier_5"


OPENAI_USAGE_TIER_PROFILES: dict[str, dict[str, Any]] = {
    "tier_1": {
        "label": "Tier 1",
        "documented_qualification": "$5 paid",
        "documented_monthly_usage_limit_usd": 100,
        "powerpacks_tpm_budget": 1_000_000,
        "openai_concurrency": 16,
        "paid_checkpoint_every": 16,
        "embedding_concurrency": 2,
    },
    "tier_2": {
        "label": "Tier 2",
        "documented_qualification": "$50 paid",
        "documented_monthly_usage_limit_usd": 500,
        "powerpacks_tpm_budget": 2_000_000,
        "openai_concurrency": 32,
        "paid_checkpoint_every": 32,
        "embedding_concurrency": 4,
    },
    "tier_3": {
        "label": "Tier 3",
        "documented_qualification": "$100 paid",
        "documented_monthly_usage_limit_usd": 1_000,
        "powerpacks_tpm_budget": 5_000_000,
        "openai_concurrency": 64,
        "paid_checkpoint_every": 64,
        "embedding_concurrency": 4,
    },
    "tier_4": {
        "label": "Tier 4",
        "documented_qualification": "$250 paid",
        "documented_monthly_usage_limit_usd": 5_000,
        "powerpacks_tpm_budget": 8_000_000,
        "openai_concurrency": 96,
        "paid_checkpoint_every": 96,
        "embedding_concurrency": 6,
    },
    "tier_5": {
        "label": "Tier 5",
        "documented_qualification": "$1,000 paid",
        "documented_monthly_usage_limit_usd": 200_000,
        "powerpacks_tpm_budget": 10_000_000,
        "openai_concurrency": 256,
        "paid_checkpoint_every": 512,
        "embedding_concurrency": 8,
    },
}


def normalize_openai_usage_tier(value: str | None = None) -> str:
    raw = (value or os.getenv("POWERPACKS_OPENAI_USAGE_TIER") or DEFAULT_OPENAI_USAGE_TIER).strip().lower()
    tier = raw.replace("-", "_").replace(" ", "_")
    if tier and tier[0].isdigit():
        tier = f"tier_{tier}"
    return tier if tier in OPENAI_USAGE_TIER_PROFILES else DEFAULT_OPENAI_USAGE_TIER


def openai_usage_tier_choices() -> list[str]:
    return sorted(OPENAI_USAGE_TIER_PROFILES)


def openai_usage_tier_profile(value: str | None = None) -> dict[str, Any]:
    tier = normalize_openai_usage_tier(value)
    return {"tier": tier, **OPENAI_USAGE_TIER_PROFILES[tier]}


def env_or_profile_int(env_name: str, profile_key: str, tier: str | None = None, fallback: int = 1) -> int:
    explicit = os.getenv(env_name)
    if explicit not in (None, ""):
        return max(1, int(explicit))
    profile = openai_usage_tier_profile(tier)
    return max(1, int(profile.get(profile_key) or fallback))


def profile_paid_checkpoint_every(configured: int, tier: str | None = None) -> int:
    limit = env_or_profile_int("POWERPACKS_PAID_CHECKPOINT_EVERY", "paid_checkpoint_every", tier=tier, fallback=512)
    return max(1, min(max(1, int(configured or 1000)), limit))
