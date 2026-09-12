"""Score complete hydrated profiles through the authenticated cross-encoder gateway."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import threading
from contextlib import ExitStack
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx

ENDPOINT = "https://proxy.powerset.dev/vendor/cross-encoder/rerank"
WARMUP_ENDPOINT = "https://proxy.powerset.dev/vendor/cross-encoder/warmup"
WARMUP_TIMEOUT = 240
MAX_PAIRS = 1000
MAX_TEXT_CHARS = 131072
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_INPUT_TOKENS = 32768
_DEMOGRAPHIC_FIELDS = {
    "age", "inferred_age", "birth_year", "inferred_birth_year", "birth_date", "date_of_birth", "dob",
    "gender", "sex", "race", "ethnicity", "religion", "sexual_orientation", "marital_status",
    "disability", "disability_status", "pregnancy", "pregnancy_status",
}


def warm_workers(*, api_key: str | None = None) -> None:
    """Start loading workers without holding up retrieval or process exit."""
    key = api_key if api_key is not None else os.environ.get("POWERSET_API_KEY")
    if not key:
        print("cross-encoder warmup: skipped (missing POWERSET_API_KEY)", file=sys.stderr)
        return

    def request() -> None:
        try:
            with httpx.Client(timeout=WARMUP_TIMEOUT) as client:
                response = client.post(WARMUP_ENDPOINT, headers={"x-powerset-key": key})
                response.raise_for_status()
        except httpx.HTTPError:
            print("cross-encoder warmup: unavailable; scoring will start workers if needed", file=sys.stderr)
            return
        print("cross-encoder warmup: ready", file=sys.stderr)

    threading.Thread(target=request, name="cross-encoder-warmup", daemon=True).start()


def _profile_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _profile_evidence(item) for key, item in value.items()
                if key.lower() not in _DEMOGRAPHIC_FIELDS}
    if isinstance(value, list):
        return [_profile_evidence(item) for item in value]
    return value


def _batches(query: str, profiles: dict[str, dict]) -> list[tuple[list[str], bytes]]:
    if not isinstance(query, str) or not 1 <= len(query) <= MAX_TEXT_CHARS:
        raise ValueError("Cross-encoder query must contain 1–131072 characters; nothing truncated")
    batches = []
    ids: list[str] = []
    encoded: list[bytes] = []
    size = len(b'{"pairs":[]}')
    for person_id, profile in profiles.items():
        if not isinstance(person_id, str) or not 1 <= len(person_id) <= 256:
            raise ValueError("Cross-encoder candidate IDs must contain 1–256 characters")
        passage = json.dumps(_profile_evidence(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(passage) > MAX_TEXT_CHARS:
            raise ValueError("Cross-encoder profile exceeds 131072 characters; nothing truncated")
        pair = json.dumps({"id": person_id, "query": query, "passage": passage},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(pair) + len(b'{"pairs":[]}') > MAX_REQUEST_BYTES:
            raise ValueError("Cross-encoder pair exceeds request byte limit; nothing truncated")
        if encoded and (len(ids) == MAX_PAIRS or size + len(pair) + 1 > MAX_REQUEST_BYTES):
            batches.append((ids, b'{"pairs":[' + b",".join(encoded) + b"]}"))
            ids, encoded, size = [], [], len(b'{"pairs":[]}')
        size += len(pair) + bool(encoded)
        ids.append(person_id)
        encoded.append(pair)
    if encoded:
        batches.append((ids, b'{"pairs":[' + b",".join(encoded) + b"]}"))
    return batches


def _validate(response: Any, ids: list[str]) -> list[dict]:
    try:
        rows = response["scores"]
        if not isinstance(rows, list) or len(rows) != len(ids):
            raise ValueError
        scores = {}
        for row in rows:
            score = row["score"]
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError
            scores[row["id"]] = score
        if set(scores) != set(ids):
            raise ValueError
        if any(not isinstance(response[field], str) or not response[field] for field in ("model", "revision")):
            raise ValueError
        if response["score_type"] != "raw_yes_minus_no_logit":
            raise ValueError
        usage = response["usage"]
        if any(type(usage[field]) is not int or usage[field] < 0
               for field in ("pairs", "input_tokens", "output_tokens", "max_tokens", "truncated")):
            raise ValueError
        if (usage["pairs"] != len(ids) or usage["truncated"] != 0
                or not 1 <= usage["max_tokens"] <= MAX_INPUT_TOKENS
                or usage["input_tokens"] < max(usage["max_tokens"], len(ids))):
            raise ValueError
        return [{"id": person_id, "score": scores[person_id]} for person_id in ids]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RuntimeError("Cross-encoder returned an invalid response; scores not applied") from None


def score_candidates(*, query: str, profiles: dict[str, dict], output_dir: Path,
                     api_key: str | None = None) -> dict:
    """Preserve input order and reuse exact successful requests without charging again."""
    result = {
        "status": "ok" if profiles else "empty", "model": None, "revision": None, "adapter": None,
        "score_type": "raw_yes_minus_no_logit", "scores": [], "artifacts": [], "requests": 0,
        "cached_batches": 0,
        "usage": {"pairs": 0, "input_tokens": 0, "output_tokens": 0, "max_tokens": 0, "truncated": 0},
    }
    if not profiles:
        return result
    batches = _batches(query, profiles)
    key = api_key if api_key is not None else os.environ.get("POWERSET_API_KEY")
    client = None
    with ExitStack() as stack:
        for ids, body in batches:
            digest = hashlib.sha256(ENDPOINT.encode() + b"\n" + body).hexdigest()
            cache = output_dir / "cross_encoder" / f"{digest}.json"
            cached = cache.exists()
            if cached:
                try:
                    response = json.loads(cache.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise RuntimeError("Cross-encoder cache is unreadable; preserved without another paid request") from None
            else:
                if not key:
                    raise RuntimeError("Cross-encoder beta requires POWERSET_API_KEY")
                if client is None:
                    client = stack.enter_context(httpx.Client(timeout=600))
                try:
                    http = client.post(ENDPOINT, content=body, headers={
                        "x-powerset-key": key, "content-type": "application/json",
                    })
                except httpx.HTTPError:
                    raise RuntimeError("Cross-encoder request failed; no automatic retry") from None
                if http.status_code != HTTPStatus.OK:
                    raise RuntimeError(f"Cross-encoder HTTP {http.status_code}; no automatic retry")
                try:
                    response = http.json()
                except ValueError:
                    raise RuntimeError("Cross-encoder returned an invalid response; scores not applied") from None
            scores = _validate(response, ids)
            if not cached:
                cache.parent.mkdir(parents=True, exist_ok=True)
                with cache.open("x", encoding="utf-8") as handle:
                    json.dump(response, handle, ensure_ascii=False)
            metadata = {field: response.get(field) for field in ("model", "revision", "adapter", "score_type")}
            if result["scores"] and any(result[field] != value for field, value in metadata.items()):
                raise RuntimeError("Cross-encoder model changed between batches; scores not applied")
            result.update(metadata)
            result["scores"].extend(scores)
            result["artifacts"].append(str(cache))
            result["cached_batches"] += int(cached)
            result["requests"] += int(not cached)
            for field in ("pairs", "input_tokens", "output_tokens", "truncated"):
                result["usage"][field] += response["usage"][field]
            result["usage"]["max_tokens"] = max(result["usage"]["max_tokens"], response["usage"]["max_tokens"])
    return result
