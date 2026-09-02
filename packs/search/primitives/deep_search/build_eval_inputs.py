"""Extract the reviewed recruiter plan from a JD in two model calls.

Call 1, the plan: title, archetype, pond prompt family, hire stage, target
level, usable cutoff, location scope, filters, JD-quoted candidate populations,
comp band. Call 2, the traits: the flat ordered person-trait list, prompted by
the family call 1 chose (`prompts/traits.txt` or
`prompts/families/<family>/traits.txt`) from the JD plus the role brief.

Writes `plan.raw.json` and `traits.raw.json` (verbatim responses) and
`plan.json` (the normalized contract). The search harness reads plan.json
before the single human Review.

Changelog:
  2026-09-02  Traits are a flat ordered list of person-traits
              ({trait, kind, evidence_quote}, 3-6, verbatim quote or dropped)
              from a second per-family call. must_have / nice_to_have /
              core_groups and the ranking-boost, tool-culture, comp-band-anchor
              hint kinds are gone; the plan prompt stands alone instead of
              composing on trait_generation.txt.
  2026-09-02  The union -> frontier bridge for the deleted exhaustive judge is
              gone; this module only extracts the plan.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from openai_client import make_openai_client  # noqa: E402

try:  # direct script execution
    from location_scope import (
        CONTINENT_COUNTRIES,
        LOCATION_FILTER_FIELDS,
        UNSCOPED_LOCATIONS,
        canonical_location_label,
        canonicalize_generated_location_filters,
        location_scope_from_plan,
    )
    from plan_filters import bind_plan_filters, normalize_plan_filters
    from pond_prompts import POND_PROMPT_FAMILIES, load_pond_prompt
    import recruiter_policy as recruiter_policy
except ImportError:  # module execution
    from .location_scope import (
        CONTINENT_COUNTRIES,
        LOCATION_FILTER_FIELDS,
        UNSCOPED_LOCATIONS,
        canonical_location_label,
        canonicalize_generated_location_filters,
        location_scope_from_plan,
    )
    from .plan_filters import bind_plan_filters, normalize_plan_filters
    from .pond_prompts import POND_PROMPT_FAMILIES, load_pond_prompt
    from . import recruiter_policy

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = os.environ.get("RECRUIT_PLAN_MODEL", "gpt-4o")

PLAN_SYSTEM = r"""
You read a job description and return the reviewed recruiter plan: the role, its level and
location scope, retrieval filters, JD-grounded candidate populations, and any posted compensation
band. Do not return traits, must-haves, or nice-to-haves; a separate call extracts traits.

- `job_title`: the posting title as written.
- `hiring_company_name`: the company hiring for this role, taken from the JD itself.
- `normalized_archetype`: a 2-4 word canonical role archetype.
- `pond_prompt_family`: choose exactly one of `engineering`, `marketing-sales`,
  `customer-support`, `operations-finance-people`, `design`, or `general`. Choose from the
  occupation that owns the recurring work and the full JD. A listed department is supporting
  evidence only; when it conflicts with a concrete role title and recurring work, follow the work.
- `hire_stage`: `founding_early` for 0-to-1/ambiguous/early startup work; `scaling_late` for
  hardening, scale, mature systems, or later-stage organizations.
- `target_level`: one of `senior_ic|staff_ic|lead|manager|director|vp|exec`.
- `usable_cutoff`: one sentence naming the concrete in-band and gated levels/tracks for this role.
- `location` and `location_filters`: required geographic scope. Empty means genuinely worldwide,
  flexible, or unstated. Supported shapes are cities+one country, states+one country,
  metro_areas-only, countries-only, or macro_regions-only. Macro regions are `Americas`,
  `Western Europe`, `Eurasia`, `APAC`, `Middle East`, `South Asia`, and `Sub-Saharan Africa`.
  If the role accepts remote candidates without naming a required geographic place, including
  worldwide or global remote, return empty location and filters; do not combine an optional office
  with that unscoped eligibility.
  When the posting header names one location and the body only permits an alternate office, use the
  header location.
  Prefer the canonical indexed metro for an explicit US city when the mapping is unambiguous
  (for example New York -> New York Metropolitan Area and San Francisco -> San Francisco Bay
  Area). A required European city or country uses Europe, represented by the two Europe macro
  regions. For other non-US locations, use the country.
  Europe maps to `["Western Europe","Eurasia"]`. `Africa`, `Oceania`, and `Latin America` are
  accepted aliases that deterministic normalization expands before review.
- `filters`: only constraints that should shrink the initial retrieval pond before a person is
  inspected: required location/work authorization and true license, credential, fiduciary,
  executive-authority, or occupational gates. Ordinary JD years of experience, current level,
  pedigree, company stage, employer identity, and preferred background are ranking evidence, not
  initial retrieval filters; express the in-band expectation in `usable_cutoff`. Do not use a
  protected-attribute proxy for career stage.
- `recruiter_preferences`: optional, only when the JD explicitly states ranking preferences.
  Allowed fields: `excellence_weights`, `pedigree_policy`, and
  `current_founder_c_suite_for_non_exec_ic`. Do not infer pedigree preference from company identity.
- `candidate_populations`: grounded search-expansion hints mined from the JD: source occupations
  worth retrieving as a pond. Every entry must contain a terse phrase, one exact contiguous quote
  copied verbatim from the JD, and exactly one `hint_kind`:
  - `stated-background`: the JD directly says it seeks a named occupation or prior background.
  - `dual-craft-sentence`: one sentence combines two substantial professional crafts; record the
    credible source-population direction implied by each craft.
  - `portfolio-signal`: portfolio or work-sample language reveals the craft culture that owns the work.
  - `department-title-tension`: the named department and destination title point to different crafts.
  - `feeder-career-language`: the JD explicitly licenses a prior career as a route into the role.
  - `situational-population`: a stated team or culture fact licenses candidates in that current situation,
    including a background explicitly shared by the existing team.
  - `capability-adjacent`: the recurring work maps to a genuinely different occupation that performs
    the defining capability, rather than a domain-qualified version of the destination title. Preserve
    the named technical paradigm when the work centers on formal languages, compilers, rules engines,
    runtimes, or another recognizable systems specialty; do not replace it with a vague quality claim.
  Inspect the title, department, candidate-background language, portfolio/work-sample requirements,
  recurring work, and culture statements before returning. Use those definitions as precedence when
  one quote could fit several kinds: a direct hiring declaration naming an occupation is
  `stated-background`; a background shared by the team in a culture statement is
  `situational-population`; recurring defining work that maps to another occupation is
  `capability-adjacent`. Exhaust these grounded hints. A stated-background quote must literally name
  an occupation or prior background, not merely describe a duty. `population` must be a search-ready
  established source occupation, optionally with one defining capability; omit destination
  seniority. Never an industry: the market, customers, or product the company serves is not a
  population and never qualifies one. For dual-craft or department-title tension, emit each credible
  source-occupation direction separately: each side's established occupation plus the other
  indispensable craft. Do not substitute the destination's internal hybrid title. One quote may
  therefore ground more than one entry. Before returning, verify independently that the output
  preserves every supported direct hiring occupation, team-background situation, and recurring
  adjacent technical specialty. Do not invent a hint without a verbatim supporting quote. The same
  quote may support multiple hint kinds when it genuinely carries multiple signals.
- `comp_band`: the normalized posted base-compensation range, or null when none is stated. It contains
  `currency`, numeric `minimum` and `maximum`, `period` (`year|month|hour|unknown`), and the exact
  contiguous JD quote.

Extract only what the JD supports. Return strict JSON:
{"job_title":"...","hiring_company_name":"...","normalized_archetype":"...","pond_prompt_family":"engineering|marketing-sales|customer-support|operations-finance-people|design|general","hire_stage":"founding_early|scaling_late",
"target_level":"senior_ic|staff_ic|lead|manager|director|vp|exec","usable_cutoff":"...",
"location":"","location_filters":{"cities":[],"states":[],"countries":[],"metro_areas":[],
"macro_regions":[]},"filters":["plain-English constraint"],
"candidate_populations":[{"population":"...","hint_kind":"stated-background|dual-craft-sentence|portfolio-signal|department-title-tension|feeder-career-language|situational-population|capability-adjacent","evidence_quote":"exact JD quote"}],
"comp_band":{"currency":"...","minimum":0,"maximum":0,"period":"year|month|hour|unknown","evidence_quote":"exact JD quote"}|null,
"recruiter_preferences":{...}}
""".strip()

VALID_TARGET_LEVELS = {"senior_ic", "staff_ic", "lead", "manager", "director", "vp", "exec"}
VALID_HINT_KINDS = {
    "stated-background", "dual-craft-sentence", "portfolio-signal",
    "department-title-tension", "feeder-career-language",
    "situational-population", "capability-adjacent",
}
TRAIT_KINDS = {"capability", "background", "tool"}
MIN_TRAITS = 3
MAX_TRAITS = 6


def _search_scope(obj: dict[str, Any]) -> dict[str, Any]:
    raw_location = str(obj.get("location") or "").strip()
    raw_filters = obj.get("location_filters", {})
    if not isinstance(raw_filters, dict):
        raise ValueError("plan extraction produced invalid location_filters")
    unknown = sorted(set(raw_filters) - set(LOCATION_FILTER_FIELDS))
    if unknown:
        raise ValueError(f"plan extraction produced unsupported location filters: {unknown}")
    filters: dict[str, list[str]] = {}
    for field in LOCATION_FILTER_FIELDS:
        values = raw_filters.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"plan extraction produced invalid location_filters.{field}")
        cleaned = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if cleaned:
            filters[field] = cleaned
    filters = canonicalize_generated_location_filters(raw_location, filters)
    countries = set(filters.get("countries") or [])
    if countries and countries <= set(CONTINENT_COUNTRIES["Europe"]):
        filters = {"macro_regions": ["Western Europe", "Eurasia"]}
    elif filters == {"macro_regions": ["Western Europe"]}:
        filters = {"macro_regions": ["Western Europe", "Eurasia"]}
    if not filters:
        if raw_location.lower() not in UNSCOPED_LOCATIONS:
            raise ValueError("a required location must have at least one structured filter")
        location = None
    else:
        # The model owns semantic extraction. Structured filters are canonicalized
        # above and become authoritative; the displayed label is derived from them.
        location = canonical_location_label(filters)
    scope = {"location": location, "filters": filters, "source": "jd"}
    location_scope_from_plan({"search_scope": scope})
    return scope


def _chat_request(
    messages: list[dict[str, str]], *, model: str,
    reasoning_effort: str | None, service_tier: str | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model, "messages": messages, "response_format": {"type": "json_object"},
    }
    if reasoning_effort:
        request["reasoning_effort"] = reasoning_effort
    if service_tier:
        request["service_tier"] = service_tier
    return request


def build_plan_messages(
    jd: str,
    system_prompt: str = PLAN_SYSTEM,
    source_metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    department = str((source_metadata or {}).get("department") or "").strip()
    hint = f"Source department hint: {department}\n\n" if department else ""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"{hint}Job description:\n\n{jd.strip()}"},
    ]


def plan_request(
    *, jd: str, model: str, system_prompt: str = PLAN_SYSTEM,
    reasoning_effort: str | None = None, service_tier: str | None = None,
    source_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _chat_request(
        build_plan_messages(jd, system_prompt, source_metadata),
        model=model, reasoning_effort=reasoning_effort, service_tier=service_tier,
    )


def role_brief(obj: Mapping[str, Any]) -> dict[str, str]:
    """The plan-call fields the traits call sees: title, archetype, level, and the prompt family."""
    job_title = str(obj.get("job_title") or "role").strip()
    target_level = str(obj.get("target_level") or "senior_ic").strip().lower()
    if target_level not in VALID_TARGET_LEVELS:
        target_level = "senior_ic"
    family = str(obj.get("pond_prompt_family") or "general").strip().lower()
    if family not in POND_PROMPT_FAMILIES:
        family = "general"
    return {
        "job_title": job_title,
        "normalized_archetype": str(obj.get("normalized_archetype") or job_title).strip(),
        "target_level": target_level,
        "pond_prompt_family": family,
    }


def build_traits_messages(
    jd: str, brief: Mapping[str, str], system_prompt: str,
) -> list[dict[str, str]]:
    role = {key: brief[key] for key in ("job_title", "normalized_archetype", "target_level")}
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Role:\n{json.dumps(role, indent=2)}\n\nJob description:\n\n{jd.strip()}"
        )},
    ]


def traits_request(
    *, jd: str, brief: Mapping[str, str], model: str, system_prompt: str,
    reasoning_effort: str | None = None, service_tier: str | None = None,
) -> dict[str, Any]:
    return _chat_request(
        build_traits_messages(jd, brief, system_prompt),
        model=model, reasoning_effort=reasoning_effort, service_tier=service_tier,
    )


def _traits(obj: Mapping[str, Any], jd_text: str | None) -> list[dict[str, str]]:
    """Verbatim-quoted traits of a known kind, in the model's order, deduped, at most MAX_TRAITS."""
    traits: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in obj.get("traits") or []:
        if not isinstance(row, Mapping):
            continue
        trait = " ".join(str(row.get("trait") or "").split())
        kind = str(row.get("kind") or "").strip().casefold()
        quote = str(row.get("evidence_quote") or "").strip()
        if (not trait or kind not in TRAIT_KINDS or not quote or
                (jd_text is not None and quote not in jd_text)):
            continue
        key = _norm(trait)
        if key in seen:
            continue
        seen.add(key)
        traits.append({"trait": trait, "kind": kind, "evidence_quote": quote})
    return traits[:MAX_TRAITS]


def _candidate_populations(obj: Mapping[str, Any], jd_text: str | None) -> list[dict[str, str]]:
    populations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in obj.get("candidate_populations") or []:
        if not isinstance(row, Mapping):
            continue
        population = " ".join(str(row.get("population") or "").split())
        hint_kind = str(row.get("hint_kind") or "").strip().casefold()
        quote = str(row.get("evidence_quote") or "").strip()
        if (not population or hint_kind not in VALID_HINT_KINDS or not quote or
                (jd_text is not None and quote not in jd_text)):
            continue
        key = (_norm(population), hint_kind, quote)
        if key not in seen:
            seen.add(key)
            populations.append({
                "population": population, "hint_kind": hint_kind, "evidence_quote": quote,
            })
    return populations[:12]


def _comp_band(obj: Mapping[str, Any], jd_text: str | None) -> dict[str, Any] | None:
    raw = obj.get("comp_band")
    if not isinstance(raw, Mapping):
        return None
    quote = str(raw.get("evidence_quote") or "").strip()
    minimum, maximum = raw.get("minimum"), raw.get("maximum")
    if (not quote or (jd_text is not None and quote not in jd_text) or
            isinstance(minimum, bool) or isinstance(maximum, bool) or
            not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)) or
            minimum < 0 or maximum < minimum):
        return None
    period = str(raw.get("period") or "unknown").strip().casefold()
    if period not in {"year", "month", "hour", "unknown"}:
        period = "unknown"
    return {
        "currency": str(raw.get("currency") or "").strip().upper(),
        "minimum": int(minimum) if float(minimum).is_integer() else float(minimum),
        "maximum": int(maximum) if float(maximum).is_integer() else float(maximum),
        "period": period,
        "evidence_quote": quote,
    }


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def plan_from_obj(
    obj: dict[str, Any],
    traits_obj: Mapping[str, Any],
    *,
    set_name: str,
    set_id: str,
    source_url: str | None,
    created_at: str,
    user_preferences: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
    jd_text: str | None = None,
) -> dict[str, Any]:
    """Normalize the plan call's JSON and the traits call's JSON into plan.json."""
    traits = _traits(traits_obj, jd_text)
    if len(traits) < MIN_TRAITS:
        raise ValueError(
            f"trait extraction produced {len(traits)} traits; {MIN_TRAITS}-{MAX_TRAITS} required"
        )
    brief = role_brief(obj)
    try:
        hire_stage = recruiter_policy.canonicalize_hire_stage(
            str(obj.get("hire_stage") or "founding_early")
        )
    except recruiter_policy.RecruiterPolicyError:
        hire_stage = "founding_early"
    # Search generation needs the role/JD evidence, not model-authored taste
    # policy. Keep operator preferences explicit and use defaults otherwise.
    resolved_policy = recruiter_policy.resolve_recruiter_preferences(
        user_preferences=user_preferences,
        jd_preferences={"hire_stage": hire_stage},
    )
    search_scope = _search_scope(obj)
    plan_filters = normalize_plan_filters(obj.get("filters"))
    if search_scope["location"]:
        plan_filters = normalize_plan_filters([
            *plan_filters,
            {"filter": f"Based in {search_scope['location']}", "source": "jd"},
        ])
    source_metadata = source_metadata or {}
    hiring_company_name = str(
        obj.get("hiring_company_name") or source_metadata.get("company_name") or ""
    ).strip()
    hiring_company_website = str(source_metadata.get("company_website_url") or "").strip() or None
    plan = {
        "route": "deep",
        "parse_only": False,
        "retrieval_ran": False,
        "job_id": "deep",
        "job_title": brief["job_title"],
        "normalized_archetype": brief["normalized_archetype"],
        "pond_prompt_family": brief["pond_prompt_family"],
        "source_url": source_url,
        "source_title": None,
        "hiring_company": {
            "name": hiring_company_name or None,
            "website_url": hiring_company_website,
        },
        "candidate_populations": _candidate_populations(obj, jd_text),
        "comp_band": _comp_band(obj, jd_text),
        "set_scope": {"name": set_name, "set_id": set_id},
        "search_scope": search_scope,
        "hire_stage": resolved_policy["preferences"]["hire_stage"],
        "target_level": brief["target_level"],
        "usable_cutoff": str(obj.get("usable_cutoff") or "Senior in-band IC; executives, founders, and advisors are out.").strip(),
        "traits": traits,
        "filters": plan_filters,
        "recruiter_policy": resolved_policy,
        "created_at": created_at,
    }
    return bind_plan_filters(plan)


def _complete(client: Any, request: dict[str, Any], raw_path: Path | None) -> str:
    """One chat call; the verbatim response is checkpointed before it is parsed."""
    response = client.chat.completions.create(**request)
    raw = response.choices[0].message.content or "{}"
    if raw_path:
        raw_path.write_text(raw, encoding="utf-8")
    return raw


def extract_plan(
    *,
    jd_file: Path,
    set_name: str,
    set_id: str,
    source_url: str | None,
    created_at: str,
    model: str,
    api_key: str | None,
    user_preferences: dict[str, Any] | None = None,
    system_prompt: str = PLAN_SYSTEM,
    traits_system_prompt: str | None = None,
    reasoning_effort: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    raw_response_path: Path | None = None,
    traits_response_path: Path | None = None,
    client: Any | None = None,
    service_tier: str | None = None,
) -> dict[str, Any]:
    """The plan call, then the traits call prompted by the family the plan call chose."""
    if client is None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        client = make_openai_client(key)
    jd = jd_file.read_text(encoding="utf-8")
    plan_obj = json.loads(_complete(client, plan_request(
        jd=jd, model=model, system_prompt=system_prompt,
        reasoning_effort=reasoning_effort, service_tier=service_tier,
        source_metadata=source_metadata,
    ), raw_response_path))
    brief = role_brief(plan_obj)
    traits_obj = json.loads(_complete(client, traits_request(
        jd=jd, brief=brief, model=model,
        system_prompt=traits_system_prompt or load_pond_prompt(brief, "traits"),
        reasoning_effort=reasoning_effort, service_tier=service_tier,
    ), traits_response_path))
    return plan_from_obj(
        plan_obj,
        traits_obj,
        set_name=set_name,
        set_id=set_id,
        source_url=source_url,
        created_at=created_at,
        user_preferences=user_preferences,
        source_metadata=source_metadata,
        jd_text=jd,
    )


def load_source_metadata(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("source metadata must be an object")
    return document


def load_user_preferences(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return recruiter_policy.validate_recruiter_preferences(document, source="user_preferences")


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract the reviewed recruiter plan (plan.json) from a JD.")
    ap.add_argument("--run-dir", required=True,
                    help="Directory that receives plan.raw.json, traits.raw.json and plan.json")
    ap.add_argument("--jd-file", required=True, help="Path to the JD text")
    ap.add_argument("--set-id", default=os.environ.get("POWERPACKS_DEFAULT_SET_ID", ""))
    ap.add_argument("--set-name", default="deep-search set")
    ap.add_argument("--source-url", default=None)
    ap.add_argument("--source-json", default=None)
    ap.add_argument("--created-at", required=True, help="ISO timestamp for the plan")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reasoning-effort", default=None,
                    help="Optional reasoning effort for plan generation")
    ap.add_argument("--system-file", default=None,
                    help="Reviewed plan-generation system prompt; defaults to the shipped prompt")
    ap.add_argument("--api-key", default=None)
    ap.add_argument(
        "--preferences",
        default=None,
        help="Optional recruiter-preferences JSON; explicit user values override JD inference and defaults",
    )
    args = ap.parse_args()

    system_prompt = (Path(args.system_file).read_text(encoding="utf-8")
                     if args.system_file else PLAN_SYSTEM)
    if not system_prompt.strip():
        ap.error("plan-generation system prompt must not be empty")

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        plan = extract_plan(
            jd_file=Path(args.jd_file),
            set_name=args.set_name,
            set_id=args.set_id,
            source_url=args.source_url,
            created_at=args.created_at,
            model=args.model,
            api_key=args.api_key,
            user_preferences=load_user_preferences(args.preferences),
            system_prompt=system_prompt,
            reasoning_effort=args.reasoning_effort,
            source_metadata=load_source_metadata(args.source_json),
            raw_response_path=run_dir / "plan.raw.json",
            traits_response_path=run_dir / "traits.raw.json",
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": str(exc)}))
        raise SystemExit(1) from exc
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "primitive": "build_eval_inputs",
        "status": "awaiting_plan_approval",
        "plan": str(run_dir / "plan.json"),
        "pond_prompt_family": plan["pond_prompt_family"],
        "traits": len(plan["traits"]),
    }, indent=2))


if __name__ == "__main__":
    main()
