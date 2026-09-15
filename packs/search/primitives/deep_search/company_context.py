"""Company hydration and a single career-context move-likelihood judgment."""
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

from packs.search.primitives.llm_rerank_candidates.cross_encoder import profile_evidence

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CACHE_DIR = ROOT / ".powerpacks/rapidapi-company-cache"
MOVE_LIKELIHOOD_LABELS = ("plausible", "unlikely", "unclear")
MOVE_LIKELIHOOD_PROMPT = """Judge whether this particular job is a plausible next move for the candidate.
Qualifications are already scored by the cross-encoder. Do not re-score skills, company prestige,
craft or potential. Compare the candidate's current responsibilities and career path with the target
role, using full work history and the supplied company context.

Focus on the work and scope, not titles alone. A founder/CEO at a small startup who still builds,
reviews work and works directly with customers can plausibly move into a staff/principal IC role.
An executive running a large organization through managers, with no recent IC work, is less likely
to take that role unless the profile supports a return to it. Apply the equivalent distinction in
every job family. A lower title is not necessarily a step down in actual responsibility.

Company size and stage, current versus target scope, recent role changes and prior transitions can
support a directional judgment. Explain the signals and distinguish inference from known intent.
A founder title or equity assumption alone is not a reason to say unlikely. An old funding round
does not prove low runway or a desire to leave. No automatic tenure or headcount cutoff.
Missing compensation does not prevent a scope-based judgment; do not invent pay, ownership terms,
financial distress or willingness. Ignore age and other protected attributes.

Return plausible when the move fits the demonstrated career scope or a reasonable transition;
unlikely when specific evidence shows a substantial career/scope step back or an explicit obstacle;
unclear when the available responsibilities and context cannot support either. Sparse evidence is
unclear, not unlikely. These are recruiting hypotheses, not knowledge of the person's intentions.
Return only JSON: {"label":"plausible|unlikely|unclear","why":"One short sentence naming the decisive evidence and uncertainty."}
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


def move_likelihood_messages(*, jd: str, candidate: Mapping[str, Any],
                             hiring_company: Mapping[str, Any], pond_query: str,
                             target_level: Any = None, comp_band: Any = None,
                             as_of: str | None = None) -> list[dict[str, str]]:
    current_company = {
        key.removeprefix("current_company_"): value
        for key, value in candidate.items()
        if key.startswith("current_company_") and value not in (None, "", [])
    }
    payload = {
        "as_of": as_of or date.today().isoformat(),
        "job_description": jd,
        "pond_query": pond_query,
        "target_level": target_level,
        "comp_band": comp_band,
        "hiring_company": {key: value for key, value in hiring_company.items()
                           if key != "pull_note" and value not in (None, "", [])},
        "candidate": profile_evidence(dict(candidate)),
        "current_company": current_company,
    }
    return [
        {"role": "system", "content": MOVE_LIKELIHOOD_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_move_likelihood(raw: str) -> dict[str, str]:
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or set(payload) != {"label", "why"} or
            not isinstance(payload["label"], str) or
            payload["label"] not in MOVE_LIKELIHOOD_LABELS or
            not isinstance(payload["why"], str) or not payload["why"].strip()):
        raise ValueError("Move likelihood requires a valid label and reason")
    return {"label": payload["label"], "why": _text(payload["why"])}
