"""RapidAPI-only company context for search-harness review rows."""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from packs.indexing.primitives.enrich_companies_checkpointed import rapidapi_company as rapidapi
from packs.search.primitives.turbopuffer import turbopuffer_resolve_companies as company_search
from packs.search.primitives.deep_search.fetch_jd import (
    JOB_BOARD_HOSTS, extract_linkedin_company_slug, fetch,
)


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CACHE_DIR = ROOT / ".powerpacks/rapidapi-company-cache"
TARGET_LEVELS = {
    "senior_ic": 1, "staff_ic": 2, "lead": 2, "manager": 3,
    "director": 4, "vp": 5, "exec": 6,
}
MOVE_PLAUSIBILITY = {
    "in-band", "promising step-up", "junior-could-grow", "too-senior", "wrong-timing",
    "flag-relationship", "unhireable",
}
PEDIGREE_PRIORS = {"strong", "neutral", "weak"}
FIT_GROUPS = {"send_worthy", "chat_worthy", "wrong_timing_relationship", "passed"}
COMPANY_FIT_PROMPT = """You are annotating a recruiter review table after ranking is complete.
For every supplied candidate, make one integrated recruiting decision using the candidate's pond trait
scores and evidence, level, current or last-known employer context, headcount, stage, funding, recent
role history, the hiring-company context, and the posted compensation band. A technically qualified person can still be unhireable when
the destination cannot plausibly pull them. Do not change rerank scores or candidate data.
A candidate whose evident market compensation materially exceeds the posted band is unhireable even
when their title wording appears in band; state the compensation mismatch in the reason.
A recent move (roughly under 18 months) to a strong employer usually makes near-term recruitment
unrealistic regardless of level fit; label that wrong-timing and explain that the relationship should
be built for later.
Compare destination pull as well: an in-band candidate at a clearly stronger employer tier, such as a
top research lab or elite product company, is not a sendable near-term move when the hiring company is
much smaller or earlier-stage. Label that flag-relationship and explain that the relationship should be
built for later. Use the supplied employer facts; do not infer that every larger company is stronger.

Separately assign a strong, neutral, or weak pedigree prior for this role family, with one sentence of
evidence. Weigh current and recent employers by how likely strong people in this role family concentrate
there. Product companies with hard role-relevant hiring bars are a strong prior. Enterprises where the
role is mainly a support function are a weak prior. Long tenure in a slow, regulated, non-product
environment is weak evidence of startup pace. This is role-family-conditional: an employer that is weak
evidence for startup software engineering can be strong evidence for a role needing that employer's
domain. Judge the employer as a talent environment for the candidate's role family, not merely by
industry overlap with the hiring company: domain relevance alone does not make a support-function
software environment a strong software-engineering prior. Pedigree is a prior, not a gate, and must
remain separate from level, timing, and move plausibility. Retrieved fit precedents are
role-family-conditional evidence. Higher-quality cards take precedence, and cards apply only when the
supplied role and candidate evidence are genuinely analogous.

Assign exactly one review group. send_worthy requires positive role evidence in the pond traits or a
strong role-family pedigree plus a plausible move. chat_worthy is plausible but needs calibration or
has only generic evidence. wrong_timing_relationship is qualified but unrealistic now because of timing
or destination pull. passed is the wrong fit, too senior, unhireable, or otherwise not worth pursuing.
When the job has a defining capability beyond its source occupation, an occupation-only trait match is
generic evidence and cannot by itself support send_worthy, regardless of its rerank score or pedigree.
The one why sentence must explain the decisive evidence for the group; do not merely restate the query,
job title, score, or location.

Return strict JSON:
{"level_read":"...","move_plausibility":"in-band|promising step-up|junior-could-grow|too-senior|wrong-timing|flag-relationship|unhireable","pedigree_prior":"strong|neutral|weak","group":"send_worthy|chat_worthy|wrong_timing_relationship|passed","why":"exactly one sentence"}
"""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _name_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).casefold())


def _domain(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    return str(parsed.hostname or "").casefold().removeprefix("www.")


def _linkedin_slug(value: Any) -> str:
    parsed = urllib.parse.urlparse(_text(value))
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname and "linkedin.com" in parsed.hostname and len(parts) >= 2 and parts[0] == "company":
        return parts[1].casefold()
    return ""


def hiring_company_ref(name: Any, source_url: Any) -> dict[str, str]:
    parsed = urllib.parse.urlparse(_text(source_url))
    host = str(parsed.hostname or "").casefold()
    return {
        "name": _text(name),
        "slug": _linkedin_slug(source_url),
        "company_id": "",
        "domain": "" if host in JOB_BOARD_HOSTS else _domain(source_url),
    }


def resolve_hiring_company_ref(company: Mapping[str, Any], source_url: Any = None) -> dict[str, str]:
    """Resolve the destination by website domain, else verified company name."""
    name = _text(company.get("name"))
    website = _text(company.get("website_url"))
    if not hiring_company_ref(name, website)["domain"]:
        website = _text(source_url) or website
    ref = hiring_company_ref(name, website)
    rows: list[dict[str, Any]] = []
    if ref["domain"]:
        rows = asyncio.run(company_search.exact_domain_lookup(ref["domain"], top_k=5))
        rows = [row for row in rows if _domain(row.get("website_domain")) == ref["domain"]]
        ref["resolution_basis"] = "website_domain"
        ref["verified_domain"] = ref["domain"]
        if not rows or not _linkedin_slug(rows[0].get("linkedin_url")):
            try:
                raw_html, final_url = fetch(f"https://{ref['domain']}")
                if _domain(final_url) == ref["domain"]:
                    ref["slug"] = extract_linkedin_company_slug(raw_html, final_url)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pass
    elif name:
        rows = asyncio.run(company_search.exact_name_lookup([name], None, top_k=5))
        if not rows:
            rows = asyncio.run(company_search.name_bm25_lookup([name], None, top_k=5))
        rows = [row for row in rows if _name_key(row.get("company_name")) == _name_key(name)]
        ref["resolution_basis"] = "verified_name"
        ref["verified_name"] = name
    if rows:
        row = rows[0]
        ref["slug"] = _linkedin_slug(row.get("linkedin_url")) or ref["slug"]
    return ref


def _months_in_seat(value: Any, as_of: date | None = None) -> int | None:
    try:
        started = datetime.fromisoformat(_text(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None
    today = as_of or datetime.now(timezone.utc).date()
    return max(0, (today.year - started.year) * 12 + today.month - started.month + 1)


def current_company_ref(profile: Mapping[str, Any], fallback_name: Any = "", *,
                        as_of: date | None = None) -> dict[str, Any]:
    positions = profile.get("positions") or []
    current = next((row for row in positions if isinstance(row, Mapping) and
                    (row.get("is_current") is True or row.get("is_current_position") is True)), None)
    timing = "current"
    if current is None:
        timing = "last-known"
        current = next((row for row in positions if isinstance(row, Mapping)), None)
        if current is None:
            current = {}
    name = _text(current.get("company_name") or current.get("company") or fallback_name)
    company_id = _text(current.get("rapidapi_company_id"))
    if company_id == "0":
        company_id = ""
    linkedin_url = current.get("company_linkedin_url") or current.get("company_url")
    start_date = _text(current.get("start_date")) if timing == "current" else ""
    return {
        "name": name,
        "slug": _text(current.get("company_public_identifier")).casefold() or _linkedin_slug(linkedin_url),
        "company_id": company_id,
        "domain": _domain(current.get("company_domain")),
        "company_timing": timing,
        "current_position_start_date": start_date or None,
        "months_in_seat": _months_in_seat(start_date, as_of) if start_date else None,
    }


def company_facts(response: Mapping[str, Any]) -> dict[str, Any]:
    data = response.get("data", response)
    if not isinstance(data, Mapping):
        return {}
    headcount = data.get("staffCount") or data.get("employeeCount")
    try:
        headcount = int(headcount) if headcount is not None else None
    except (TypeError, ValueError):
        headcount = None
    funding = data.get("fundingData") or {}
    last_round = funding.get("lastFundingRound") if isinstance(funding, Mapping) else {}
    last_round = last_round if isinstance(last_round, Mapping) else {}
    money = last_round.get("moneyRaised") or {}
    last_amount = money.get("amount") if isinstance(money, Mapping) else None
    last_currency = _text(money.get("currencyCode")) if isinstance(money, Mapping) else ""
    total = funding.get("totalFunding") if isinstance(funding, Mapping) else None
    if isinstance(total, Mapping):
        total_amount = total.get("amount")
        total_currency = _text(total.get("currencyCode"))
    else:
        total_amount = total
        total_currency = ""
    amount = total_amount if str(total_amount or "").strip() else last_amount
    currency = total_currency or last_currency
    funding_basis = "total_raised" if str(total_amount or "").strip() else "last_round"
    try:
        amount = float(amount) if str(amount or "").strip() else None
    except (TypeError, ValueError):
        amount = None
    name = _text(data.get("name") or data.get("companyName"))
    stage = _text(last_round.get("fundingType"))
    return {
        "name": name,
        "headcount": headcount,
        "stage": stage or None,
        "funding": amount,
        "funding_currency": currency or None,
        "funding_basis": funding_basis if amount is not None else None,
        "linkedin_slug": _text(data.get("universalName")).casefold() or None,
        "domain": _domain(data.get("website")),
    } if name or headcount is not None or stage or amount is not None else {}


def pull_note(context: Mapping[str, Any]) -> str:
    parts = []
    if context.get("headcount") is not None:
        parts.append(f"{int(context['headcount']):,} employees")
    parts.append(_text(context.get("stage")).replace("_", " ").title() or "stage unavailable")
    amount = context.get("funding")
    basis = "total raised" if context.get("funding_basis") == "total_raised" else "latest round"
    parts.append(f"{basis} ${float(amount):,.0f}" if amount is not None else f"{basis} unavailable")
    return " · ".join(parts)


def _cache_index(cache_dir: Path) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    ambiguous: set[str] = set()
    if not cache_dir.is_dir():
        return index
    for path in cache_dir.glob("*.json"):
        try:
            response = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(response, Mapping):
            continue
        data = response.get("data", response)
        lookup = response.get("_search_harness_lookup") or {}
        if not isinstance(data, Mapping) or not isinstance(lookup, Mapping):
            continue
        aliases = {
            f"name:{_name_key(data.get('name') or data.get('companyName') or lookup.get('name'))}",
            f"slug:{_text(data.get('universalName') or lookup.get('slug')).casefold()}",
            f"domain:{_domain(data.get('website') or lookup.get('domain'))}",
        }
        for alias in aliases:
            if alias.endswith(":") or alias in ambiguous:
                continue
            if alias in index:
                index.pop(alias)
                ambiguous.add(alias)
            else:
                index[alias] = response
    return index


def _ref_key(ref: Mapping[str, Any]) -> str:
    for field, normalizer in (("company_id", _text), ("slug", lambda value: _text(value).casefold()),
                              ("domain", _domain), ("name", _name_key)):
        value = normalizer(ref.get(field))
        if value:
            return f"{field}:{value}"
    return "unknown:"


def _cached(ref: Mapping[str, Any], cache_dir: Path,
            index: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    company_id, slug = _text(ref.get("company_id")), _text(ref.get("slug")).casefold()
    if company_id:
        found = rapidapi.load_cached_company_details([company_id], cache_dir=cache_dir).get(company_id)
        if found is not None:
            return found
    if slug:
        found = rapidapi.load_cached_company_details_by_slug([slug], cache_dir=cache_dir).get(slug)
        if found is not None:
            return found
    aliases = [f"domain:{_domain(ref.get('domain'))}"]
    if not ref.get("verified_domain"):
        aliases.append(f"name:{_name_key(ref.get('name'))}")
    for alias in aliases:
        if not alias.endswith(":") and alias in index:
            return index[alias]
    return None


def _remember_failure(ref: Mapping[str, Any], response: Mapping[str, Any], cache_dir: Path) -> None:
    wrapper = {
        "_search_harness_lookup": {key: _text(ref.get(key)) for key in
                                   ("name", "slug", "company_id", "domain")},
        "lookup_error": _text(response.get("error")) or "unresolved",
        "data": {},
    }
    if ref.get("company_id"):
        rapidapi._write_cache(_text(ref["company_id"]), wrapper, cache_dir)  # noqa: SLF001
    elif ref.get("slug"):
        rapidapi._write_cache(rapidapi._slug_cache_key(_text(ref["slug"])), wrapper, cache_dir)  # noqa: SLF001


def resolve_company_contexts(
    refs: Sequence[Mapping[str, Any]], *, cache_dir: str | Path | None = None,
    api_key: str | None = None, unit_cost_usd: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve unique company references cache-first, then by RapidAPI ID/slug."""
    directory = Path(cache_dir or os.getenv("POWERPACKS_RAPIDAPI_COMPANY_CACHE") or DEFAULT_CACHE_DIR)
    index = _cache_index(directory)
    unique = {_ref_key(ref): dict(ref) for ref in refs if _ref_key(ref) != "unknown:"}
    resolved: dict[str, dict[str, Any]] = {}
    stats = {"cache_hits": 0, "cache_misses": 0, "live_lookups": 0,
             "unresolved": 0, "cost_usd": 0.0}
    key = api_key if api_key is not None else rapidapi._api_key()  # noqa: SLF001
    for ref_key, ref in unique.items():
        response = _cached(ref, directory, index)
        source = "cache"
        if response is None:
            stats["cache_misses"] += 1
            company_id, slug = _text(ref.get("company_id")), _text(ref.get("slug")).casefold()
            if key and (company_id or slug):
                stats["live_lookups"] += 1
                source = "rapidapi"
                response = (rapidapi.fetch_company_details(company_id, api_key=key, cache_dir=directory)
                            if company_id else rapidapi.fetch_company_details_by_slug(
                                slug, api_key=key, cache_dir=directory))
                if response.get("error"):
                    _remember_failure(ref, response, directory)
            else:
                response = {}
        else:
            stats["cache_hits"] += 1
        context = company_facts(response)
        expected_name = _text(ref.get("verified_name"))
        expected_domain = _domain(ref.get("verified_domain"))
        if context and expected_name and _name_key(context.get("name")) != _name_key(expected_name):
            context = {}
        if context and expected_domain and _domain(context.get("domain")) != expected_domain:
            context = {}
        if context:
            context["source"] = source
            context["resolution_basis"] = ref.get("resolution_basis")
        else:
            stats["unresolved"] += 1
        resolved[ref_key] = context
    price = float(unit_cost_usd if unit_cost_usd is not None else
                  os.getenv("POWERPACKS_RAPIDAPI_COMPANY_LOOKUP_USD", "0") or 0)
    stats["cost_usd"] = (round(int(stats["live_lookups"]) * price, 6)
                         if price or not stats["live_lookups"] else None)
    stats["unit_cost_usd"] = price
    stats["billing_basis"] = "configured_per_lookup" if price else "unit_price_not_configured"
    return [resolved.get(_ref_key(ref), {}) for ref in refs], stats


def company_move(hiring: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    def band(value: Any) -> int | None:
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return 0 if count < 50 else 1 if count < 200 else 2 if count < 1000 else 3
    target, origin = band(hiring.get("headcount")), band(current.get("headcount"))
    if target is None or origin is None:
        return "unknown"
    return "step-up" if target > origin else "step-down" if target < origin else "lateral"


def fit_label(title: Any, target_level: Any) -> str | None:
    text = _text(title).casefold()
    rules = (
        (r"\b(founder|owner|partner|chief|cto|ceo|cfo|coo)\b", 6),
        (r"\b(vp|vice president)\b", 5), (r"\b(director|head of)\b", 4),
        (r"\bmanager\b", 3), (r"\b(staff|principal|lead)\b", 2),
        (r"\bsenior\b", 1), (r"\b(junior|associate|analyst|intern)\b", 0),
    )
    current = next((rank for pattern, rank in rules if re.search(pattern, text)), None)
    target = TARGET_LEVELS.get(_text(target_level).casefold())
    if current is None or target is None:
        return None
    if current > target:
        return "too-senior"
    if current == target:
        return "in-band"
    return "promising step-up" if current == target - 1 else "junior — could grow"


def fallback_company_fit(candidate: Mapping[str, Any], target_level: Any) -> dict[str, Any]:
    label = fit_label(candidate.get("title"), target_level)
    return {
        "level_read": _text(candidate.get("title")) or "Level unclear",
        "move_plausibility": (
            "junior-could-grow" if label == "junior — could grow" else label or "in-band"
        ),
        "pedigree_prior": "neutral",
        "group": "passed",
        "why": "Candidate fit was not model-reviewed because the company-fit call failed.",
        "move_annotation_source": "fallback",
        "pedigree_annotation_source": "fallback",
        "fit_annotation_source": "fallback",
    }


def company_fit_messages(*, jd: str, target_level: Any, comp_band: Any = None,
                         hiring_company: Mapping[str, Any],
                         candidate: Mapping[str, Any], brief: Mapping[str, Any],
                         fit_precedents: Sequence[Mapping[str, Any]] = (),
                         ) -> list[dict[str, str]]:
    compact = {
        "title": candidate.get("title"),
        "company": candidate.get("company"),
        "company_timing": candidate.get("company_timing"),
        "company_headcount": candidate.get("current_company_headcount"),
        "company_stage": candidate.get("current_company_stage"),
        "company_funding": candidate.get("current_company_funding"),
        "company_funding_basis": candidate.get("current_company_funding_basis"),
        "current_position_start_date": candidate.get("current_position_start_date"),
        "months_in_seat": candidate.get("months_in_seat"),
        "recent_roles": candidate.get("recent_roles") or [],
        "rerank_score": candidate.get("score"),
        "pond_trait_scores": candidate.get("trait_scores") or {},
    }
    return [
        {"role": "system", "content": COMPANY_FIT_PROMPT},
        {"role": "user", "content": json.dumps({
            "job_description": jd,
            "target_level": target_level,
            "brief": dict(brief),
            "comp_band": comp_band,
            "hiring_company": dict(hiring_company),
            "fit_precedents": list(fit_precedents),
            "candidate": compact,
        }, ensure_ascii=False)},
    ]


def apply_company_fit_response(candidate: Mapping[str, Any], raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    required = {"level_read", "move_plausibility", "pedigree_prior", "group", "why"}
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("company-fit response has the wrong fields")
    label = _text(payload.get("move_plausibility"))
    pedigree = _text(payload.get("pedigree_prior"))
    group = _text(payload.get("group"))
    if (not _text(payload.get("level_read")) or label not in MOVE_PLAUSIBILITY or
            pedigree not in PEDIGREE_PRIORS or group not in FIT_GROUPS or
            not _text(payload.get("why"))):
        raise ValueError("company-fit response has an invalid label")
    row = dict(candidate)
    row.update({
        "level_read": _text(payload.get("level_read")), "move_plausibility": label,
        "pedigree_prior": pedigree, "group": group, "why": _text(payload.get("why")),
        "move_annotation_source": "luna", "pedigree_annotation_source": "luna",
        "fit_annotation_source": "luna",
    })
    override = row.get("fit_override")
    if (isinstance(override, Mapping) and override.get("reviewed") is True and
            _text(override.get("group")) in FIT_GROUPS and _text(override.get("why"))):
        row.update({"group": _text(override.get("group")), "why": _text(override.get("why")),
                    "fit_annotation_source": "human"})
    return row
