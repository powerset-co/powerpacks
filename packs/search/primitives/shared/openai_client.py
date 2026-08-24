"""Shared OpenAI client construction for search primitives.

One place that interprets OPENAI_API_BASE, so every primitive resolves the same
base URL whether or not the configured value carries the /v1 suffix the SDK
needs. Same normalization as typed search model stages'
openai_base_url; without it, a custom OPENAI_API_BASE like
"https://proxy.example.com" works in the older primitives but 404s in any
primitive that passes the raw value through.

Usage capture is ALWAYS ON — it all sits local. Both factories return clients
whose chat/embeddings/responses create() calls append one JSONL row per response
carrying a usage block: {ts, model, stage, prompt_tokens, completion_tokens,
reasoning_tokens, latency_ms}. Rows land in .powerpacks/usage/usage.jsonl unless
POWERPACKS_USAGE_LOG points somewhere else (the deep loop and the fast pipeline
point it into their run dirs for per-run cost attribution). The stage tag comes
from POWERPACKS_USAGE_STAGE. completion_tokens EXCLUDES reasoning tokens (they
are broken out into their own field) so downstream pricing never double-counts.
Capture is fail-open by default. POWERPACKS_USAGE_REQUIRED=1 makes missing usage
or a logging error fail closed for spend-bounded callers. Nothing here uploads anything — sharing usage is the $reflect skill's
explicit opt-in, elsewhere.

Changelog:
  2026-07-30  usage-capture hooks + make_async_openai_client; capture always on.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

import openai

DEFAULT_API_BASE = "https://api.openai.com"
HOOKED_METHODS = ("chat.completions.create", "embeddings.create", "responses.create")
DEFAULT_USAGE_LOG = Path(__file__).resolve().parents[4] / ".powerpacks" / "usage" / "usage.jsonl"


def openai_base_url(api_base: str | None = None) -> str:
    """Resolve explicit arg > OPENAI_API_BASE env > default, always /v1-suffixed."""
    base = (api_base or os.environ.get("OPENAI_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _usage_row(requested_model: Any, resp: Any, latency_ms: int) -> dict[str, Any] | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    prompt = int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0) or 0)
    details = getattr(usage, "completion_tokens_details", None) or getattr(usage, "output_tokens_details", None)
    reasoning = int(getattr(details, "reasoning_tokens", 0) or 0) if details is not None else 0
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": str(getattr(resp, "model", None) or requested_model or ""),
        "stage": os.environ.get("POWERPACKS_USAGE_STAGE", "unknown"),
        "prompt_tokens": prompt,
        "completion_tokens": max(0, completion - reasoning),
        "reasoning_tokens": reasoning,
        "latency_ms": latency_ms,
    }
    tier = getattr(resp, "service_tier", None)
    if tier and tier != "default":
        row["service_tier"] = str(tier)  # flex is billed at half rate; pricing needs to know
    return row


class UsageCaptureError(RuntimeError):
    """A caller-required provider usage record could not be captured."""


def _usage_required() -> bool:
    return os.environ.get("POWERPACKS_USAGE_REQUIRED") == "1"


def _append_row(log_path: str, row: dict[str, Any]) -> None:
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        if _usage_required():
            raise UsageCaptureError(f"provider usage log append failed: {exc}") from exc


def _resolve_method(client: Any, dotted: str) -> tuple[Any, str] | None:
    parent = client
    parts = dotted.split(".")
    for name in parts[:-1]:
        parent = getattr(parent, name, None)
        if parent is None:
            return None
    if not callable(getattr(parent, parts[-1], None)):
        return None
    return parent, parts[-1]


def _instrument(client: Any, *, is_async: bool) -> Any:
    log_path = os.environ.get("POWERPACKS_USAGE_LOG") or str(DEFAULT_USAGE_LOG)
    for dotted in HOOKED_METHODS:
        resolved = _resolve_method(client, dotted)
        if resolved is None:
            continue
        parent, attr = resolved
        method = getattr(parent, attr)
        if is_async:
            @wraps(method)
            async def hooked(*args: Any, _method=method, **kwargs: Any) -> Any:
                t0 = time.monotonic()
                resp = await _method(*args, **kwargs)
                row = _usage_row(kwargs.get("model"), resp, int((time.monotonic() - t0) * 1000))
                if row is None and _usage_required():
                    raise UsageCaptureError("provider response omitted required usage")
                if row is not None:
                    _append_row(log_path, row)
                return resp
        else:
            @wraps(method)
            def hooked(*args: Any, _method=method, **kwargs: Any) -> Any:
                t0 = time.monotonic()
                resp = _method(*args, **kwargs)
                row = _usage_row(kwargs.get("model"), resp, int((time.monotonic() - t0) * 1000))
                if row is None and _usage_required():
                    raise UsageCaptureError("provider response omitted required usage")
                if row is not None:
                    _append_row(log_path, row)
                return resp
        setattr(parent, attr, hooked)
    return client


def _client_kwargs(api_key: str | None, api_base: str | None, timeout: float | None,
                   max_retries: int | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"base_url": openai_base_url(api_base)}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return kwargs


def make_openai_client(api_key: str | None = None, api_base: str | None = None,
                       timeout: float | None = None, max_retries: int | None = None) -> openai.OpenAI:
    return _instrument(openai.OpenAI(**_client_kwargs(api_key, api_base, timeout, max_retries)), is_async=False)


def make_async_openai_client(api_key: str | None = None, api_base: str | None = None,
                             timeout: float | None = None, max_retries: int | None = None) -> openai.AsyncOpenAI:
    return _instrument(openai.AsyncOpenAI(**_client_kwargs(api_key, api_base, timeout, max_retries)), is_async=True)
