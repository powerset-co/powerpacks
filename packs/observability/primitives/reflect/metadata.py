"""Controlled product, runtime, model, effort, usage, and OS metadata."""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Any

from .contracts import (
    EFFORTS,
    HARNESSES,
    ROLES,
    SEMVER_RE,
    closed_enum,
    cost_bucket,
    count_bucket,
    normalized_model,
    normalized_provider,
    token_bucket,
)
from .manifests import first_number, first_string


def product_metadata(root: Path) -> dict[str, str]:
    version = "unknown"
    try:
        import tomllib

        value = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        candidate = str(value.get("project", {}).get("version", ""))
        if SEMVER_RE.fullmatch(candidate):
            version = candidate
    except (OSError, ValueError):
        pass
    channel = closed_enum(os.environ.get("POWERPACKS_CHANNEL"), ("stable", "rc", "edge"))
    if channel == "unknown":
        try:
            stamp = json.loads((root / ".powerpacks-install.json").read_text(encoding="utf-8"))
            channel = closed_enum(stamp.get("channel"), ("stable", "rc", "edge"))
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return {"version": version, "channel": channel}


def runtime_metadata(
    *,
    harness: str,
    model: str,
    provider: str,
    effort: str,
    role: str,
    fallback: bool,
    stages: list[dict[str, Any]],
    raw_manifests: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_model = "unknown"
    manifest_effort = "unknown"
    total_tokens: float | None = None
    total_cost: float | None = None
    calls: float | None = None
    for payload in raw_manifests:
        if manifest_model == "unknown":
            manifest_model = normalized_model(first_string(payload, ("model",)))
        if manifest_effort == "unknown":
            manifest_effort = closed_enum(
                first_string(payload, ("reasoning_effort", "effort")), EFFORTS
            )
        token_value = first_number(payload, ("total_tokens",))
        if token_value is None:
            token_parts = [
                first_number(payload, (key,))
                for key in ("input_tokens", "output_tokens", "reasoning_tokens")
            ]
            known_parts = [part for part in token_parts if part is not None]
            token_value = sum(known_parts) if known_parts else None
        cost_value = first_number(
            payload, ("cost_usd", "total_cost_usd", "estimated_cost_usd")
        )
        call_value = first_number(payload, ("llm_calls", "api_calls", "call_count"))
        total_tokens = max(total_tokens or 0, token_value) if token_value is not None else total_tokens
        total_cost = max(total_cost or 0, cost_value) if cost_value is not None else total_cost
        calls = max(calls or 0, call_value) if call_value is not None else calls
    selected_model = normalized_model(model)
    if selected_model == "unknown":
        selected_model = manifest_model
    selected_effort = closed_enum(effort, EFFORTS)
    if selected_effort == "unknown":
        selected_effort = manifest_effort
    return {
        "harness": closed_enum(harness, HARNESSES),
        "provider": normalized_provider(provider, selected_model),
        "model": selected_model,
        "effort": selected_effort,
        "role": closed_enum(role, ROLES),
        "fallback_or_reroute": bool(fallback),
        "token_bucket": token_bucket(total_tokens),
        "cost_bucket": cost_bucket(total_cost),
        "call_count_bucket": count_bucket(calls),
        "latency_buckets": [
            {"stage": stage["stage"], "bucket": stage["duration_bucket"]}
            for stage in stages
            if stage["duration_bucket"] != "unknown"
        ],
    }


def os_metadata() -> dict[str, str]:
    family = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(
        platform.system().lower(), "other"
    )
    release = platform.mac_ver()[0] if family == "macos" else platform.release()
    match = re.match(r"^(\d+)", release)
    return {"family": family, "major": match.group(1) if match else "unknown"}
