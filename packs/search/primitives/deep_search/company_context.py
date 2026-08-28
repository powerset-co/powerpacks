"""RapidAPI-only company context for search-harness review rows."""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone
from enum import Enum
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
PEDIGREE_PRIORS = {"strong", "neutral", "weak"}
CRAFT_SIGNALS = {"strong", "promising", "unclear", "weak"}
FIT_GROUPS = {"send_worthy", "chat_worthy", "wrong_timing_relationship", "passed"}


class FitExpert(str, Enum):
    ROLE = "role_fit"
    COMPANY = "company_taste"
    CRAFT = "craft_and_potential"
    MOVE = "move_feasibility"


FIT_EXPERTS = tuple(FitExpert)
ROLE_FIT_PROMPT = """You are the role and seniority expert on a recruiter review panel.
Judge only whether the candidate has evidence for the role's defining work and whether their current
career level fits the target. A title match without the defining capability is generic evidence. Treat
seniority as a real transition: do not call a materially higher-level candidate a fit for a lower or
different job merely because they could do it. Do not automatically reject an adjacent-level candidate
when the destination scope could match. Use pond trait evidence and recent roles. Ignore employer
prestige, compensation, tenure, timing, and destination pull; other experts own those judgments. Do not
change the rerank score or candidate data.

If a retrieved precedent is genuinely analogous, include its ID and return that card's judgment label and
reason. Including an ID means applying it; otherwise return an empty list. Return strict JSON:
{"label":"in-band|promising step-up|junior-could-grow|too-senior|unhireable","why":"1-2 evidence-based sentences naming the capability and level signals","applied_precedent_ids":["..."]}
"""
COMPANY_TASTE_PROMPT = """You are the company-taste expert on a recruiter review panel.
Assign a strong, neutral, or weak company prior for this candidate in this role family. Judge current and
recent employers as talent environments for the candidate's actual function, not by industry overlap or
company size alone. Product companies with hard role-relevant hiring bars are strong evidence; support
functions, weak agencies, and unrelated professional environments are weak evidence unless the job needs
that exact domain. Founding, freelance, or agency experience alone does not prove a strong hiring bar;
when employer selectivity is not evidenced, stay neutral. Retrieved precedents apply only when genuinely
analogous. The prior is evidence, not a gate. Ignore candidate seniority, compensation, tenure, timing,
and destination pull; other experts own those judgments.

If a retrieved precedent is genuinely analogous, include its ID and return that card's judgment label and
reason. Including an ID means applying it; otherwise return an empty list. Return strict JSON:
{"label":"strong|neutral|weak","why":"1-2 evidence-based sentences naming the role-family employer evidence and uncertainty","applied_precedent_ids":["..."]}
"""
CRAFT_POTENTIAL_PROMPT = """You are the individual craft and potential expert on a recruiter review panel.
First infer what exceptional craft or potential would look like for this specific JD and job family. Then
judge the candidate's individual quality and upside from role-appropriate evidence. Consider trajectory,
scope, ownership, outcomes, and evidence quality. Scope may appear as technical complexity, people,
revenue, transactions, product reach, operational scale, or another form implied by the JD; do not apply
one function's proxy to another. Fast progression or increasing responsibility can show potential. Company,
team, and education selectivity are supporting priors when relevant, never proof or substitutes for work
evidence. Do not reward famous names or impressive titles mechanically. Strong means demonstrated
exceptional individual work. Promising means visible trajectory or ownership despite incomplete proof.
Unclear is the default when supplied evidence cannot support a confident read; do not invent weakness.
Weak requires affirmative evidence of shallow, irrelevant, or poor-quality work. Use level changes to
understand trajectory, but leave role-level fit to the role expert. Ignore compensation, move timing, and
destination pull.

If a retrieved precedent is genuinely analogous, include its ID and return that card's judgment label and
reason. Including an ID means applying it; otherwise return an empty list. Return strict JSON:
{"label":"strong|promising|unclear|weak","why":"1-2 evidence-based sentences naming the individual's work, trajectory, and uncertainty","applied_precedent_ids":["..."]}
"""
MOVE_FEASIBILITY_PROMPT = """You are the move-feasibility expert on a recruiter review panel.
Assume role fit is judged separately. Decide whether this hiring company and posted compensation can
plausibly pull the candidate now. Evident market compensation materially above the band is unhireable.
A recent move, roughly under 18 months, to a strong employer is usually wrong-timing. A clearly stronger
current employer can support a relationship judgment only with supplied evidence of a compensation or
destination pull mismatch. Missing destination stage, funding, or compensation is never negative evidence
and cannot justify flag-relationship. Do not infer a mismatch from employer brand or headcount alone.
Ignore role quality and company pedigree; other experts own those judgments.

If a retrieved precedent is genuinely analogous, include its ID and return that card's judgment label and
reason. Including an ID means applying it; otherwise return an empty list. Return strict JSON:
{"label":"in-band|wrong-timing|flag-relationship|unhireable","why":"1-2 evidence-based sentences naming the compensation, destination, and timing evidence","applied_precedent_ids":["..."]}
"""
COMPANY_FIT_PROMPT = """You make the final decision from four independent recruiter experts.
Do not re-score the candidate or invent evidence. The role expert owns role and seniority fit, the company
expert owns the role-family company prior, the craft expert owns individual quality and upside, and the
move expert owns compensation, timing, and destination pull. Treat the outputs as distinct evidence, not
votes to average.

Assign exactly one review group. send_worthy requires positive role evidence, strong or promising craft,
and a plausible move; company pedigree can strengthen role evidence but never substitute for it.
chat_worthy is plausible but needs calibration, has only generic role evidence, or has unclear craft.
wrong_timing_relationship requires a qualified candidate with strong or promising craft and a supported
timing or destination-pull blocker. passed is the wrong role, weak craft, materially too senior,
unhireable, or otherwise not worth pursuing. The why sentence must name the decisive evidence rather than
restating a title or score.

If a retrieved final-decision precedent is genuinely analogous, follow it and include its ID. Otherwise
return an empty list. Return strict JSON:
{"group":"send_worthy|chat_worthy|wrong_timing_relationship|passed","why":"exactly one sentence","applied_precedent_ids":["..."]}
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
    industries = data.get("industries") or data.get("industry") or []
    if isinstance(industries, str):
        industries = [industries]
    industries = [_text(value) for value in industries if _text(value)]
    return {
        "name": name,
        "headcount": headcount,
        "stage": stage or None,
        "funding": amount,
        "funding_currency": currency or None,
        "funding_basis": funding_basis if amount is not None else None,
        "linkedin_slug": _text(data.get("universalName")).casefold() or None,
        "domain": _domain(data.get("website")),
        "industries": industries,
    } if name or headcount is not None or stage or amount is not None or industries else {}


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
    role_label = "junior-could-grow" if label == "junior — could grow" else label or "in-band"
    unavailable = "Not model-reviewed because the company-fit panel failed."
    return {
        "fit_experts": {
            FitExpert.ROLE.value: {
                "label": role_label, "why": unavailable, "applied_precedent_ids": []},
            FitExpert.COMPANY.value: {
                "label": "neutral", "why": unavailable, "applied_precedent_ids": []},
            FitExpert.CRAFT.value: {
                "label": "unclear", "why": unavailable, "applied_precedent_ids": []},
            FitExpert.MOVE.value: {
                "label": "in-band", "why": unavailable, "applied_precedent_ids": []},
        },
        "applied_precedent_ids": [],
        "applied_fit_precedents": [],
        "group": "passed",
        "why": unavailable,
        "fit_annotation_source": "fallback",
    }


def _fit_input(*, jd: str, target_level: Any, comp_band: Any,
               hiring_company: Mapping[str, Any], candidate: Mapping[str, Any],
               brief: Mapping[str, Any],
               fit_precedents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    compact = {
        "title": candidate.get("title"),
        "company": candidate.get("company"),
        "company_timing": candidate.get("company_timing"),
        "company_headcount": candidate.get("current_company_headcount"),
        "company_stage": candidate.get("current_company_stage"),
        "company_industries": candidate.get("current_company_industries") or [],
        "company_funding": candidate.get("current_company_funding"),
        "company_funding_basis": candidate.get("current_company_funding_basis"),
        "current_position_start_date": candidate.get("current_position_start_date"),
        "months_in_seat": candidate.get("months_in_seat"),
        "recent_roles": candidate.get("recent_roles") or [],
        "education": candidate.get("education") or [],
        "rerank_score": candidate.get("score"),
        "pond_trait_scores": candidate.get("trait_scores") or {},
    }
    return {
        "job_description": jd,
        "target_level": target_level,
        "brief": dict(brief),
        "comp_band": comp_band,
        "hiring_company": {key: value for key, value in hiring_company.items()
                           if key != "pull_note" and value is not None and value != ""},
        "fit_precedents": list(fit_precedents),
        "candidate": compact,
    }


def company_fit_expert_messages(*, expert: FitExpert, jd: str, target_level: Any,
                                comp_band: Any = None, hiring_company: Mapping[str, Any],
                                candidate: Mapping[str, Any], brief: Mapping[str, Any],
                                fit_precedents: Sequence[Mapping[str, Any]] = (),
                                ) -> list[dict[str, str]]:
    prompts = {
        FitExpert.ROLE: ROLE_FIT_PROMPT,
        FitExpert.COMPANY: COMPANY_TASTE_PROMPT,
        FitExpert.CRAFT: CRAFT_POTENTIAL_PROMPT,
        FitExpert.MOVE: MOVE_FEASIBILITY_PROMPT,
    }
    return [
        {"role": "system", "content": prompts[expert]},
        {"role": "user", "content": json.dumps(_fit_input(
            jd=jd, target_level=target_level, comp_band=comp_band,
            hiring_company=hiring_company, candidate=candidate, brief=brief,
            fit_precedents=fit_precedents), ensure_ascii=False)},
    ]


def company_fit_decision_messages(*, fit_experts: Mapping[str, Mapping[str, Any]],
                                  fit_precedents: Sequence[Mapping[str, Any]] = (),
                                  ) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": COMPANY_FIT_PROMPT},
        {"role": "user", "content": json.dumps(
            {"fit_experts": dict(fit_experts), "fit_precedents": list(fit_precedents)},
            ensure_ascii=False)},
    ]


def parse_fit_expert(expert: FitExpert, raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if (not isinstance(payload, Mapping) or
            set(payload) != {"label", "why", "applied_precedent_ids"}):
        raise ValueError(f"{expert.value} response has the wrong fields")
    applied = payload["applied_precedent_ids"]
    if not isinstance(applied, list) or not all(isinstance(value, str) for value in applied):
        raise ValueError(f"{expert.value} response has invalid precedent IDs")
    values = {
        "label": _text(payload["label"]), "why": _text(payload["why"]),
        "applied_precedent_ids": [_text(value) for value in applied if _text(value)],
    }
    allowed = {
        FitExpert.ROLE: {
            "in-band", "promising step-up", "junior-could-grow", "too-senior", "unhireable"},
        FitExpert.COMPANY: PEDIGREE_PRIORS,
        FitExpert.CRAFT: CRAFT_SIGNALS,
        FitExpert.MOVE: {"in-band", "wrong-timing", "flag-relationship", "unhireable"},
    }[expert]
    if values["label"] not in allowed or not values["why"]:
        raise ValueError(f"{expert.value} response has an invalid label")
    return values


def parse_fit_decision(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if (not isinstance(payload, Mapping) or
            set(payload) != {"group", "why", "applied_precedent_ids"}):
        raise ValueError("company-fit decision has the wrong fields")
    applied = payload["applied_precedent_ids"]
    if not isinstance(applied, list) or not all(isinstance(value, str) for value in applied):
        raise ValueError("company-fit decision has invalid precedent IDs")
    decision = {"group": _text(payload["group"]), "why": _text(payload["why"]),
                "applied_precedent_ids": [_text(value) for value in applied if _text(value)]}
    if decision["group"] not in FIT_GROUPS or not decision["why"]:
        raise ValueError("company-fit decision has an invalid label")
    return decision


def _bind_fit_precedents(
    fit_experts: Mapping[str, Mapping[str, Any]], decision: Mapping[str, Any],
    fit_precedents: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    experts = {name: dict(values) for name, values in fit_experts.items()}
    final = dict(decision)
    applied = []
    for dimension, values in [*experts.items(), ("final_decision", final)]:
        requested = set(values.get("applied_precedent_ids") or [])
        for card in fit_precedents.get(dimension, ()):
            if card.get("id") not in requested:
                continue
            judgment = card.get("judgment") or {}
            if dimension == "final_decision" and judgment.get("group") in FIT_GROUPS:
                values["group"] = judgment["group"]
                values["why"] = _text(card.get("reason")) or values.get("why")
            elif judgment.get("label"):
                values["label"] = _text(judgment["label"])
                values["why"] = _text(card.get("reason")) or values.get("why")
            applied.append({key: card.get(key) for key in (
                "id", "dimension", "judgment", "reason", "retrieval_score")})
    return experts, final, applied


def apply_company_fit_response(candidate: Mapping[str, Any],
                               fit_experts: Mapping[str, Mapping[str, Any]],
                               decision: Mapping[str, Any],
                               fit_precedents: Mapping[
                                   str, Sequence[Mapping[str, Any]]] | None = None,
                               ) -> dict[str, Any]:
    bound_experts, bound_decision, applied = _bind_fit_precedents(
        fit_experts, decision, fit_precedents or {})
    role = bound_experts[FitExpert.ROLE.value]
    company = bound_experts[FitExpert.COMPANY.value]
    craft = bound_experts[FitExpert.CRAFT.value]
    move = bound_experts[FitExpert.MOVE.value]
    role_label = _text(role.get("label"))
    move_label = _text(move.get("label"))
    group = _text(bound_decision.get("group"))
    why = _text(bound_decision.get("why"))
    if role_label in {"too-senior", "unhireable"}:
        group, why = "passed", _text(role.get("why"))
    elif move_label == "unhireable":
        group, why = "passed", _text(move.get("why"))
    elif _text(craft.get("label")) == "weak":
        group, why = "passed", _text(craft.get("why"))
    elif group == "send_worthy" and _text(company.get("label")) == "weak":
        group, why = "chat_worthy", _text(company.get("why"))
    elif (group in {"send_worthy", "wrong_timing_relationship"} and
          _text(craft.get("label")) == "unclear"):
        group, why = "chat_worthy", _text(craft.get("why"))
    elif group == "send_worthy" and role_label not in {"in-band", "promising step-up"}:
        group, why = "chat_worthy", _text(role.get("why"))
    elif (group == "wrong_timing_relationship" and
          role_label not in {"in-band", "promising step-up"}):
        group, why = "chat_worthy", _text(role.get("why"))
    row = dict(candidate)
    row.update({
        "fit_experts": bound_experts,
        "applied_precedent_ids": [card["id"] for card in applied],
        "applied_fit_precedents": applied,
        "group": group, "why": why,
        "fit_annotation_source": "luna",
    })
    override = row.get("fit_override")
    if (isinstance(override, Mapping) and override.get("reviewed") is True and
            _text(override.get("group")) in FIT_GROUPS and _text(override.get("why"))):
        row.update({"group": _text(override.get("group")), "why": _text(override.get("why")),
                    "fit_annotation_source": "human"})
    return row
