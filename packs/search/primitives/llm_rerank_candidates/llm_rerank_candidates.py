#!/usr/bin/env python3
"""Async fan-out LLM rerank for arbitrary candidate items.

Calls an OpenAI-compatible chat completion endpoint once per input item,
in parallel under a configurable concurrency limit. Same shape as the
production `SEARCH_V2_RERANK_MAX_CONCURRENT=400` path in network-search-api,
but Powerpacks-local and stdlib-only.

Differences from `llm_filter_candidates`:
- Generic per-item prompts (not tied to task_state shape)
- Async fan-out with `asyncio.Semaphore` (configurable, default 50)
- Does NOT require `set_id` or any set context
- Designed for testing concurrency / load / latency without a full
  search-network task

Inputs:
- `--in PATH | -` : JSONL of candidates. Each row is a JSON object.
- `--query STRING` : the search query (for prompt context)
- `--traits TRAIT` : expected traits (repeatable)
- `--concurrency N` : asyncio.Semaphore size (default 50)
- `--model NAME` : chat completion model (default gpt-4o-mini)
- `--api-base URL` : base URL (default https://api.openai.com)
- `--api-key KEY` : OpenAI API key (default $OPENAI_API_KEY)
- `--out PATH | -` : where to write the enriched JSONL (default stdout)
- `--dry-run` : build prompts, do not call the API; emit prompts to stderr
- `--include-prompt` : echo the per-item prompt back into the output row
- `--max-retries N` : retry on 429 / 5xx (default 3)
- `--timeout SEC` : per-call timeout (default 120)

Outputs (JSONL, one line per input):
    {
      "id": "<from input or position>",
      "score": 0.0..1.0,
      "verdict": "include" | "exclude",
      "reason": "...",
      "model": "...",
      "elapsed_ms": int,
      "error": null | str,
      "input": {...original...}
    }

A summary is printed to stderr at the end:
    rerank: items=N concurrency=M ok=X failed=Y elapsed=Ts

Stdlib only.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com")
DEFAULT_MODEL = os.environ.get("LLM_RERANK_MODEL", "gpt-4o-mini")
DEFAULT_CONCURRENCY = int(os.environ.get("LLM_RERANK_CONCURRENCY", "50"))


SYSTEM_PROMPT = """You are a fast pre-screener for people-search results.

Given a query, expected traits, and a candidate profile, return a strict
JSON object:

  {"score": <0.0-1.0>, "verdict": "include" | "exclude", "reason": "<short>"}

Scoring guide:
  1.0  perfect match for query + every trait
  0.7  matches query, partial trait match
  0.3  weakly relevant
  0.0  unrelated

If uncertain, lean toward `include` with a moderate score (e.g. 0.5).
Output JSON only. No markdown fences. No commentary.
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RerankItem:
    """One input candidate."""

    position: int
    payload: dict[str, Any]

    @property
    def id(self) -> str:
        for key in ("id", "person_id", "member_id", "candidate_id"):
            v = self.payload.get(key)
            if v is not None:
                return str(v)
        return f"pos-{self.position}"


@dataclass
class RerankResult:
    """One rerank verdict."""

    id: str
    score: float
    verdict: str
    reason: str
    model: str
    elapsed_ms: int
    input: dict[str, Any]
    error: Optional[str] = None
    prompt: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "score": self.score,
            "verdict": self.verdict,
            "reason": self.reason,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "input": self.input,
        }
        if self.prompt is not None:
            out["prompt"] = self.prompt
        return out


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_user_prompt(query: str, traits: list[str], item: RerankItem) -> str:
    traits_block = "\n".join(f"- {t}" for t in traits) if traits else "(none specified)"
    payload_json = json.dumps(item.payload, sort_keys=True, indent=2)
    return f"""Query: {query}

Expected traits:
{traits_block}

Candidate (JSON):
{payload_json}

Return the JSON verdict object only.
"""


# ---------------------------------------------------------------------------
# OpenAI call (sync, run in thread pool from async fan-out)
# ---------------------------------------------------------------------------


def call_chat_completion(
    api_base: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int,
) -> dict[str, Any]:
    """Synchronous OpenAI-compatible chat completion. Returns raw JSON."""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode()
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def parse_verdict(raw_response: dict[str, Any]) -> tuple[float, str, str]:
    """Extract (score, verdict, reason) from an OpenAI chat response."""
    try:
        content = raw_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"unexpected response shape: {e}")
    # Tolerate occasional markdown fences even though we asked for json_object.
    content = content.strip()
    if content.startswith("```"):
        match = re.search(r"\{.*\}", content, re.DOTALL)
        content = match.group(0) if match else content
    parsed = json.loads(content)
    score_raw = parsed.get("score", 0.0)
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    verdict = str(parsed.get("verdict", "exclude")).lower()
    if verdict not in ("include", "exclude"):
        verdict = "include" if score >= 0.5 else "exclude"
    reason = str(parsed.get("reason", "")).strip()
    return score, verdict, reason


# ---------------------------------------------------------------------------
# Async fan-out
# ---------------------------------------------------------------------------


async def rerank_one(
    item: RerankItem,
    *,
    query: str,
    traits: list[str],
    api_base: str,
    api_key: str,
    model: str,
    semaphore: asyncio.Semaphore,
    executor: concurrent.futures.Executor,
    timeout: int,
    max_retries: int,
    include_prompt: bool,
) -> RerankResult:
    user_prompt = build_user_prompt(query, traits, item)
    started = time.monotonic()
    error: Optional[str] = None
    score = 0.0
    verdict = "exclude"
    reason = ""
    raw_response: dict[str, Any] = {}

    async with semaphore:
        loop = asyncio.get_running_loop()
        attempt = 0
        while True:
            try:
                raw_response = await loop.run_in_executor(
                    executor,
                    call_chat_completion,
                    api_base,
                    api_key,
                    model,
                    SYSTEM_PROMPT,
                    user_prompt,
                    timeout,
                )
                score, verdict, reason = parse_verdict(raw_response)
                error = None
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < max_retries:
                    backoff = 0.5 * (2**attempt)
                    await asyncio.sleep(backoff)
                    attempt += 1
                    continue
                error = f"http {e.code}: {e.reason}"
                break
            except (urllib.error.URLError, TimeoutError, asyncio.TimeoutError) as e:
                if attempt < max_retries:
                    backoff = 0.5 * (2**attempt)
                    await asyncio.sleep(backoff)
                    attempt += 1
                    continue
                error = f"network: {e}"
                break
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
                break

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return RerankResult(
        id=item.id,
        score=score,
        verdict=verdict,
        reason=reason,
        model=model,
        elapsed_ms=elapsed_ms,
        input=item.payload,
        error=error,
        prompt=user_prompt if include_prompt else None,
    )


async def rerank_all(
    items: list[RerankItem],
    *,
    query: str,
    traits: list[str],
    api_base: str,
    api_key: str,
    model: str,
    concurrency: int,
    timeout: int,
    max_retries: int,
    include_prompt: bool,
) -> list[RerankResult]:
    semaphore = asyncio.Semaphore(concurrency)
    # Pool of OS threads so urllib calls don't block the event loop.
    # max_workers >= concurrency so we never bottleneck on the executor.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
    try:
        tasks = [
            rerank_one(
                item,
                query=query,
                traits=traits,
                api_base=api_base,
                api_key=api_key,
                model=model,
                semaphore=semaphore,
                executor=executor,
                timeout=timeout,
                max_retries=max_retries,
                include_prompt=include_prompt,
            )
            for item in items
        ]
        return await asyncio.gather(*tasks)
    finally:
        executor.shutdown(wait=True, cancel_futures=False)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def load_items(path: str) -> list[RerankItem]:
    if path == "-":
        data = sys.stdin.read()
    else:
        data = Path(path).read_text()
    items: list[RerankItem] = []
    for i, line in enumerate(data.splitlines()):
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"input line {i} is not a JSON object: {line[:80]}")
        items.append(RerankItem(position=i, payload=payload))
    return items


def write_results(results: list[RerankResult], path: str) -> None:
    lines = [json.dumps(r.to_dict(), sort_keys=True) for r in results]
    body = "\n".join(lines) + ("\n" if lines else "")
    if path == "-":
        sys.stdout.write(body)
    else:
        Path(path).write_text(body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Async fan-out LLM rerank over a JSONL of candidates."
    )
    parser.add_argument("--in", dest="in_path", required=True, help="JSONL path or '-' for stdin")
    parser.add_argument("--out", dest="out_path", default="-", help="JSONL path or '-' for stdout")
    parser.add_argument("--query", required=True, help="Search query (prompt context)")
    parser.add_argument("--traits", action="append", default=[], help="Expected trait (repeatable)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-prompt", action="store_true")
    args = parser.parse_args()

    try:
        items = load_items(args.in_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not items:
        print("error: no input items", file=sys.stderr)
        return 2

    if args.dry_run:
        for item in items:
            prompt = build_user_prompt(args.query, args.traits, item)
            sys.stderr.write(f"--- {item.id} ---\n{prompt}\n\n")
        sys.stderr.write(
            f"rerank: dry-run items={len(items)} concurrency={args.concurrency}\n"
        )
        return 0

    if not args.api_key:
        print("error: --api-key or OPENAI_API_KEY required", file=sys.stderr)
        return 2

    started = time.monotonic()
    results = asyncio.run(
        rerank_all(
            items,
            query=args.query,
            traits=args.traits,
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
            concurrency=args.concurrency,
            timeout=args.timeout,
            max_retries=args.max_retries,
            include_prompt=args.include_prompt,
        )
    )
    elapsed = time.monotonic() - started

    write_results(results, args.out_path)

    ok = sum(1 for r in results if r.error is None)
    failed = len(results) - ok
    sys.stderr.write(
        f"rerank: items={len(results)} concurrency={args.concurrency} "
        f"ok={ok} failed={failed} elapsed={elapsed:.2f}s\n"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
