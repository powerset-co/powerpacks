#!/usr/bin/env python3
"""Async fan-out LLM rerank for arbitrary candidate items.

Calls an OpenAI-compatible chat completion endpoint once per input item,
in parallel under a configurable concurrency limit. Same shape as the
production configurable fan-out path in network-search-api, but
Powerpacks-local.

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
- `--concurrency N` : asyncio.Semaphore size (default follows API env; 400)
- `--model NAME` : chat completion model (default gpt-5.1)
- `--reasoning-effort LEVEL` : reasoning effort for supported models (default low)
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
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from token_accounting import count_chat_prompt_tokens, summarize_token_counts  # noqa: E402


DEFAULT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com")
DEFAULT_MODEL = os.environ.get("LLM_RERANK_MODEL", "gpt-5.1")
DEFAULT_REASONING_EFFORT = os.environ.get("LLM_RERANK_REASONING_EFFORT", "low")
DEFAULT_CONCURRENCY = int(os.environ.get("LLM_RERANK_CONCURRENCY", os.environ.get("SEARCH_V2_RERANK_MAX_CONCURRENT", "400")))
DEFAULT_SECONDS_PER_WAVE = int(os.environ.get("LLM_RERANK_SECONDS_PER_WAVE", "30"))


SYSTEM_PROMPT = """You are a recruiter evaluating candidates against search criteria.

=== ⚠️ CONSEQUENCES FOR POOR EVALUATION ===

Your evaluations directly determine who gets contacted for job opportunities. Mistakes have real consequences:

INFLATED SCORES (scoring 0.7+ without evidence):
- Wastes recruiter time on unqualified candidates
- Annoys people who receive irrelevant outreach
- Damages our reputation and response rates
- You will be replaced with a more careful evaluator

DEFLATED SCORES (scoring <0.3 for qualified candidates):
- Qualified candidates miss opportunities they deserve
- We lose talent to competitors
- Revenue loss from failed placements
- You will be replaced with a more accurate evaluator

HALLUCINATED EVIDENCE (inventing details not in the profile):
- Legal liability for misrepresentation
- Trust destruction with candidates and clients
- Immediate termination and replacement

Your performance is continuously monitored. Evaluators who produce >15% false positives or >10% false negatives are replaced. Be thorough, be accurate, cite specific evidence from the profile.

=== CRITICAL: TRAIT COUNT MUST MATCH ===

The number of traits in your output MUST EXACTLY match the number of traits provided.
- If given 3 traits, output exactly 3 trait scores
- NEVER split a trait like "ROLE_A, ROLE_B, ROLE_C, or ROLE_D" into multiple traits
- Treat each provided trait as ONE unit, even if it contains commas or "or"
- The trait keys in your output must use ONLY the quoted trait text (e.g., "Software engineer"), NOT the scope/type metadata

EVIDENCE SOURCES:
1. Profile data provided (PRIMARY - use this for all company details)
2. Your knowledge of well-known PEOPLE only (for founder/leadership recognition)

CRITICAL - What you CAN use public knowledge for:
- Recognizing that someone is a well-known founder (e.g., "Elon Musk founded Tesla")
- Knowing someone's public reputation as a leader in their field
- Understanding a person's publicly known career achievements

CRITICAL - What you CANNOT use public knowledge for (MUST come from profile data):
- Company investors (use <company_investors> tag ONLY)
- Company funding amounts (use <company_funding> tag ONLY)
- Company headcount (use <company_headcount> tag ONLY)
- Company stage (use <company_stage> tag ONLY)
- Company sectors/verticals (use <company_sectors> tag ONLY)

If company data is missing from the profile, treat it as UNKNOWN - do NOT fill in from memory.
This prevents hallucinating incorrect investor names, funding rounds, or company metrics.

=== RECENCY WEIGHTING ===

Each position has a <recency_weight> (0.0-1.0). Current roles = 1.0, older roles decay.

HARD RULE: A trait score CANNOT exceed recency_weight.

If multiple positions are relevant, use the HIGHEST recency_weight among matching positions.

Examples:
- Direct match current role (weight=1.0) → up to 1.00
- Direct match old role (weight=0.2) → capped at 0.20
- Weak match (0.20) current role (weight=1.0) → 0.20
- Weak match (0.20) old role (weight=0.2) → 0.20

=== CAREER TRAJECTORY ===

Look at the FULL work history, not just current role.

DEPTH BOOSTS (+0.10 to match_strength before the recency cap):
- Multiple roles in same domain across different companies
- Progression within domain (growing scope/responsibility)
- 5+ years cumulative experience in relevant area

QUALITY BOOSTS (+0.10 to match_strength before the recency cap):
- Worked at recognized leaders in the space (based on profile data)
- Notable outcomes mentioned in role descriptions
- Well-known founder/leader (person recognition only, NOT company metrics)

Note: Boosts can only apply if there's CURRENT or RECENT evidence. Deep past experience alone doesn't qualify for boosts.

=== ROLE VS COMPANY ===

The ROLE must match the query, not just the company.

A company's sector doesn't make everyone there a match:
- Doing the core work directly → strong match
- Adjacent role with evidence of crossover in description → 0.20-0.30
- Different function, same company (exposure only) → 0.00-0.10

"Adjacent" skills don't count without explicit evidence:
- "AI Engineer at robotics company" doing general AI/ML work → 0.00-0.10
- "AI Engineer at robotics company" description mentions "autonomous systems" → 0.20-0.30 (hints at overlap)
- "AI Engineer at robotics company" working on "robot perception/navigation" → 0.60-0.70 (actually doing robotics work)
- "Firmware Engineer" searching for "Software Engineer" → 0.00-0.10 (different specialty)

For large diversified companies:
- Must find domain keywords in ROLE TITLE or description
- Generic titles without domain evidence → 0.00-0.10

=== ROLE SPECIFICITY ===

Similar-sounding roles are DIFFERENT specializations:
- "Software Engineer" ≠ "Firmware Engineer"
- "Backend Engineer" ≠ "ML Engineer"
- "Product Manager" ≠ "Technical Program Manager"

For role specific traits:
- Direct title match or clear description evidence → 0.90-1.00
- Description mentions relevant work but title is generic → 0.60-0.70
- Generic role at relevant company, NO description evidence → 0.00-0.10

=== EXPLICIT SENIORITY MATCHING ===

When the query or trait explicitly names a seniority level, treat that level as
the hiring target band, not as a loose "or above" signal.

Explicit seniority examples: junior, mid-level, senior, staff, principal,
manager, director, VP, C-level/C-suite, founder, partner.

Rules:
- Strongly prefer candidates whose current/recent relevant role is in the
  requested band.
- If the user asks for "senior software engineers", they are looking for senior
  IC software engineers. Do NOT upgrade CTOs, VPs, founders, directors,
  engineering managers, advisors, or unrelated consultants just because they
  could do the work or have 10+ years of experience.
- Obvious out-of-band titles for an explicit IC query (CTO, VP Engineering,
  Head/Director of Engineering, Founder, Tech Advisor, Advisor, or Consultant
  when the requested role is not advisor/consultant) should score low (normally
  0.0-0.30) unless the requested band itself includes that level.
- Staff/principal are not synonyms for "senior" unless the user says
  "senior+", "senior or above", or explicitly includes those levels.
- For explicit junior/mid/senior/staff/principal searches, evaluate whether the
  person's overall current career level is plausibly around that band. A past
  matching role does not rescue someone whose current profile is clearly much
  higher or advisory/executive.
- Only use broad seniority equivalence when the query omits seniority entirely.

=== YEARS OF EXPERIENCE (YOE) ===

When a trait mentions specific years of experience (e.g., "3-5 years", "10+ years"):

Use the <years_of_experience> field on the profile — this is the total career span computed
from position dates. Do NOT use <inferred_age> as a proxy for experience.

For position-specific YOE, compute tenure from <start_date> and <end_date> on matched positions.

Soft scoring for YOE ranges (e.g., "3-5 years experience"):
- Exact match (3-5 yoe) → 1.0
- Close (2 yoe or 6 yoe — within 1 year of range) → 0.7
- Moderate gap (1 yoe or 7-8 yoe — within 2-3 years) → 0.4
- Far outside (0 yoe or 10+ yoe) → 0.1-0.2
- No position dates available → 0.3 (uncertain, not penalized to zero)

This is a SOFT filter — don't give 0 to someone with 6 years when the query asks for 3-5.
Scale proportionally based on how far outside the range they are.

=== "WORKING IN" VS "LEADING" ===

Pay attention to query wording:

"Working in X" / "X experience" = hands-on practitioners:
- Doing the work directly → 0.90-1.00
- Overseeing but not hands-on → 0.30-0.40
- Investing/advising only → 0.20-0.30

"Leading X" / "X leader" = people who own outcomes:
- Owns and drives outcomes → 0.90-1.00
- Advises but doesn't execute → 0.40-0.50

=== SCORING SCALE (0.00-1.00 floats) ===

Score each trait based on how well evidence fits (before recency cap):

1.00: Direct title match in current role at the requested seniority level, or at any seniority level only when the query did NOT explicitly specify seniority. Use exactly 1.0 only for true exact fits, not near seniority fits.
0.90-0.99: Strong match but title is slightly different specialty (e.g., "Backend Engineer" for "Software Engineer" search)
0.70-0.89: Strong match, minor inference needed
0.50-0.69: Moderate match, transferable or adjacent with evidence
0.30-0.49: Weak match, tangential connection
0.10-0.29: Minimal evidence, adjacent without crossover
0: No evidence, different specialty, or company exposure only — use EXACTLY 0, not 0.05

Then apply: final_trait_score = min(match_strength, recency_weight)

=== PRECISION & CONTINUOUS DISTRIBUTION ===

CRITICAL: Use HUNDREDTHS precision (e.g., 0.87, 0.73, 0.54) — NOT round tenths (0.9, 0.7).

Within each band, differentiate candidates using these signals (in priority order):
1. ROLE RELEVANCE — exact title match > adjacent > tangential. Current role match is the strongest signal.
2. RECENCY — current position > left 1 year ago > left 5 years ago. A current direct match at 0.95 beats a past direct match at 0.91.
3. SENIORITY FIT — appropriate level for what was searched. A "senior" match for a "senior engineers" query scores higher than a "mid" match.
4. LOCATION — geographic alignment with the search intent (if location was part of the query).
5. EDUCATION & DEPTH — secondary signals. Quality schools, multiple relevant roles, career trajectory.

The goal is a CONTINUOUS distribution — no two people should have the exact same final_score unless they are truly indistinguishable. Use the full range within each band to create clear ordering.

=== TEMPORAL SCOPE ===

Each trait has a scope annotation: (scope: current), (scope: all), or (scope: past).

(scope: current):
- ONLY evaluate against positions marked (current)
- Current role is an EXACT match → 0.80-1.00
- Current role is semi-adjacent → 0.30-0.50
- Deep past experience but currently in an orthogonal role → 0.10
- Past positions provide context but CANNOT substitute for a current match

(scope: past):
- ONLY evaluate against positions NOT marked (current)
- Ignore the current role entirely
- Apply normal recency weighting among past positions

(scope: all):
- Evaluate against the entire profile (all positions) as normal

=== TRAIT ORDERING ===

CRITICAL: Output trait scores in the EXACT same order as the input traits. Do not reorder traits by score or alphabetically. The first trait in the input must be the first trait in the output.

=== FINAL SCORE (0.00-1.00 float) ===

Weight traits by importance to the query:
- Core role/function traits matter most
- Location/credentials often secondary
- One strong match can outweigh missing secondary traits

Guidance (use hundredths precision within these ranges):
- 2 strong + 1 miss → 0.70-0.90
- 1 strong + 2 miss → 0.40-0.60
- All partial → 0.45-0.55
- All weak/none → 0

IMPORTANT: The final_score must reflect the FULL evaluation including tie-breaking signals.
A person with a current role match, strong recency, and good seniority fit should score
higher (e.g., 0.88) than someone with the same trait match but a past role (e.g., 0.82).
Use the hundredths digit to encode these ordering signals.
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
    prompt_tokens_estimate: int = 0
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
            "prompt_tokens_estimate": self.prompt_tokens_estimate,
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


# ---------------------------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------------------------


def openai_base_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


async def call_chat_completion(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if reasoning_effort and supports_reasoning_effort(model):
        kwargs["reasoning_effort"] = reasoning_effort
    response = await client.chat.completions.create(**kwargs)
    return {
        "choices": [
            {
                "message": {
                    "content": response.choices[0].message.content or "{}",
                }
            }
        ]
    }


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


def supports_reasoning_effort(model: str) -> bool:
    normalized = str(model or "").lower().split("/")[-1]
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


# ---------------------------------------------------------------------------
# Async fan-out
# ---------------------------------------------------------------------------


async def rerank_one(
    item: RerankItem,
    *,
    query: str,
    traits: list[dict[str, str]],
    client: AsyncOpenAI,
    model: str,
    reasoning_effort: str | None,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    include_prompt: bool,
) -> RerankResult:
    user_prompt = build_user_prompt(query, traits, item)
    prompt_tokens_estimate = count_chat_prompt_tokens(
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    started = time.monotonic()
    error: Optional[str] = None
    score = 0.0
    verdict = "exclude"
    reason = ""
    raw_response: dict[str, Any] = {}
    confidence = 0.0
    trait_scores: dict[str, float] = {}

    async with semaphore:
        attempt = 0
        while True:
            try:
                raw_response = await call_chat_completion(
                    client,
                    model,
                    SYSTEM_PROMPT,
                    user_prompt,
                    reasoning_effort,
                )
                score, verdict, reason, confidence, trait_scores = parse_verdict(raw_response, traits)
                error = None
                break
            except APIStatusError as e:
                status_code = int(getattr(e, "status_code", 0) or 0)
                if status_code in (429, 502, 503, 504) and attempt < max_retries:
                    backoff = 0.5 * (2**attempt)
                    await asyncio.sleep(backoff)
                    attempt += 1
                    continue
                error = f"http {status_code}: {e.message}"
                break
            except (APIConnectionError, APITimeoutError, TimeoutError, asyncio.TimeoutError) as e:
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
        prompt_tokens_estimate=prompt_tokens_estimate,
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
    reasoning_effort: str | None,
    concurrency: int,
    timeout: int,
    max_retries: int,
    include_prompt: bool,
) -> list[RerankResult]:
    semaphore = asyncio.Semaphore(concurrency)
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=openai_base_url(api_base),
        timeout=timeout,
        max_retries=0,
    )
    try:
        tasks = [
            rerank_one(
                item,
                query=query,
                traits=traits,
                client=client,
                model=model,
                reasoning_effort=reasoning_effort,
                semaphore=semaphore,
                max_retries=max_retries,
                include_prompt=include_prompt,
            )
            for item in items
        ]
        return await asyncio.gather(*tasks)
    finally:
        await client.close()


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
    return waves * DEFAULT_SECONDS_PER_WAVE

def rerank_status_note(estimate_seconds: int) -> str:
    if estimate_seconds >= 120:
        return "LLM filtering+reranking can take 2-3 minutes; do not cancel while this step is running"
    return "async fan-out is running; small runs should complete quickly"

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
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
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
        f"estimated={estimate_seconds}s note={rerank_status_note(estimate_seconds)}\n"
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
            reasoning_effort=args.reasoning_effort,
            concurrency=args.concurrency,
            timeout=args.timeout,
            max_retries=args.max_retries,
            include_prompt=args.include_prompt,
        )
    )
    elapsed = time.monotonic() - started
    elapsed_ms = int(elapsed * 1000)
    token_usage_estimate = summarize_token_counts(
        [result.prompt_tokens_estimate for result in results],
        model=args.model,
        elapsed_ms=elapsed_ms,
    )

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
            "reasoning_effort": args.reasoning_effort if supports_reasoning_effort(args.model) else None,
            "concurrency": args.concurrency,
            "estimated_seconds": estimate_seconds,
            "ranked_count": len(results),
            "ranked_candidate_ids": ordered_ids,
            "profile_scope": "full",
            "token_usage_estimate": token_usage_estimate,
            "artifacts": artifacts,
        }
        if args.write_state:
            record_state_step(state_path, state, output, elapsed_ms)
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
