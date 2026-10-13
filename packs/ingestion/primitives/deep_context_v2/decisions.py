"""OpenAI's Decisions API: typed questions answered by gpt-6-luna as numbers, never text.

A predicate comes back as a probability, a choice as the chosen value with every value's
probability and a confidence, a score as the probability-weighted level (levels count from 0).
Only input tokens are billed: $0.10 per million.

`decide_all` answers requests keyed by `request_digest`, which covers the caller's question
version as well as the request, so a version bump asks again instead of reusing an answer to
other questions. Every answer is cached on disk as it arrives: a request is billed once, and a
stopped run keeps what it paid for.

Created: 2026-10-13
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import tiktoken
from openai import AsyncOpenAI

from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context_v2.openai import DEFAULT_OPENAI_CONCURRENCY, load_env
from packs.search.primitives.shared.openai_client import append_usage_row

MODEL = "gpt-6-luna"
INPUT_PRICE_PER_MILLION = 0.10
TIMEOUT_SECONDS = 120
MAX_RETRIES = 3  # the SDK retries transient statuses and honours Retry-After

Answer = dict[str, Any]  # one entry of the response's `answers`, as OpenAI sends it

_ENCODER = tiktoken.get_encoding("o200k_base")


def request_digest(version: str, request: dict[str, Any]) -> str:
    """The cache key: the question version and the exact request."""
    payload = json.dumps({"version": version, "request": request}, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_path(cache_dir: Path, digest: str) -> Path:
    return cache_dir / f"{digest}.json"


def input_tokens(request: dict[str, Any]) -> int:
    """The request's size as OpenAI's tokenizer counts it: the estimate's basis."""
    return len(_ENCODER.encode(json.dumps(request, ensure_ascii=False)))


async def decide_all(requests: dict[str, dict[str, Any]], cache_dir: Path, *, cache_only: bool, stage: str,
                     on_progress: Callable[[int, int], None]) -> dict[str, list[Answer]]:
    """Every request's answers by digest, cached ones first. `cache_only` never calls OpenAI: an
    answer that is not cached stops the run before any write. `on_progress(done, total)` follows
    the requests sent."""
    answers: dict[str, list[Answer]] = {}
    missing: dict[str, dict[str, Any]] = {}
    for digest, request in requests.items():
        path = cache_path(cache_dir, digest)
        if path.exists():
            answers[digest] = json.loads(path.read_text(encoding="utf-8"))["answers"]
        else:
            missing[digest] = request
    if not missing:
        return answers
    if cache_only:
        raise ValueError(f"{len(missing)} answers are not saved yet. Check the cost again.")
    load_env()
    cache_dir.mkdir(parents=True, exist_ok=True)
    slots: int = env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
                                    fallback=DEFAULT_OPENAI_CONCURRENCY)
    semaphore = asyncio.Semaphore(slots)
    answered: list[str] = []
    on_progress(0, len(missing))
    async with AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL") or None,
                           timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES) as client:

        async def ask(digest: str, request: dict[str, Any]) -> None:
            async with semaphore:
                started = time.monotonic()
                response: dict[str, Any] = await client.post("/decisions", cast_to=dict[str, Any], body=request)
                latency_ms = round((time.monotonic() - started) * 1000)
            path = cache_path(cache_dir, digest)
            partial = path.with_suffix(".partial")
            partial.write_text(json.dumps({"answers": response["answers"], "usage": response.get("usage")},
                                          ensure_ascii=False), encoding="utf-8")
            partial.replace(path)
            answers[digest] = response["answers"]
            _log_usage(request, response, stage, latency_ms)
            answered.append(digest)
            on_progress(len(answered), len(missing))

        await asyncio.gather(*(ask(digest, request) for digest, request in missing.items()))
    return answers


def _log_usage(request: dict[str, Any], response: dict[str, Any], stage: str, latency_ms: int) -> None:
    """One usage row per paid call: OpenAI's own input count when the response carries one, else ours."""
    usage = response.get("usage") or {}
    reported = usage.get("input_tokens")
    tokens: int = reported if isinstance(reported, int) else input_tokens(request)
    append_usage_row({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": MODEL,
        "stage": stage,
        "prompt_tokens": tokens,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "latency_ms": latency_ms,
        "cost_usd": tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
        "cost_basis": "reported_input_tokens" if isinstance(reported, int) else "tiktoken_estimate",
    })
