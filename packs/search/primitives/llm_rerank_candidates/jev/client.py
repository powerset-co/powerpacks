"""Async TypeSafe client for Jev question answering.

`answer_requests` is the question-agnostic half: sha-keyed requests in, validated
answers out, reusing the exact-request cache under `<output_dir>/jev/`.
`score_candidates` is the JD capability judge built on it — it binds the JD
question set, then turns the answers into a qualification score.

Changelog:
  2026-09-25: profile-only request. Request compaction is gone with the per-position
    questions (the request no longer duplicates position bodies), cache records no longer
    carry an effective-request kind, and features build from answers alone. Cache validation
    no longer checks the capability judge's prompt version, so deep-context's jev_worth
    records survive this bump.
  2026-09-24: made the cache path reusable by request callers.
  2026-09-24 (share stage): the cache/semaphore/request/checkpoint body of
    `score_candidates`' inner `score_one` became the module-level
    `answer_requests`, so a second question set (the share stage's labels) can
    reuse it. `_cache_record`/`_validate_cache` take the caller's
    request/question versions instead of reading the JD constants, and
    `score_candidates` passes the JD ones. Same cache file names and contents.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx

from packs.search.primitives.shared.openai_client import append_usage_row
from packs.search.primitives.llm_rerank_candidates.jev.features import build_features
from packs.search.primitives.llm_rerank_candidates.jev.model import (
    F1_CUTOFF,
    MODEL_ASSET,
    MODEL_ID,
    PROMPT_VERSION,
    QUESTION_VERSION,
    predict,
)
from packs.search.primitives.llm_rerank_candidates.jev.questions import REQUEST_VERSION, build_request


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = MODEL_ID
SCORE_TYPE = "qualification_score"
THRESHOLD = F1_CUTOFF
MODEL_ASSET_SHA256 = hashlib.sha256(MODEL_ASSET.read_bytes()).hexdigest()
MAX_CONCURRENCY = 4
MAX_RETRIES = 2
TIMEOUT_SECONDS = 120
MAX_INPUT_TOKENS = 64_000
INPUT_PRICE_PER_MILLION = 0.042
MAX_UNKNOWN_CALL_COST_USD = MAX_INPUT_TOKENS * INPUT_PRICE_PER_MILLION / 1_000_000
RETRYABLE_STATUS = frozenset((429, 500, 502, 503, 504, 529))


def request_digest(value: object) -> str:
    """The cache key for a request: sha256 of its canonical JSON."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def cache_path(output_dir: Path, request_hash: str) -> Path:
    """The exact-request cache file."""
    return output_dir / "jev" / f"{request_hash}.json"


def _validate_probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError
    return result


def validate_response(response: object, request: dict) -> dict:
    """Public for pin_confidence, which reuses Jev on its own question set."""
    try:
        if not isinstance(response, dict) or response.get("model") != MODEL:
            raise ValueError
        answers = response["answers"]
        if not isinstance(answers, dict) or set(answers) != set(request["questions"]):
            raise ValueError
        for name, question in request["questions"].items():
            answer = answers[name]
            if not isinstance(answer, dict) or answer.get("type") != question["type"]:
                raise ValueError
            if question["type"] == "noul":
                _validate_probability(answer["noul"])
                continue
            expected = (
                set(question["criteria"])
                if question["type"] == "choice"
                else {str(index) for index in range(len(question["criteria"]))}
            )
            probabilities = answer["probabilities"]
            if not isinstance(probabilities, dict) or set(probabilities) != expected:
                raise ValueError
            values = [_validate_probability(value) for value in probabilities.values()]
            if abs(sum(values) - 1) > 0.015:
                raise ValueError
        usage = response["usage"]
        if not isinstance(usage, dict):
            raise ValueError
        if type(usage["input_tokens"]) is not int or not 0 < usage["input_tokens"] <= MAX_INPUT_TOKENS:
            raise ValueError
        if type(usage["output_tokens"]) is not int or usage["output_tokens"] < 0:
            raise ValueError
        return response
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RuntimeError("Jev returned an invalid response; candidate remains unscored") from None


def _cache_record(
    raw_response: str,
    request_hash: str,
    request: dict,
    *,
    request_version: str,
    question_version: str,
) -> dict:
    return {
        "raw_response": raw_response,
        "request_sha256": request_hash,
        "model": MODEL,
        "request_version": request_version,
        "question_version": question_version,
        "prompt_version": PROMPT_VERSION,
        "assessment_date": request["state"]["reference_date"],
        "model_asset_sha256": MODEL_ASSET_SHA256,
        "threshold": THRESHOLD,
    }


def _validate_cache(
    record: object,
    request: dict,
    request_hash: str,
    *,
    request_version: str,
    question_version: str,
) -> dict:
    # The request hash and the caller's own versions bind a record; the capability judge's
    # prompt version is provenance only, so other question sets survive its bumps.
    expected = {
        "request_sha256": request_hash,
        "model": MODEL,
        "request_version": request_version,
        "question_version": question_version,
        "assessment_date": request["state"]["reference_date"],
    }
    if not isinstance(record, dict) or any(record.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Jev cache binding changed; preserved without another paid request")
    try:
        response = json.loads(record["raw_response"])
    except (KeyError, TypeError, json.JSONDecodeError):
        raise RuntimeError("Jev cached response is invalid; candidate remains unscored") from None
    return validate_response(response, request)


def _choice(answers: dict, name: str, options: list[str]) -> str:
    probabilities = answers[name]["probabilities"]
    return max(options, key=lambda option: probabilities[option])


def _evidence_summary(answers: dict) -> str:
    transfer = answers["transfer"]["probabilities"]
    transfer_score = sum(int(option) * probability for option, probability in transfer.items()) / 3
    execution = answers["independent_execution_quality"]["noul"]
    basis = _choice(answers, "evidence_basis", ["description", "summary", "repeated_roles", "isolated_title", "none"])
    continuity = _choice(
        answers,
        "continuity",
        ["current", "recent", "senior_adjacent", "stale_switch", "unknown", "none"],
    )
    return (
        f"Jev signals: transfer {transfer_score:.2f}; independent execution {execution:.2f}; "
        f"evidence {basis}; continuity {continuity}."
    )


async def _retry_delay(response: Any, attempt: int) -> None:
    header = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
    try:
        seconds = float(header) if header is not None else 2**attempt
    except ValueError:
        seconds = 2**attempt
    await asyncio.sleep(min(30.0, max(0.0, seconds)))


def _record_paid_usage(payload: object, latency_ms: int) -> None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    known_input = type(input_tokens) is int and input_tokens >= 0
    billed_input = input_tokens if known_input else MAX_INPUT_TOKENS
    append_usage_row(
        {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": str((payload.get("model") or MODEL) if isinstance(payload, dict) else MODEL),
            "stage": os.environ.get("POWERPACKS_USAGE_STAGE", "jev_capability"),
            "prompt_tokens": billed_input,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "completion_tokens": output_tokens if type(output_tokens) is int and output_tokens >= 0 else 0,
            "reasoning_tokens": 0,
            "latency_ms": latency_ms,
            "cost_usd": (
                billed_input * INPUT_PRICE_PER_MILLION / 1_000_000
                if known_input
                else MAX_UNKNOWN_CALL_COST_USD
            ),
            "cost_basis": "reported_input_tokens" if known_input else "64000_input_token_upper_bound",
        }
    )


async def _request(
    client: Any,
    request: dict,
    api_key: str,
    checkpoint: Any,
) -> tuple[dict, int]:
    attempts = 0
    retries = 0
    while True:
        attempts += 1
        started = time.monotonic()
        try:
            response = await client.post(
                ENDPOINT,
                json=request,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            if retries == MAX_RETRIES:
                raise RuntimeError("Jev request failed; candidate remains unscored") from None
            await asyncio.sleep(2**retries)
            retries += 1
            continue
        if response.status_code == HTTPStatus.OK:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                _record_paid_usage(None, int((time.monotonic() - started) * 1000))
                checkpoint(response.text)
                raise RuntimeError("Jev returned malformed JSON; candidate remains unscored") from None
            _record_paid_usage(payload, int((time.monotonic() - started) * 1000))
            raw_response = response.text or json.dumps(payload, ensure_ascii=False)
            checkpoint(raw_response)
            return validate_response(payload, request), attempts
        if response.status_code in RETRYABLE_STATUS and retries < MAX_RETRIES:
            await _retry_delay(response, retries)
            retries += 1
            continue
        raise RuntimeError(f"Jev HTTP {response.status_code}; candidate remains unscored")


@dataclass(frozen=True)
class AnsweredRequest:
    """One request's validated answers plus how they were obtained."""

    response: dict
    cache: Path
    cached: bool
    attempts: int


async def answer_requests(
    requests: dict[str, dict],
    *,
    output_dir: Path,
    api_key: str | None,
    client: Any | None,
    concurrency: int,
    request_version: str,
    question_version: str,
) -> dict[str, AnsweredRequest]:
    """Answer sha-keyed Jev requests, reusing the exact-request cache.

    `requests` maps `request_digest(request)` to the request; the result maps the same
    hashes to their answers, in the same order. A cached answer costs nothing and
    needs no API key. `request_version`/`question_version` bind the cache record to
    the CALLER's question set, so two question sets cannot read each other's files.
    Every request's state must carry `reference_date`, the cache record's assessment_date.
    """
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"Jev concurrency must be between 1 and {MAX_CONCURRENCY}")
    semaphore = asyncio.Semaphore(concurrency)
    key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
    owned_client = None

    async def answer_one(request_hash: str, request: dict) -> AnsweredRequest:
        nonlocal client, owned_client
        cache = cache_path(output_dir, request_hash)
        if cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise RuntimeError("Jev cache is unreadable; preserved without another paid request") from None
            response = _validate_cache(
                saved,
                request,
                request_hash,
                request_version=request_version,
                question_version=question_version,
            )
            return AnsweredRequest(response=response, cache=cache, cached=True, attempts=0)
        async with semaphore:
            if not key:
                raise RuntimeError("Jev requires TYPESAFE_API_KEY; candidate remains unscored")
            if client is None:
                owned_client = client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)

            def checkpoint(raw_response: str) -> None:
                saved = _cache_record(
                    raw_response,
                    request_hash,
                    request,
                    request_version=request_version,
                    question_version=question_version,
                )
                cache.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with cache.open("x", encoding="utf-8") as handle:
                        json.dump(saved, handle, ensure_ascii=False, allow_nan=False)
                except FileExistsError:
                    raise RuntimeError("Jev cache changed concurrently; preserved without overwrite") from None

            response, attempts = await _request(client, request, key, checkpoint)
            return AnsweredRequest(response=response, cache=cache, cached=False, attempts=attempts)

    try:
        rows = await asyncio.gather(
            *(answer_one(request_hash, request) for request_hash, request in requests.items()),
            return_exceptions=True,
        )
    finally:
        if owned_client is not None:
            await owned_client.aclose()
    for row in rows:
        if isinstance(row, BaseException):
            raise row
    return dict(zip(requests, rows))


async def evaluate_once(*, client: Any, request: dict, api_key: str) -> dict:
    """Evaluate an exact question set once, without retries or caching."""
    started = time.monotonic()
    response = await client.post(ENDPOINT, json=request,
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        _record_paid_usage(None, int((time.monotonic() - started) * 1000))
        raise RuntimeError("Jev returned malformed JSON; candidate remains unscored") from None
    _record_paid_usage(payload, int((time.monotonic() - started) * 1000))
    return validate_response(payload, request)


async def score_candidates(
    *,
    jd: str,
    profiles: dict[str, dict],
    output_dir: Path,
    as_of: str,
    api_key: str | None = None,
    concurrency: int = MAX_CONCURRENCY,
    client: Any | None = None,
) -> dict:
    """Score candidates with exact successful-response caching and no reject fallback."""
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"Jev concurrency must be between 1 and {MAX_CONCURRENCY}")
    result = {
        "status": "ok" if profiles else "empty",
        "model": MODEL,
        "revision": MODEL_ASSET_SHA256,
        "request_version": REQUEST_VERSION,
        "prompt_version": PROMPT_VERSION,
        "question_version": QUESTION_VERSION,
        "assessment_date": as_of,
        "score_type": SCORE_TYPE,
        "threshold": THRESHOLD,
        "model_asset_sha256": MODEL_ASSET_SHA256,
        "scores": [],
        "artifacts": [],
        "requests": 0,
        "cached_candidates": 0,
        "usage": {"pairs": 0, "input_tokens": 0, "output_tokens": 0},
        "paid_usage": {"pairs": 0, "input_tokens": 0, "output_tokens": 0},
        "cached_usage": {"pairs": 0, "input_tokens": 0, "output_tokens": 0},
    }
    if not profiles:
        return result

    requests: dict[str, dict] = {}
    candidate_requests = {}
    for person_id, profile in profiles.items():
        request = build_request(jd=jd, profile=profile, as_of=as_of)
        request_hash = request_digest(request)
        requests[request_hash] = request
        candidate_requests[person_id] = request_hash

    records = await answer_requests(
        requests,
        output_dir=output_dir,
        api_key=api_key,
        client=client,
        concurrency=concurrency,
        request_version=REQUEST_VERSION,
        question_version=QUESTION_VERSION,
    )
    for answer in records.values():
        result["artifacts"].append(str(answer.cache))
        result["requests"] += answer.attempts
        usage = answer.response["usage"]
        result["usage"]["pairs"] += 1
        result["usage"]["input_tokens"] += usage["input_tokens"]
        result["usage"]["output_tokens"] += usage["output_tokens"]
        usage_bucket = result["cached_usage"] if answer.cached else result["paid_usage"]
        usage_bucket["pairs"] += 1
        usage_bucket["input_tokens"] += usage["input_tokens"]
        usage_bucket["output_tokens"] += usage["output_tokens"]
    for person_id, request_hash in candidate_requests.items():
        answers = records[request_hash].response["answers"]
        score = predict(build_features(answers))
        result["cached_candidates"] += int(records[request_hash].cached)
        result["scores"].append(
            {
                "id": person_id,
                "score": score,
                "passed": score >= THRESHOLD,
                "evidence": _evidence_summary(answers),
                "threshold": THRESHOLD,
            }
        )
    return result
