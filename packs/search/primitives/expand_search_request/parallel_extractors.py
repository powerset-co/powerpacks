"""Parallel domain-specific query extractors.

Ported from network-search-api/api_v2/search/query/ extractors.
Uses the openai Python SDK (already a powerpacks dep) instead of langchain.
Each extractor is a system prompt + structured JSON response, run in parallel.

Usage:
    from parallel_extractors import expand_query_parallel
    result = await expand_query_parallel("founders backed by sequoia", api_key="...")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import openai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts — ported verbatim from network-search-api/prompts/
# ---------------------------------------------------------------------------

# We import the prompt text from separate files to keep this module readable.
# Each prompt is the exact text from the app repo.
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text()
    raise FileNotFoundError(f"Missing prompt: {path}")


# Default models per extractor (matching app defaults)
EXTRACTOR_MODELS = {
    "temporal": "gpt-4.1",
    "company": "gpt-5.4",
    "location": "gpt-4.1",
    "education": "gpt-4.1",
    "seniority": "gpt-4.1",
    "social": "gpt-4.1",
    "role": "gpt-4.1",
}


# ---------------------------------------------------------------------------
# Role extractor prompt (inline — the app uses a complex agent, we simplify)
# ---------------------------------------------------------------------------

ROLE_EXTRACTION_PROMPT = """You are an expert at extracting role/title information from people search queries.

Extract ONLY role-related information. Return JSON with:

### semantic_query
Dense retrieval prose (2-3 sentences) describing what the target person does,
their responsibilities, skills, and profile evidence. NOT a title phrase.
Omit only for pure hard-filter searches with no role intent.

### bm25_queries
Title/keyword aliases for BM25 matching. ALWAYS populate when there is role intent.
Examples: ["software engineer", "SWE", "backend engineer"] or
["founder", "co-founder", "cofounder", "founding CEO"].

### role_ids
Only for well-known canonical roles:
- "founder" for founder/cofounder queries
- Do NOT use for general roles

### is_current_role / is_current_company
- Set true when "current", "currently", "now at" is explicit
- Set false ONLY when "previously", "formerly", "ex-", "past", "used to" WITHOUT a date range
- Leave null when ambiguous
- Leave null when there is an explicit date range (position_after_date / position_before_date)
  because the date window already constrains the time period
- For "ROLE at COMPANY" recruiting queries with no date range, default both to true
- "who worked at X between 2020 and 2022" → null (date range handles it)
- "who worked at X" with no dates → null (ambiguous)

Return JSON:
{
  "semantic_query": null,
  "bm25_queries": [],
  "role_ids": [],
  "is_current_role": null,
  "is_current_company": null
}

IMPORTANT:
- semantic_query must be 2-3 sentences of dense prose, NOT a title
- bm25_queries must include common title variations
- Do NOT include seniority, location, company, education — those are extracted separately
"""


# ---------------------------------------------------------------------------
# Single extractor call
# ---------------------------------------------------------------------------

async def _extract(
    client: openai.AsyncOpenAI,
    name: str,
    system_prompt: str,
    query: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Run one extractor via OpenAI chat completion."""
    model = model or EXTRACTOR_MODELS.get(name, "gpt-4o-mini")
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=30,
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as e:
        logger.error(f"[{name}] extraction failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# Merge extractor results → role_search_filters
# ---------------------------------------------------------------------------

def _merge(
    role: dict[str, Any],
    company: dict[str, Any],
    location: dict[str, Any],
    education: dict[str, Any],
    temporal: dict[str, Any],
    seniority: dict[str, Any],
    social: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    """Merge parallel extractor outputs into a single role_search_filters dict."""
    filters: dict[str, Any] = {}

    # Role
    if role.get("semantic_query"):
        filters["semantic_query"] = role["semantic_query"]
    if role.get("bm25_queries"):
        filters["bm25_queries"] = role["bm25_queries"]
    if role.get("role_ids"):
        filters["role_ids"] = role["role_ids"]
    if role.get("is_current_role") is not None:
        filters["is_current_role"] = role["is_current_role"]
    if role.get("is_current_company") is not None:
        filters["is_current_company"] = role["is_current_company"]

    # Company
    if company.get("company_names"):
        filters["company_names"] = company["company_names"]
    if company.get("company_semantic_queries"):
        csq = company["company_semantic_queries"]
        # Ensure it's a list of strings
        if isinstance(csq, str):
            csq = [csq]
        filters["company_semantic_queries"] = csq
    if company.get("investors"):
        filters["investor_names"] = company["investors"]
    if company.get("entity_types"):
        # Fix common mistakes
        et = [("venture_backed_startup" if t == "startup" else t) for t in company["entity_types"]]
        filters["entity_types"] = et
    if company.get("sector_types"):
        filters["sector_types"] = company["sector_types"]
    if company.get("funding_stage_min"):
        filters["funding_stage_min"] = company["funding_stage_min"]
    if company.get("funding_stage_max"):
        filters["funding_stage_max"] = company["funding_stage_max"]
    if company.get("funding_amount_min") is not None:
        filters["funding_amount_min"] = company["funding_amount_min"]
    if company.get("funding_amount_max") is not None:
        filters["funding_amount_max"] = company["funding_amount_max"]
    if company.get("headcount_min") is not None:
        filters["headcount_min"] = company["headcount_min"]
    if company.get("headcount_max") is not None:
        filters["headcount_max"] = company["headcount_max"]
    if company.get("valuation_min") is not None:
        filters["valuation_min"] = company["valuation_min"]
    if company.get("valuation_max") is not None:
        filters["valuation_max"] = company["valuation_max"]
    if company.get("founded_year_min") is not None:
        filters["founded_year_min"] = int(company["founded_year_min"])
    if company.get("founded_year_max") is not None:
        filters["founded_year_max"] = int(company["founded_year_max"])
    if company.get("last_funding_before"):
        filters["last_funding_before"] = company["last_funding_before"]
    if company.get("last_funding_after"):
        filters["last_funding_after"] = company["last_funding_after"]
    if company.get("yc_batches"):
        filters["yc_batches"] = company["yc_batches"]
    if company.get("technology_types"):
        filters["technology_types"] = company["technology_types"]
    if company.get("customer_types"):
        filters["customer_types"] = company["customer_types"]

    # Auto-add company_sector_strategy when both semantic + sector present
    if filters.get("company_semantic_queries") and filters.get("sector_types"):
        filters.setdefault("company_sector_strategy", "soft_union")

    # Location (person)
    for key in ("cities", "states", "metro_areas", "countries", "macro_regions"):
        vals = location.get(key)
        if vals:
            filters[key] = vals
    # Location (company)
    for key in ("company_cities", "company_states", "company_metro_areas", "company_countries", "company_macro_regions"):
        vals = location.get(key)
        if vals:
            filters[key] = vals

    # Education
    if education.get("schools"):
        filters["education_names"] = education["schools"]
    if education.get("degree_levels"):
        # Keep title-case to match TurboPuffer degree_normalized values
        # (Bachelors, Masters, MBA, PhD, MD, JD)
        filters["degree_levels"] = education["degree_levels"]
    if education.get("fields_of_study"):
        filters["fields_of_study"] = education["fields_of_study"]
    if education.get("education_op") and education["education_op"] != "or":
        filters["education_op"] = education["education_op"]

    # Temporal
    if temporal.get("position_start_year") is not None:
        filters["position_after_date"] = str(temporal["position_start_year"])
    if temporal.get("position_end_year") is not None:
        filters["position_before_date"] = str(temporal["position_end_year"])
    if temporal.get("graduation_year_min") is not None:
        filters["graduation_year_min"] = int(temporal["graduation_year_min"])
    if temporal.get("graduation_year_max") is not None:
        filters["graduation_year_max"] = int(temporal["graduation_year_max"])
    if temporal.get("age_min") is not None:
        filters["age_min"] = int(temporal["age_min"])
    if temporal.get("age_max") is not None:
        filters["age_max"] = int(temporal["age_max"])
    if temporal.get("years_experience_min") is not None:
        filters["years_experience_min"] = int(temporal["years_experience_min"])
    if temporal.get("years_experience_max") is not None:
        filters["years_experience_max"] = int(temporal["years_experience_max"])

    # Seniority
    if seniority.get("seniority_bands"):
        # Normalize: "vice-president" → "vice_president"
        bands = [b.replace("-", "_") for b in seniority["seniority_bands"]]
        filters["seniority_bands"] = bands

    # Social
    for key in ("x_followers_min", "x_followers_max", "li_followers_min", "li_followers_max",
                "li_connections_min", "li_connections_max", "ig_followers_min", "ig_followers_max"):
        val = social.get(key)
        if val is not None:
            filters[key] = int(val)

    # If date range is set, drop is_current flags — the date window handles time scoping.
    # is_current=false + date range causes the prefilter to look for non-current positions
    # which is almost never what the user wants when they specify explicit years.
    if filters.get("position_after_date") or filters.get("position_before_date"):
        filters.pop("is_current_role", None)
        filters.pop("is_current_company", None)

    # Strip empty/null values
    filters = {k: v for k, v in filters.items() if v is not None and v != [] and v != ""}

    return filters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def expand_query_parallel(
    query: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Run all extractors in parallel and merge results.

    Returns the same shape as the single-prompt expand_search_request:
    {
        "intent_type": "role_search",
        "source_type": "query",
        "normalized_query": "...",
        "vertical": "people_by_role",
        "role_search_filters": { ... },
        "notes": [...],
        "extractor_timings": { ... }
    }
    """
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base = api_base or os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    if not base.endswith("/v1"):
        base = base.rstrip("/") + "/v1"

    client = openai.AsyncOpenAI(api_key=key, base_url=base)

    # Load prompts
    prompts = {
        "temporal": _load_prompt("temporal"),
        "company": _load_prompt("company"),
        "location": _load_prompt("location"),
        "education": _load_prompt("education"),
        "seniority": _load_prompt("seniority"),
        "social": _load_prompt("social"),
        "role": ROLE_EXTRACTION_PROMPT,
    }

    # Fan out all extractors in parallel
    started = time.monotonic()
    timings: dict[str, float] = {}

    async def timed_extract(name: str) -> dict[str, Any]:
        t0 = time.monotonic()
        result = await _extract(
            client, name, prompts[name], query,
            model=model_override or EXTRACTOR_MODELS.get(name),
        )
        timings[name] = round(time.monotonic() - t0, 3)
        return result

    role, company, location, education, temporal, seniority, social = await asyncio.gather(
        timed_extract("role"),
        timed_extract("company"),
        timed_extract("location"),
        timed_extract("education"),
        timed_extract("temporal"),
        timed_extract("seniority"),
        timed_extract("social"),
    )

    total_ms = int((time.monotonic() - started) * 1000)

    # Merge
    filters = _merge(role, company, location, education, temporal, seniority, social, query)

    return {
        "intent_type": "role_search",
        "source_type": "query",
        "normalized_query": query,
        "vertical": "people_by_role",
        "role_search_filters": filters,
        "notes": [],
        "extractor_timings": timings,
        "total_ms": total_ms,
    }
