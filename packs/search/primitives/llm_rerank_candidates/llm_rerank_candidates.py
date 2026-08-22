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
- `--model NAME` : chat completion model (default gpt-5.6-luna)
- `--reasoning-effort LEVEL` : reasoning effort for supported models (default medium)
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
      "trait_scores": {
        "<trait>": {"score": 0.0..1.0, "reason": "...", "confidence": 0.0..1.0}
      },
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
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI


PRIMITIVES_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
    sys.path.insert(0, str(_path))

from token_accounting import count_chat_prompt_tokens, summarize_token_counts  # noqa: E402
from openai_client import make_async_openai_client  # noqa: E402


DEFAULT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com")
DEFAULT_MODEL = os.environ.get("LLM_RERANK_MODEL", "gpt-5.6-luna")
DEFAULT_REASONING_EFFORT = os.environ.get("LLM_RERANK_REASONING_EFFORT", "medium")
DEFAULT_CONCURRENCY = int(os.environ.get("LLM_RERANK_CONCURRENCY", os.environ.get("SEARCH_V2_RERANK_MAX_CONCURRENT", "400")))
DEFAULT_SECONDS_PER_WAVE = int(os.environ.get("LLM_RERANK_SECONDS_PER_WAVE", "30"))


def load_system_prompt(path: str | None) -> tuple[str, str]:
    prompt = Path(path).read_text(encoding="utf-8") if path else SYSTEM_PROMPT
    if not prompt.strip():
        raise ValueError("rerank system prompt must not be empty")
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def parse_evaluation_traits(value: str | None) -> list[dict[str, str]]:
    """Parse a canonical evaluation trait list from JSON, @file, or a JSON file path."""
    if not value:
        return []
    source = value
    if value.startswith("@"):
        source = Path(value[1:]).read_text(encoding="utf-8")
    else:
        candidate = Path(value)
        try:
            if candidate.is_file():
                source = candidate.read_text(encoding="utf-8")
        except OSError:
            pass
    parsed = json.loads(source)
    raw: Any = parsed
    if isinstance(parsed, dict):
        raw = parsed.get("traits", parsed)
        if isinstance(raw, dict):
            raw = [*(raw.get("must_have") or []), *(raw.get("nice_to_have") or [])]
    if not isinstance(raw, list):
        raise ValueError("evaluation traits JSON must be a list or an object containing traits")
    traits: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            trait = {"value": item, "temporal": "all", "meaning": "general"}
        elif isinstance(item, dict):
            text = str(item.get("value") or item.get("trait") or "").strip()
            if not text:
                continue
            trait = {
                "value": text,
                "temporal": str(item.get("temporal") or "all"),
                "meaning": str(item.get("meaning") or item.get("tier") or "general"),
            }
        else:
            continue
        if trait["value"].strip():
            traits.append(trait)
    if not traits:
        raise ValueError("evaluation traits JSON contains no usable traits")
    return traits


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

=== EVIDENCE CALIBRATION ===
Treat scores as ranking confidence, not proof. Reserve 0.00-0.29 for clear non-matches, contradictory evidence, or only superficial exposure.
When current role, responsibilities, seniority, and organization context strongly imply a trait that profiles rarely state explicitly, score the reasonable inference 0.60-0.89 and say it is inferred; do not require the exact query words.
Use 0.30-0.59 for genuinely ambiguous or partial evidence and 0.90-1.00 for direct evidence. Do not infer from organization context alone or invent facts absent from the profile.
You may use common knowledge of a well-known organization's broad function or stature only to interpret a person's role; never invent exact metrics or let organization alone substitute for role evidence.

=== REQUIRED JSON OUTPUT ===

Return exactly one JSON object with this shape:
{
  "score": 0.00,
  "verdict": "include" or "exclude",
  "confidence": 0.00,
  "overall_reasoning": "A concise explanation of the overall score grounded in profile evidence.",
  "trait_scores": [
    {
      "trait": "Exact quoted trait text from the input",
      "score": 0.00,
      "reason": "Concise profile evidence for this score, or what evidence is missing.",
      "confidence": 0.00
    }
  ]
}

Output one trait_scores entry per input trait, in the same order, using the exact
quoted trait text. Every trait entry must have its own reason; do not repeat the
overall reasoning as a substitute. Explain observable evidence and reasonable
inferences concisely. Do not reveal hidden chain-of-thought or invent evidence.
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
    trait_scores: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        "response_format": {"type": "json_object"},
    }
    if supports_custom_temperature(model):
        kwargs["temperature"] = 0
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


def _bounded_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _reason_text(value: Any) -> str:
    """Normalize current and legacy reason/evidence shapes to display text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(
            text for item in value if (text := _reason_text(item))
        )
    if isinstance(value, dict):
        for key in ("reason", "evidence", "text", "summary"):
            if key in value:
                return _reason_text(value[key])
    return ""


def normalize_trait_score(
    value: Any,
    *,
    fallback_score: float,
    fallback_reason: str,
    fallback_confidence: float,
) -> dict[str, Any]:
    """Return the QueryResultV2-compatible per-trait score object.

    Older results used a bare number, ``reasons`` arrays, or list entries. The
    workbench needs one stable object without discarding richer new responses.
    """
    if isinstance(value, dict):
        raw_score = value.get("score", value.get("match_score", fallback_score))
        raw_reason = value.get(
            "reason",
            value.get("evidence", value.get("reasons", fallback_reason)),
        )
        raw_confidence = value.get("confidence", fallback_confidence)
    else:
        raw_score = value
        raw_reason = fallback_reason
        raw_confidence = fallback_confidence
    return {
        "score": _bounded_float(raw_score, fallback_score),
        "reason": _reason_text(raw_reason) or fallback_reason,
        "confidence": _bounded_float(raw_confidence, fallback_confidence),
    }


def parse_verdict(
    raw_response: dict[str, Any],
    traits: list[dict[str, str]],
) -> tuple[float, str, str, float, dict[str, dict[str, Any]]]:
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
    reason = _reason_text(
        parsed.get("overall_reasoning", parsed.get("reasoning", parsed.get("reason", "")))
    )
    confidence = _bounded_float(parsed.get("confidence", 0.0))
    trait_scores_raw = parsed.get("trait_scores") or parsed.get("traits") or {}
    raw_by_trait: dict[str, Any] = {}
    if isinstance(trait_scores_raw, dict):
        for key, value in trait_scores_raw.items():
            raw_by_trait[str(key)] = value
    elif isinstance(trait_scores_raw, list):
        for item in trait_scores_raw:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("trait")
                or item.get("trait_name")
                or item.get("name")
                or item.get("key")
            )
            if not key:
                continue
            raw_by_trait[str(key)] = item
    raw_score_values = []
    for value in raw_by_trait.values():
        raw_score = value.get("score") if isinstance(value, dict) else value
        try:
            raw_score_values.append(float(raw_score))
        except (TypeError, ValueError):
            continue
    score_raw = parsed.get("score", parsed.get("final_score", parsed.get("overall_trait_score")))
    if score_raw is None and raw_score_values:
        score_raw = sum(raw_score_values) / len(raw_score_values)
    score = _bounded_float(score_raw)
    verdict_raw = parsed.get("verdict")
    verdict = str(verdict_raw).lower() if verdict_raw is not None else ""
    if verdict not in ("include", "exclude"):
        verdict = "include" if score >= 0.5 else "exclude"
    # Canonicalize requested trait keys while accepting old casing and shapes.
    casefold_keys = {key.casefold(): key for key in raw_by_trait}
    trait_scores: dict[str, dict[str, Any]] = {}
    for trait in traits:
        trait_name = trait["value"]
        source_key = trait_name if trait_name in raw_by_trait else casefold_keys.get(trait_name.casefold())
        value = raw_by_trait.get(source_key, score) if source_key is not None else score
        trait_scores[trait_name] = normalize_trait_score(
            value,
            fallback_score=score,
            fallback_reason=reason,
            fallback_confidence=confidence,
        )
    return score, verdict, reason, confidence, trait_scores


def supports_reasoning_effort(model: str) -> bool:
    normalized = str(model or "").lower().split("/")[-1]
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def supports_custom_temperature(model: str) -> bool:
    normalized = str(model or "").lower().split("/")[-1]
    return not normalized.startswith(("gpt-5", "o1", "o3", "o4"))


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
    system_prompt: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    include_prompt: bool,
) -> RerankResult:
    user_prompt = build_user_prompt(query, traits, item)
    prompt_tokens_estimate = count_chat_prompt_tokens(
        model,
        [
            {"role": "system", "content": system_prompt},
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
    trait_scores: dict[str, dict[str, Any]] = {}

    async with semaphore:
        attempt = 0
        while True:
            try:
                raw_response = await call_chat_completion(
                    client,
                    model,
                    system_prompt,
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
    system_prompt: str,
    concurrency: int,
    timeout: int,
    max_retries: int,
    include_prompt: bool,
) -> list[RerankResult]:
    semaphore = asyncio.Semaphore(concurrency)
    client = make_async_openai_client(api_key, api_base, timeout=timeout, max_retries=0)
    try:
        tasks = [
            rerank_one(
                item,
                query=query,
                traits=traits,
                client=client,
                model=model,
                reasoning_effort=reasoning_effort,
                system_prompt=system_prompt,
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


def latest_step(state: dict[str, Any], step_id: str) -> dict[str, Any] | None:
    for step in reversed(state.get("steps", [])):
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    return None


def state_frontier_ids(state: dict[str, Any]) -> list[str]:
    rerank = step_output(state, "llm_rerank_candidates")
    ids = rerank.get("ranked_candidate_ids")
    if isinstance(ids, list):
        return list(dict.fromkeys(str(pid) for pid in ids if pid))
    llm_filter = step_output(state, "llm_filter_candidates")
    # An explicit empty passed frontier is authoritative. Falling through to
    # retrieval IDs would resurrect candidates the filter rejected.
    ids = llm_filter.get("passed_candidate_ids")
    if isinstance(ids, list):
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


def valid_empty_filtered_state(state: dict[str, Any]) -> bool:
    """Return true only for an explicit completed zero-result filter frontier."""
    filter_step = latest_step(state, "llm_filter_candidates")
    hydrate_step = latest_step(state, "hydrate_people")
    if not filter_step or filter_step.get("status") != "completed":
        return False
    if not hydrate_step or hydrate_step.get("status") != "completed":
        return False
    filter_output = filter_step.get("output")
    hydrate_output = hydrate_step.get("output")
    if not isinstance(filter_output, dict) or not isinstance(hydrate_output, dict):
        return False
    passed_ids = filter_output.get("passed_candidate_ids")
    passed_count = filter_output.get("passed_count")
    if not isinstance(passed_ids, list) or passed_ids or passed_count != 0:
        return False
    # A completed hydrate handoff must still be present. This keeps a missing
    # or malformed state from being mistaken for a legitimate empty search.
    return bool(
        hydrate_output.get("profiles_path")
        or isinstance(hydrate_output.get("profiles"), list)
    )


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
            trait: normalize_trait_score(
                value,
                fallback_score=result.score,
                fallback_reason=result.reason,
                fallback_confidence=result.confidence,
            )
            for trait, value in per_trait.items()
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
    parser.add_argument("--evaluation-query",
                        help="Canonical query/brief used only for evaluation; retrieval remains state.query")
    parser.add_argument("--evaluation-traits-json",
                        help="Canonical traits as JSON, @file, or JSON file path")
    parser.add_argument("--system-file",
                        help="Reviewed rerank system prompt; the exact prompt is snapshotted and hashed")
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

    # Normalize explicit canonical traits before falling back to legacy repeated strings/state.
    try:
        evaluation_traits = parse_evaluation_traits(args.evaluation_traits_json)
        if evaluation_traits:
            args.traits = evaluation_traits
        elif args.traits and isinstance(args.traits[0], str):
            args.traits = [{"value": t, "temporal": "all", "meaning": "general"} for t in args.traits]
        system_prompt, system_sha256 = load_system_prompt(args.system_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

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
            retrieval_query = state.get("query") or ""
            if not args.query:
                args.query = retrieval_query
            if not args.traits:
                args.traits = state_traits(state)
        else:
            items = load_items(args.in_path)
            retrieval_query = args.query or ""
            if args.max_candidates:
                items = items[: args.max_candidates]
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    evaluation_query = args.evaluation_query or args.query
    if not evaluation_query:
        print("error: --query required unless --state has query", file=sys.stderr)
        return 2

    empty_filtered_state = bool(
        state_path is not None
        and state is not None
        and valid_empty_filtered_state(state)
    )
    if not items and not empty_filtered_state:
        print("error: no input items", file=sys.stderr)
        return 2

    estimate_seconds = estimate_rerank_seconds(len(items), args.concurrency)

    if args.dry_run:
        for item in items:
            prompt = build_user_prompt(evaluation_query, args.traits, item)
            sys.stderr.write(f"--- {item.id} ---\n{prompt}\n\n")
        sys.stderr.write(
            f"rerank: dry-run items={len(items)} concurrency={args.concurrency} "
            f"estimated={estimate_seconds}s profile_scope=full\n"
        )
        return 0

    started = time.monotonic()
    if items:
        if not args.api_key:
            print("error: --api-key or OPENAI_API_KEY required", file=sys.stderr)
            return 2
        sys.stderr.write(
            f"rerank: starting items={len(items)} concurrency={args.concurrency} "
            f"estimated={estimate_seconds}s note={rerank_status_note(estimate_seconds)}\n"
        )
        results = asyncio.run(
            rerank_all(
                items,
                query=evaluation_query,
                traits=args.traits,
                api_base=args.api_base,
                api_key=args.api_key,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                system_prompt=system_prompt,
                concurrency=args.concurrency,
                timeout=args.timeout,
                max_retries=args.max_retries,
                include_prompt=args.include_prompt,
            )
        )
    else:
        results = []
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
        prompt_path = out_dir / f"system_prompt.{system_sha256[:12]}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(system_prompt, encoding="utf-8")
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        query_result_rows = build_query_result_rows(
            results,
            state=state,
            query=retrieval_query,
            created_at=created_at,
        )
        write_query_results_csv(csv_path, query_result_rows)
        if args.dump_debug:
            write_results(results, str(raw_jsonl_path))
        ordered_ids = [row["person_id"] for row in query_result_rows]
        artifacts = {
            "query_results_csv": str(csv_path),
            "system_prompt": str(prompt_path),
        }
        if args.dump_debug:
            artifacts["raw_rerank_results_jsonl"] = str(raw_jsonl_path)
        output = {
            "model": args.model,
            "reasoning_effort": args.reasoning_effort if supports_reasoning_effort(args.model) else None,
            "retrieval_query": retrieval_query,
            "evaluation_query": evaluation_query,
            "evaluation_traits": args.traits,
            "system_prompt_sha256": system_sha256,
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
    return 0 if ok or not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
