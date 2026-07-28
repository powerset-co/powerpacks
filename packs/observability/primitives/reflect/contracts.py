"""Closed enums, numeric buckets, and fail-closed export validation."""

from __future__ import annotations

import re
from typing import Any, Iterable

SCHEMA_VERSION = 1
WORKFLOWS = ("setup", "import-gmail", "import-messages", "deep-context")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra", "unknown")
ROLES = ("primary", "reviewer", "subagent", "unknown")
PROVIDERS = ("openai", "anthropic", "google", "other", "unknown")
HARNESSES = ("codex", "claude-code", "nanoclaw", "pi", "other", "unknown")
INTERVENTIONS = (
    "none",
    "expected_approval",
    "oauth",
    "os_permission",
    "user_correction",
    "retry",
    "manual_recovery",
)

MODEL_RE = re.compile(
    r"^(?:"
    r"gpt-[A-Za-z0-9._:-]+|"
    r"o[134](?:-[A-Za-z0-9._:-]+)?|"
    r"codex(?:-[A-Za-z0-9._:-]+)?|"
    r"(?:claude|gemini|llama|mistral|qwen|grok|deepseek)-[A-Za-z0-9._:-]+"
    r")$",
    re.IGNORECASE,
)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
OPAQUE_RECEIPT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
def closed_enum(value: str | None, allowed: Iterable[str], default: str = "unknown") -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "claude": "claude-code",
        "claude code": "claude-code",
        "nano-claw": "nanoclaw",
        "x-high": "xhigh",
        "extra-high": "xhigh",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in allowed else default


def normalized_model(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if MODEL_RE.fullmatch(candidate) else "unknown"


def normalized_provider(value: str | None, model: str) -> str:
    explicit = closed_enum(value, PROVIDERS)
    if explicit != "unknown":
        return explicit
    lowered = model.lower()
    if lowered.startswith(("gpt-", "o1", "o3", "o4", "codex")):
        return "openai"
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "google"
    return "unknown"


def normalized_intervention(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in INTERVENTIONS else "none"


def nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def count_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "0"
    if value <= 10:
        return "1-10"
    if value <= 100:
        return "11-100"
    if value <= 1_000:
        return "101-1k"
    if value <= 10_000:
        return "1k-10k"
    return "10k+"


def duration_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 1:
        return "<1s"
    if seconds < 10:
        return "1-10s"
    if seconds < 60:
        return "10-60s"
    if seconds < 300:
        return "1-5m"
    if seconds < 1_800:
        return "5-30m"
    if seconds < 7_200:
        return "30m-2h"
    return "2h+"


def token_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "0"
    if value <= 1_000:
        return "1-1k"
    if value <= 10_000:
        return "1k-10k"
    if value <= 100_000:
        return "10k-100k"
    if value <= 1_000_000:
        return "100k-1m"
    return "1m+"


def cost_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "$0"
    if value < 0.01:
        return "<$0.01"
    if value < 0.10:
        return "$0.01-0.10"
    if value < 1:
        return "$0.10-1"
    if value < 10:
        return "$1-10"
    return "$10+"


def key_count(value: Any) -> int:
    if isinstance(value, dict):
        return len(value) + sum(key_count(child) for child in value.values())
    if isinstance(value, list):
        return sum(key_count(child) for child in value)
    return 0
