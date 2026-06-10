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
ROOT = Path(__file__).resolve().parents[4]
ROLE_TAXONOMY_PATH = ROOT / "packs/search/data/roles/canonical_role_taxonomy.json"

CITY_ALIASES = {
    "sf": "San Francisco",
    "s.f.": "San Francisco",
    "nyc": "New York City",
}

FOUNDER_BM25_QUERIES = ["founder", "co-founder", "cofounder", "founding", "CEO", "Chief Executive Officer"]
FOUNDER_SEMANTIC_QUERY = (
    "founder who started, founded, or built a company from scratch, took entrepreneurial risk, founding team "
    "member, owns equity as a founder, made early strategic decisions, hired initial team, raised funding or "
    "bootstrapped"
)
CSUITE_EXPANSION = {
    "ceo": {
        "display": "Chief Executive Officer",
        "role_id": "chief_executive_officer",
        "bm25": ["CEO", "Chief Executive Officer", "president", "managing director"],
    },
    "cto": {
        "display": "Chief Technology Officer",
        "role_id": "chief_technology_officer",
        "bm25": ["CTO", "Chief Technology Officer", "SVP Engineering"],
    },
    "cfo": {
        "display": "Chief Financial Officer",
        "role_id": "chief_financial_officer",
        "bm25": ["CFO", "Chief Financial Officer", "head of finance"],
    },
    "cmo": {
        "display": "Chief Marketing Officer",
        "role_id": "chief_marketing_officer",
        "bm25": ["CMO", "Chief Marketing Officer", "VP Marketing"],
    },
    "coo": {
        "display": "Chief Operating Officer",
        "role_id": "chief_operating_officer",
        "bm25": ["COO", "Chief Operating Officer", "head of operations"],
    },
    "cpo": {
        "display": "Chief Product Officer",
        "role_id": "chief_product_officer",
        "bm25": ["CPO", "Chief Product Officer", "VP Product"],
    },
    "cro": {
        "display": "Chief Revenue Officer",
        "role_id": "chief_revenue_officer",
        "bm25": ["CRO", "Chief Revenue Officer", "head of revenue"],
    },
    "ciso": {
        "display": "Chief Information Security Officer",
        "role_id": "chief_information_security_officer",
        "bm25": ["CISO", "Chief Information Security Officer"],
    },
}
ROLE_ID_TITLE_INJECTIONS = {
    "ai_engineer": ["Member of Technical Staff"],
    "ml_engineer": ["Member of Technical Staff"],
    "software_engineer": ["Member of Technical Staff"],
    "researcher": ["Research Fellow", "Postdoctoral Fellow"],
    "ai_researcher": ["Research Scientist", "Research Fellow"],
    "chief_revenue_officer": ["CRO"],
    "chief_marketing_officer": ["CMO"],
    "chief_operating_officer": ["COO"],
    "chief_financial_officer": ["CFO"],
    "chief_product_officer": ["CPO"],
    "chief_technology_officer": ["CTO"],
    "chief_executive_officer": ["CEO"],
}

PERSON_LOCATION_PREFIXES = (
    "in",
    "based in",
    "located in",
    "lives in",
    "living in",
)

COMPANY_LOCATION_NOUNS = (
    "companies",
    "company",
    "startups",
    "startup",
    "firms",
    "firm",
    "employers",
    "employer",
)

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
    "trait_generation": "gpt-4.1",
}


# ---------------------------------------------------------------------------
# Role extractor prompt.
#
# This mirrors network-search-api's active RoleSearchAgentV2 path rather than the
# older role_extraction_v3 prompt. Prod runs RoleSearchAgentV2 to emit
# semantic_query, bm25_queries, role_ids, departments, and seniority, then applies
# deterministic founder/C-suite short-circuits and optional title clustering.
# Local DuckDB cannot run prod title clustering without a live title index, but it
# can use the same role-agent prompt/model and the same deterministic shortcuts.
# ---------------------------------------------------------------------------

ROLE_AGENT_SYSTEM_PROMPT = """You are a role search specialist. Given a search query, select the MOST PRECISE role_ids to retrieve matching people.

## ROLE TAXONOMY (175 functions across 22 departments)
{taxonomy}

## GENERIC ROLES (in "general" department)
The "general" department contains: analyst, associate, chief_executive_officer, consultant, coordinator, director, founder, head_of, manager, officer, owner, president, principal, specialist, vice_president
These are cross-departmental. IMPORTANT: director, vice_president, head_of, partner are SENIORITY LEVELS, not role functions. Do NOT put them in role_ids — use them in seniority instead.
C-suite role_ids (chief_executive_officer, chief_technology_officer, chief_financial_officer, etc.) ARE valid role functions — use them when the query specifically asks for C-level executives.

## SENIORITY BANDS (13 levels)
trainee, entry, junior, mid, senior, staff, principal, manager, director, vice_president, c_suite, partner, owner

## YOUR TASK
Select the best combination of:
- **role_ids**: Pick ONLY the role_ids that DIRECTLY match the query. Be PRECISE, not expansive. Typically 1-4 role_ids. The downstream title clustering module discovers adjacent roles automatically — your job is to hit the bullseye, not cast a wide net. ONLY use IDs from the taxonomy above.
- **departments**: Pick the relevant departments.
- **seniority**: Pick seniority bands if the query implies a level. Empty = all levels.
- **semantic_query**: 2-4 sentences describing what this role DOES. Rich, descriptive, covers responsibilities/skills/tools.
- **bm25_queries**: 5-15 diverse phrase keywords. Use stemmed phrases — "software engineer" matches all seniority variants. Focus on SYNONYMS and ADJACENT terms, not seniority prefixes.

## PRECISION RULES
- Return ONLY roles that someone searching for the query would actually want to see in results.
- "devops engineers" → devops_engineer, sre, maybe platform_engineer. NOT backend_engineer, qa_engineer, software_engineer — those are different jobs.
- "data scientists" → data_scientist, maybe ml_engineer. NOT data_engineer, data_analyst — those are different jobs.
- "ai engineers" → ai_engineer, ml_engineer. NOT data_engineer, data_analyst, data_architect — those are different jobs.
- "product managers" → product_manager. NOT program_manager, product_designer — those are different jobs.
- "software engineers" → software_engineer, backend_engineer, frontend_engineer, full_stack_engineer, mobile_engineer. These are SUBTYPES of the same job, so they belong.
- "founders" → role_ids=["founder"], seniority=[] (EMPTY — founders exist at all levels)
- "X leaders" → role_ids = DOMAIN functions + relevant C-suite (e.g., "data science leaders" → data_scientist, data_science_manager). seniority = [director, vice_president, c_suite]. Do NOT put director/VP/head_of in role_ids — those are seniority, not roles.
- "engineering leadership" → role_ids=[software_engineer, engineering_manager, chief_technology_officer], seniority=[director, vice_president, c_suite]
- "gtm leaders" → role_ids=[sales_manager, account_executive, marketing_manager, business_development, chief_revenue_officer, chief_marketing_officer], seniority=[director, vice_president, c_suite]
- "CTOs" or "chief technology officers" → role_ids=[chief_technology_officer], seniority=[] (the role IS the C-suite level)
- "CFOs at banks" → role_ids=[chief_financial_officer], seniority=[]
- NEVER output bare "partner" as a role_id. Resolve it from context.
- "partners at law firms" or "law firm partners" → role_ids=[attorney], seniority=[partner]
- "venture partners" → role_ids=[venture_partner]
- "general partners" or "managing partners" at VC/PE/investment firms → role_ids=[general_partner]
- "limited partners" or "LPs" → role_ids=[limited_partner]
- "investment partners" or "investor partners" → role_ids=[general_partner]

## KEY PRINCIPLE
Ask yourself: "Would someone searching for this query be surprised to see a [candidate_role] in their results?" If yes, do NOT include that role_id. Adjacent roles that happen to share a department are NOT the same job.

## OTHER RULES
- Company/location/industry filters are handled ELSEWHERE — only output role-related fields
- If query has NO role intent (just company/location/school) → return ALL fields empty. Do NOT explain why — just return empty strings and empty lists. No "this query is about people" or "no role detected" in semantic_query.
- NEVER invent role_ids — only use ones from the taxonomy above

Return JSON with exactly these keys: semantic_query, bm25_queries, role_ids, departments, seniority.
"""


def get_role_taxonomy_prompt() -> str:
    data = json.loads(ROLE_TAXONOMY_PATH.read_text())
    departments = data.get("departments", {})
    lines: list[str] = []
    for dept_name in sorted(departments):
        if dept_name == "noise":
            continue
        functions = departments[dept_name].get("functions") or []
        if functions:
            lines.append(f"  {dept_name}: {', '.join(functions)}")
    return "\n".join(lines)


def role_agent_system_prompt() -> str:
    return ROLE_AGENT_SYSTEM_PROMPT.format(taxonomy=get_role_taxonomy_prompt())


def role_agent_user_content(query: str) -> str:
    return f'Query: "{query}"'


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
    # Trait generation uses a specific user prompt format
    if name == "trait_generation":
        user_content = f'Generate traits for this query:\n\n"{query}"\n\nReturn JSON with: {{"traits": [{{"value": "...", "temporal": "current|past|all", "meaning": "role|experience|location|education|company|investor|general"}}], "has_domain_intent": true/false}}'
    elif name == "role":
        user_content = role_agent_user_content(query)
    else:
        user_content = query
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
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
    if role.get("departments"):
        # Preserve the RoleSearchAgentV2 department decision for parity/debugging.
        # Prod uses it to derive role_function, but does not currently expose it
        # as a hard role_track filter in ExtractedEntities.
        filters["role_departments"] = _dedupe_strings(role["departments"])
    if role.get("seniority"):
        filters["seniority_bands"] = _normalize_seniority_bands(role["seniority"])
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
    if temporal.get("is_current") is not None:
        current = bool(temporal["is_current"])
        filters.setdefault("is_current_role", current)
        filters.setdefault("is_current_company", current)

    # Seniority
    if seniority.get("seniority_bands"):
        # Normalize: "vice-president"/"c-suite" → local filter values.
        filters["seniority_bands"] = _normalize_seniority_bands(seniority["seniority_bands"])

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

    _apply_role_expansion_parity(filters, query)
    _apply_location_alias_fallback(filters, query)

    # Strip empty/null values
    filters = {k: v for k, v in filters.items() if v is not None and v != [] and v != ""}

    return filters


def _dedupe_strings(items: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item).strip()
        if not value:
            continue
        key = value.lower()
        if key not in seen:
            out.append(value)
            seen.add(key)
    return out


def _normalize_seniority_bands(items: list[Any]) -> list[str]:
    """Normalize prod/local seniority spellings to Powerpacks filter values."""
    return _dedupe_strings([
        str(item).strip().lower().replace("-", "_").replace(" ", "_")
        for item in items or []
        if item not in (None, "")
    ])


def _detect_csuite_expansions(query: str) -> list[dict[str, Any]]:
    q_lower = query.lower().strip()
    words: set[str] = set()
    for word in re.findall(r"[a-z]+", q_lower):
        words.add(word)
        if word.endswith("s") and len(word) > 3:
            words.add(word[:-1])
    return [spec for abbrev, spec in CSUITE_EXPANSION.items() if abbrev in words or spec["display"].lower() in q_lower]


def _has_explicit_founder_term(query: str) -> bool:
    return bool(re.search(r"\b(co[-\s]?founders?|founders?|founding)\b", query, re.IGNORECASE))


def _role_core_patterns_from_bm25(queries: list[str]) -> list[dict[str, Any]]:
    seniority_prefixes = re.compile(
        r"^(senior|staff|lead|principal|junior|founding|head of|director of|"
        r"vp of|chief|associate|intern|executive|managing|group)\s+",
        re.IGNORECASE,
    )
    examples: list[str] = []
    seen: set[str] = set()
    for query in queries:
        phrase = seniority_prefixes.sub("", str(query)).strip()
        phrase = re.split(r"[|,]", phrase)[0].strip()
        if len(phrase) < 3:
            continue
        key = phrase.lower()
        if key not in seen:
            examples.append(phrase)
            seen.add(key)

    if len(examples) > 1:
        lowered = [phrase.lower() for phrase in examples]
        keep = []
        for idx, phrase in enumerate(examples):
            candidate = lowered[idx]
            covered = any(other != candidate and " " in other and other in candidate for other in lowered)
            if not covered:
                keep.append(phrase)
        examples = keep or examples

    if not examples:
        return []
    escaped = [re.escape(phrase.lower()) for phrase in examples]
    return [{"regex": "\\b(" + "|".join(escaped) + ")\\b", "examples": examples[:15]}]


def _apply_role_expansion_parity(filters: dict[str, Any], query: str) -> None:
    """Apply prod-compatible deterministic role expansion to local query output.

    This covers the parts of prod role expansion that can be unit-tested without
    live TurboPuffer/title-clustering access: founder and C-suite short-circuits,
    canonical role_ids, BM25 aliases, role function, and regex preview patterns.
    """
    csuite_expansions = _detect_csuite_expansions(query)
    role_ids = _dedupe_strings(filters.get("role_ids") or [])
    role_ids_lower = {role_id.lower() for role_id in role_ids}

    is_founder = _has_explicit_founder_term(query) or bool(role_ids_lower & {"founder", "cofounder", "co-founder"})
    if is_founder:
        csuite_bm25 = [bm25 for spec in csuite_expansions for bm25 in spec["bm25"]]
        filters["role_ids"] = ["founder"]
        filters["bm25_queries"] = _dedupe_strings([*(filters.get("bm25_queries") or []), *FOUNDER_BM25_QUERIES, *csuite_bm25])
        if len(str(filters.get("semantic_query") or "")) < 80:
            filters["semantic_query"] = FOUNDER_SEMANTIC_QUERY
        filters["role_function"] = "founder"
        filters.pop("seniority_bands", None)
    elif csuite_expansions:
        display_values = [spec["display"] for spec in csuite_expansions]
        filters["role_ids"] = _dedupe_strings([*role_ids, *[spec["role_id"] for spec in csuite_expansions]])
        filters["bm25_queries"] = _dedupe_strings([
            *(filters.get("bm25_queries") or []),
            *[bm25 for spec in csuite_expansions for bm25 in spec["bm25"]],
        ])
        filters.setdefault("seniority_bands", ["c_suite"])
        if len(str(filters.get("semantic_query") or "")) < 80:
            filters["semantic_query"] = (
                f"Executive leader serving as {', '.join(display_values)}, responsible for strategic direction, "
                "organizational leadership, senior decision-making, cross-functional execution, and accountability "
                "for company or department outcomes. Profile evidence should include a current or past C-suite title."
            )
        filters["role_function"] = "leader"

    injected_titles = [title for role_id in filters.get("role_ids") or [] for title in ROLE_ID_TITLE_INJECTIONS.get(role_id, [])]
    if injected_titles:
        filters["bm25_queries"] = _dedupe_strings([*(filters.get("bm25_queries") or []), *sorted(set(injected_titles))])

    if filters.get("bm25_queries") and not filters.get("role_core_patterns"):
        filters["role_core_patterns"] = _role_core_patterns_from_bm25(filters["bm25_queries"])


def _add_unique(filters: dict[str, Any], key: str, value: str) -> None:
    values = list(filters.get(key) or [])
    if value not in values:
        values.append(value)
    filters[key] = values


def _alias_token_pattern(alias: str) -> str:
    return re.escape(alias).replace(r"\ ", r"\s+")


def _apply_location_alias_fallback(filters: dict[str, Any], query: str) -> None:
    """Deterministically recover common city abbreviations missed by extraction."""
    q = " ".join(query.lower().split())
    company_nouns = "|".join(COMPANY_LOCATION_NOUNS)
    person_prefixes = "|".join(re.escape(prefix).replace(r"\ ", r"\s+") for prefix in PERSON_LOCATION_PREFIXES)
    for alias, city in CITY_ALIASES.items():
        token = _alias_token_pattern(alias)
        company_prefix = rf"\b{token}\s+(?:{company_nouns})\b"
        company_suffix = rf"\b(?:{company_nouns})\s+(?:in|based\s+in|located\s+in)\s+{token}\b"
        person_suffix = rf"\b(?:{person_prefixes})\s+{token}\b"

        if re.search(company_prefix, q) or re.search(company_suffix, q):
            _add_unique(filters, "company_cities", city)
        elif re.search(person_suffix, q):
            _add_unique(filters, "cities", city)


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
        "role": role_agent_system_prompt(),
        "trait_generation": _load_prompt("trait_generation"),
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

    role, company, location, education, temporal, seniority, social, traits = await asyncio.gather(
        timed_extract("role"),
        timed_extract("company"),
        timed_extract("location"),
        timed_extract("education"),
        timed_extract("temporal"),
        timed_extract("seniority"),
        timed_extract("social"),
        timed_extract("trait_generation"),
    )

    total_ms = int((time.monotonic() - started) * 1000)

    # Merge
    filters = _merge(role, company, location, education, temporal, seniority, social, query)

    # Extract traits and has_domain_intent from trait generator
    generated_traits = traits.get("traits") or []
    has_domain_intent = traits.get("has_domain_intent")
    if has_domain_intent is not None:
        filters["has_domain_intent"] = bool(has_domain_intent)

    return {
        "intent_type": "role_search",
        "source_type": "query",
        "normalized_query": query,
        "vertical": "people_by_role",
        "role_search_filters": filters,
        "traits": generated_traits,
        "notes": [],
        "extractor_timings": timings,
        "total_ms": total_ms,
    }
