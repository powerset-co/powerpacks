#!/usr/bin/env python3
"""Async fan-out LLM rerank for arbitrary candidate items.

Calls an OpenAI-compatible chat completion endpoint once per input item,
in parallel under a configurable concurrency limit. Same shape as the
production configurable fan-out path in network-search-api, but
Powerpacks-local and stdlib-only.

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
import csv
import gzip
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
DEFAULT_CONCURRENCY = int(os.environ.get("LLM_RERANK_CONCURRENCY", "200"))
DEFAULT_ESTIMATE_SECONDS = int(os.environ.get("LLM_RERANK_ESTIMATE_SECONDS", "180"))
EXPLICIT_SENIORITY_RE = re.compile(
    r"\b("
    r"entry[- ]level|junior|mid[- ]level|senior|staff|principal|"
    r"manager|director|vp|vice president|c[- ]level|c[- ]suite|"
    r"founder|co[- ]founder|partner|owner|lead"
    r")\b",
    re.I,
)
IC_SENIORITY_TERMS = {"junior", "mid", "senior", "staff", "principal", "lead"}
LEADERSHIP_SENIORITY_RE = re.compile(
    r"\b(manager|director|vp|vice president|c[- ]level|c[- ]suite|founder|co[- ]founder|partner|owner)\b",
    re.I,
)
OUT_OF_BAND_IC_TITLE_RE = re.compile(
    r"\b("
    r"cto|chief\s+technology\s+officer|chief\s+technical\s+officer|"
    r"ceo|coo|cfo|cpo|cio|ciso|chief\b|"
    r"vp\b|vice\s+president|head\s+of|director|founder|co[- ]founder|"
    r"advisor|adviser|consultant"
    r")\b",
    re.I,
)
SENIORITY_MISMATCH_SCORE_CAP = 0.25


SYSTEM_PROMPT = """You are an expert recruiter reranking people-search results.

Given a search query, expected traits, and one candidate profile, evaluate the
candidate with evidence-based scoring. This prompt mirrors the production app's
LLM rerank behavior but is Powerpacks-local and stdlib-only.

Return a strict JSON object:

  {
    "score": <0.0-1.0>,
    "verdict": "include" | "exclude",
    "reason": "<1-2 short evidence-based sentences>",
    "confidence": <0.0-1.0>,
    "trait_scores": {"<trait name>": <0.0-1.0>, ...}
  }

Scoring rubric:
- 0.9-1.0 (Exceptional): Clear, direct, recent evidence for nearly every trait.
- 0.75-0.89 (Strong): Strong evidence and likely highly relevant.
- 0.60-0.74 (Good): Solid evidence for the main intent; may miss a secondary trait.
- 0.40-0.59 (Moderate): Some relevant evidence but not a standout.
- 0.25-0.39 (Weak): Limited, indirect, stale, or low-confidence signal.
- 0.0 (Out): No match or disqualified by an explicit exclusion.

Critical rules:
1. Do not give everyone high scores. Differentiate candidates clearly.
2. Cite specific evidence from the profile: title, company, education, dates, or description.
3. If a trait has no evidence, its trait score should be low.
4. Recency matters. Current/recent roles beat old roles unless the user asks for past experience.
5. Explicit exclusions are hard gates: excluded candidates score 0.0.
6. Explicit seniority is a hiring target band, not a loose "or above" signal. For "senior software engineer", strongly prefer Senior SWE/current senior IC profiles and score obvious out-of-band titles like CTO, VP Engineering, Director, Founder, Tech Advisor, Advisor, or Consultant low/out unless the query asks for those leadership/founder/advisor/consultant levels. Staff/principal are not synonyms for "senior" unless the query says senior+ or names those bands.
7. Output JSON only. No markdown fences. No commentary.
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
    confidence: float = 0.0
    trait_scores: dict[str, float] = field(default_factory=dict)
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
            "confidence": self.confidence,
            "trait_scores": self.trait_scores,
            "error": self.error,
            "input": self.input,
        }
        if self.prompt is not None:
            out["prompt"] = self.prompt
        return out


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_user_prompt(query: str, traits: list[dict[str, str]], item: RerankItem) -> str:
    traits_block = format_traits_block(traits)
    payload_json = json.dumps(item.payload, sort_keys=True, indent=2)
    return f"""Query: {query}

Expected traits:
{traits_block}

Candidate (JSON):
{payload_json}

Return the JSON verdict object only.
"""


def requested_ic_seniority_terms(query: str, traits: list[dict[str, str]]) -> set[str]:
    """Return explicit requested IC seniority terms, preserving inferred-default behavior."""
    text = " ".join([query, *[str(t.get("value", "")) for t in traits]]).lower()
    if not EXPLICIT_SENIORITY_RE.search(text) or LEADERSHIP_SENIORITY_RE.search(text):
        return set()

    requested: set[str] = set()
    if re.search(r"\b(entry[- ]level|junior)\b", text):
        requested.add("junior")
    if re.search(r"\bmid[- ]level\b", text):
        requested.add("mid")
    if re.search(r"\b(senior|lead)\b", text):
        requested.add("senior")
    if re.search(r"\bsenior\s*(\+|or above)\b", text):
        requested.update({"staff", "principal"})
    if re.search(r"\bstaff\b", text):
        requested.add("staff")
    if re.search(r"\bprincipal\b", text):
        requested.add("principal")
    return requested & IC_SENIORITY_TERMS


def requested_advisory_terms(query: str, traits: list[dict[str, str]]) -> set[str]:
    text = " ".join([query, *[str(t.get("value", "")) for t in traits]])
    allowed = set()
    if re.search(r"\bconsultants?\b", text, re.I):
        allowed.add("consultant")
    if re.search(r"\badvis[oe]rs?\b", text, re.I):
        allowed.add("advisor")
    return allowed


def _candidate_evidence_positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions = payload.get("positions")
    if isinstance(positions, list) and positions:
        current = [p for p in positions if isinstance(p, dict) and p.get("is_current")]
        if current:
            return current
        return [p for p in positions[:2] if isinstance(p, dict)]

    title = payload.get("headline") or payload.get("title") or payload.get("position_title")
    if title:
        return [{"position_title": title, "is_current": True}]
    return []


def _is_out_of_band_ic_title(title: str, advisory_terms_allowed: set[str]) -> bool:
    title_for_check = title
    if "consultant" in advisory_terms_allowed:
        title_for_check = re.sub(r"\bconsultants?\b", " ", title_for_check, flags=re.I)
    if "advisor" in advisory_terms_allowed:
        title_for_check = re.sub(r"\badvis[oe]rs?\b", " ", title_for_check, flags=re.I)
    return bool(OUT_OF_BAND_IC_TITLE_RE.search(title_for_check))


def _position_matches_requested_ic_band(pos: dict[str, Any], requested_terms: set[str], advisory_terms_allowed: set[str]) -> bool:
    title = str(pos.get("position_title") or pos.get("title") or "")
    seniority_band = str(pos.get("seniority_band") or "").lower()
    haystack = f"{title} {seniority_band}".lower()
    if _is_out_of_band_ic_title(title, advisory_terms_allowed):
        return False
    return any(term in haystack for term in requested_terms)


def apply_explicit_seniority_guardrail(
    query: str,
    traits: list[dict[str, str]],
    payload: dict[str, Any],
    score: float,
    verdict: str,
    reason: str,
    trait_scores: dict[str, float],
) -> tuple[float, str, str, dict[str, float]]:
    """Cap obvious out-of-band executive/advisor profiles for explicit IC seniority queries."""
    requested_terms = requested_ic_seniority_terms(query, traits)
    if not requested_terms:
        return score, verdict, reason, trait_scores
    advisory_terms_allowed = requested_advisory_terms(query, traits)

    evidence_positions = _candidate_evidence_positions(payload)
    if not evidence_positions:
        return score, verdict, reason, trait_scores
    if any(_position_matches_requested_ic_band(pos, requested_terms, advisory_terms_allowed) for pos in evidence_positions):
        return score, verdict, reason, trait_scores

    has_out_of_band_title = any(
        _is_out_of_band_ic_title(str(pos.get("position_title") or pos.get("title") or ""), advisory_terms_allowed)
        for pos in evidence_positions
    )
    if not has_out_of_band_title or score <= SENIORITY_MISMATCH_SCORE_CAP:
        return score, verdict, reason, trait_scores

    capped_traits = {key: min(value, SENIORITY_MISMATCH_SCORE_CAP) for key, value in trait_scores.items()}
    capped_traits["Seniority fit"] = 0.0
    guardrail_reason = (
        "Explicit IC seniority was requested, but the current/recent title is clearly "
        "out of band for that hiring level."
    )
    reason = f"{reason} {guardrail_reason}".strip()
    return SENIORITY_MISMATCH_SCORE_CAP, "exclude", reason, capped_traits


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


def parse_verdict(raw_response: dict[str, Any], traits: list[dict[str, str]]) -> tuple[float, str, str, float, dict[str, float]]:
    """Extract (score, verdict, reason, confidence, trait_scores) from a chat response."""
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
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    trait_scores_raw = parsed.get("trait_scores") or {}
    trait_scores: dict[str, float] = {}
    if isinstance(trait_scores_raw, dict):
        for key, value in trait_scores_raw.items():
            try:
                trait_scores[str(key)] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
    for trait in traits:
        trait_scores.setdefault(trait["value"], score)
    return score, verdict, reason, confidence, trait_scores


# ---------------------------------------------------------------------------
# Async fan-out
# ---------------------------------------------------------------------------


async def rerank_one(
    item: RerankItem,
    *,
    query: str,
    traits: list[dict[str, str]],
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
    confidence = 0.0
    trait_scores: dict[str, float] = {}

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
                score, verdict, reason, confidence, trait_scores = parse_verdict(raw_response, traits)
                score, verdict, reason, trait_scores = apply_explicit_seniority_guardrail(
                    query=query,
                    traits=traits,
                    payload=item.payload,
                    score=score,
                    verdict=verdict,
                    reason=reason,
                    trait_scores=trait_scores,
                )
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
        confidence=confidence,
        trait_scores=trait_scores,
        error=error,
        prompt=user_prompt if include_prompt else None,
    )


async def rerank_all(
    items: list[RerankItem],
    *,
    query: str,
    traits: list[dict[str, str]],
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
# State helpers / I/O
# ---------------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")



def estimate_rerank_seconds(item_count: int, concurrency: int) -> int:
    """Return a conservative user-facing runtime estimate for LLM reranking."""
    if item_count <= 0:
        return 0
    concurrency = max(1, concurrency)
    waves = (item_count + concurrency - 1) // concurrency
    return max(DEFAULT_ESTIMATE_SECONDS, waves * 30)

def append_event(state_path: Path, event: dict[str, Any]) -> None:
    event_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def step_output(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def state_frontier_ids(state: dict[str, Any]) -> list[str]:
    rerank = step_output(state, "llm_rerank_candidates")
    ids = rerank.get("ranked_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))
    llm_filter = step_output(state, "llm_filter_candidates")
    ids = llm_filter.get("passed_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))
    for step_id, key in [
        ("merge_candidate_frontier", "frontier_candidate_ids"),
        ("execute_role_search", "candidate_ids"),
        ("execute_search_slice", "candidate_ids"),
        ("direct_execute", "person_ids"),
    ]:
        ids = step_output(state, step_id).get(key) or []
        if ids:
            return list(dict.fromkeys(str(pid) for pid in ids if pid))
    hydrate = step_output(state, "hydrate_people")
    ids = hydrate.get("profile_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))
    return list(dict.fromkeys(str(p["person_id"]) for p in hydrate.get("profiles", []) or [] if p.get("person_id")))


def state_hydrated_profiles(state: dict[str, Any], *, llm_handoff: bool) -> dict[str, dict[str, Any]]:
    hydrate = step_output(state, "hydrate_people")
    path_key = "llm_profiles_path" if llm_handoff else "profiles_path"
    profiles_path = hydrate.get(path_key) or hydrate.get("profiles_path")
    rows = load_items(str(profiles_path)) if profiles_path else [RerankItem(position=i, payload=profile) for i, profile in enumerate(hydrate.get("profiles", []) or [])]
    out: dict[str, dict[str, Any]] = {}
    for item in rows:
        profile = item.payload
        if isinstance(profile, dict) and profile.get("person_id"):
            out[str(profile["person_id"])] = profile
    return out


def state_traits(state: dict[str, Any]) -> list[dict[str, str]]:
    """Get structured traits from the trait generator in expand_search_request.

    Returns list of {"value": ..., "temporal": ..., "meaning": ...} dicts
    directly from the trait generator output. No string conversion.
    """
    expand = step_output(state, "expand_search_request") or step_output(state, "expand")
    generated = expand.get("traits") or []
    traits = []
    for t in generated:
        if isinstance(t, dict) and t.get("value"):
            traits.append({
                "value": t["value"],
                "temporal": t.get("temporal", "all"),
                "meaning": t.get("meaning", "general"),
            })
    if not traits:
        # Fallback: wrap query as a single trait
        query = state.get("query") or "Relevant to the original query"
        traits = [{"value": query, "temporal": "all", "meaning": "general"}]
    return traits


def format_traits_block(traits: list[dict[str, str]]) -> str:
    """Format structured traits for the reranker prompt.

    Matches the app's format: 1. "value" (scope: temporal, type: meaning)
    """
    if not traits:
        return "(none specified)"
    lines = []
    for i, t in enumerate(traits, 1):
        lines.append(f'{i}. "{t["value"]}" (scope: {t["temporal"]}, type: {t["meaning"]})')
    return "\n".join(lines)


def trait_values(traits: list[dict[str, str]]) -> list[str]:
    """Extract just the value strings from structured traits."""
    return [t["value"] for t in traits]


def artifact_dir(state_path: Path, state: dict[str, Any]) -> Path:
    existing = state.get("artifacts") or {}
    if existing.get("artifact_dir"):
        return Path(str(existing["artifact_dir"]))
    return state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem)


def compact_llm_profile(profile: dict[str, Any]) -> dict[str, Any]:
    positions = profile.get("positions") or []
    matched = set(profile.get("matched_position_indexes") or [])
    selected = []
    for idx, pos in enumerate(positions):
        if isinstance(pos, dict) and (pos.get("is_current") or idx in matched):
            selected.append(pos)
    if not selected and positions:
        selected = [positions[0]]
    out = dict(profile)
    out["positions"] = selected
    return out


def load_items_from_state(state_path: Path, *, max_candidates: Optional[int] = None) -> tuple[dict[str, Any], list[RerankItem]]:
    state = read_json(state_path)
    ids = state_frontier_ids(state)
    # Rerank needs the full hydrated profile. LLM filtering may use the compact
    # handoff, but reranking should see all profile evidence for final ordering.
    profiles = state_hydrated_profiles(state, llm_handoff=False)
    items: list[RerankItem] = []
    for pid in ids:
        profile = profiles.get(pid)
        if not profile:
            continue
        items.append(RerankItem(position=len(items), payload=profile))
        if max_candidates and len(items) >= max_candidates:
            break
    return state, items


def load_items(path: str) -> list[RerankItem]:
    if path == "-":
        data = sys.stdin.read()
    else:
        path_obj = Path(path)
        if path_obj.suffix == ".gz":
            with gzip.open(path_obj, "rt") as handle:
                data = handle.read()
        else:
            data = path_obj.read_text()
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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(body)


QUERY_RESULTS_V2_FIELDS = [
    "conversation_id",
    "query",
    "person_id",
    "result_index",
    "matched_position_indexes",
    "final_score",
    "trait_scores",
    "overall_reasoning",
    "pre_rerank_score",
    "tags",
    "vertical_sources",
    "created_at",
]


def build_query_result_rows(
    results: list[RerankResult],
    *,
    state: dict[str, Any],
    query: str,
    created_at: str,
) -> list[dict[str, Any]]:
    """Return rows shaped exactly like network-search-api QueryResultV2.to_full_dict()."""
    conversation_id = str(state.get("conversation_id") or state.get("task_id") or "")
    ordered = sorted(results, key=lambda r: r.score, reverse=True)
    rows: list[dict[str, Any]] = []
    for index, result in enumerate(ordered):
        profile = result.input or {}
        per_trait = result.trait_scores or {"overall": result.score}
        trait_scores = {
            trait: {"score": score, "reason": result.reason, "confidence": result.confidence}
            for trait, score in per_trait.items()
        }
        rows.append({
            "conversation_id": conversation_id,
            "query": query,
            "person_id": result.id,
            "result_index": index,
            "matched_position_indexes": profile.get("matched_position_indexes") or [],
            "final_score": result.score,
            "trait_scores": trait_scores,
            "overall_reasoning": result.reason,
            "pre_rerank_score": profile.get("base_score") or profile.get("score"),
            "tags": profile.get("tags"),
            "vertical_sources": profile.get("vertical_sources"),
            "created_at": created_at,
        })
    return rows


def write_query_results_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUERY_RESULTS_V2_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(row.get(key), sort_keys=True) if isinstance(row.get(key), (dict, list)) else row.get(key)
                for key in QUERY_RESULTS_V2_FIELDS
            })


def record_state_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state.setdefault("steps", []).append({
        "id": "llm_rerank_candidates",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "llm_rerank_candidates",
        "status": "completed",
        "timestamp": now,
        "ranked_count": output.get("ranked_count"),
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Async fan-out LLM rerank over a JSONL of candidates."
    )
    parser.add_argument("--in", dest="in_path", help="JSONL path or '-' for stdin")
    parser.add_argument("--state", help="Powerpacks task-state path; reads full hydrate_people profiles_path and writes rerank artifacts")
    parser.add_argument("--out", dest="out_path", default="-", help="JSONL path or '-' for stdout")
    parser.add_argument("--query", help="Search query (prompt context); defaults to state.query in --state mode")
    parser.add_argument("--traits", action="append", default=[], help="Expected trait string (repeatable, wrapped to structured dict at parse time)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-prompt", action="store_true")
    parser.add_argument("--current-and-matched-only", action="store_true", help="Deprecated no-op in --state mode; rerank always reads full profiles_path")
    parser.add_argument("--include-all-positions", action="store_true", help="Deprecated no-op in --state mode; rerank always reads full profiles_path")
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--dump-debug", action="store_true", help="Write raw rerank JSONL for debugging")
    args = parser.parse_args()

    # Normalize CLI --traits strings to structured dicts immediately
    if args.traits and isinstance(args.traits[0], str):
        args.traits = [{"value": t, "temporal": "all", "meaning": "general"} for t in args.traits]

    if not args.in_path and not args.state:
        print("error: --in or --state required", file=sys.stderr)
        return 2

    state: Optional[dict[str, Any]] = None
    state_path: Optional[Path] = Path(args.state) if args.state else None
    try:
        if state_path:
            state, items = load_items_from_state(
                state_path,
                max_candidates=args.max_candidates,
            )
            if not args.query:
                args.query = state.get("query") or ""
            if not args.traits:
                args.traits = state_traits(state)
        else:
            items = load_items(args.in_path)
            if args.max_candidates:
                items = items[: args.max_candidates]
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not args.query:
        print("error: --query required unless --state has query", file=sys.stderr)
        return 2

    if not items:
        print("error: no input items", file=sys.stderr)
        return 2

    estimate_seconds = estimate_rerank_seconds(len(items), args.concurrency)

    if args.dry_run:
        for item in items:
            prompt = build_user_prompt(args.query, args.traits, item)
            sys.stderr.write(f"--- {item.id} ---\n{prompt}\n\n")
        sys.stderr.write(
            f"rerank: dry-run items={len(items)} concurrency={args.concurrency} "
            f"estimated={estimate_seconds}s profile_scope=full\n"
        )
        return 0

    if not args.api_key:
        print("error: --api-key or OPENAI_API_KEY required", file=sys.stderr)
        return 2

    sys.stderr.write(
        f"rerank: starting items={len(items)} concurrency={args.concurrency} "
        f"estimated={estimate_seconds}s note=LLM filtering+reranking can take 2-3 minutes; do not cancel while this step is running\n"
    )
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

    artifacts: dict[str, Any] = {}
    if state_path and state is not None:
        out_dir = artifact_dir(state_path, state) / "llm_rerank_candidates"
        csv_path = out_dir / "query_results.csv"
        raw_jsonl_path = out_dir / "raw_rerank_results.jsonl"
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        query_result_rows = build_query_result_rows(
            results,
            state=state,
            query=args.query,
            created_at=created_at,
        )
        write_query_results_csv(csv_path, query_result_rows)
        if args.dump_debug:
            write_results(results, str(raw_jsonl_path))
        ordered_ids = [row["person_id"] for row in query_result_rows]
        artifacts = {
            "query_results_csv": str(csv_path),
        }
        if args.dump_debug:
            artifacts["raw_rerank_results_jsonl"] = str(raw_jsonl_path)
        output = {
            "model": args.model,
            "concurrency": args.concurrency,
            "estimated_seconds": estimate_seconds,
            "ranked_count": len(results),
            "ranked_candidate_ids": ordered_ids,
            "profile_scope": "full",
            "artifacts": artifacts,
        }
        if args.write_state:
            record_state_step(state_path, state, output, int((time.monotonic() - started) * 1000))
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        write_results(results, args.out_path)

    ok = sum(1 for r in results if r.error is None)
    failed = len(results) - ok
    sys.stderr.write(
        f"rerank: items={len(results)} concurrency={args.concurrency} "
        f"ok={ok} failed={failed} elapsed={elapsed:.2f}s estimated={estimate_seconds}s\n"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
