"""Recruiter-plan prompt construction and typed plan normalization.

The plan call is generation-constrained: `PLAN_RESPONSE_FORMAT` makes the provider enforce
`PLAN_EXTRACTION_SCHEMA` (strict structured outputs), so an extraction cannot come back with an
out-of-cap trait list or an invalid policy enum. Every cap and enum in that schema — and every
legal value the prompt states — is read from the canonical recruiter-plan JSON schema and the
`recruiter_policy` constants, so prompt, response schema, and validator cannot drift apart.
JSON Schema still cannot express the cross-field rules (location-filter family shape, display
agreement, resolved-policy invariants); those stay in `plan_from_obj` and the plan validator.

OPERATIONAL REQUIREMENT: the plan stage now requires an endpoint and model that support strict
structured outputs (`response_format={"type": "json_schema", ..., "strict": true}`). There is
deliberately NO fallback to the old unconstrained `json_object` mode — silently reverting to it is
what let a malformed plan kill a run mid-flight. An endpoint that rejects the schema fails closed
with a typed `needs_input` carrying the provider error. If you repoint `OPENAI_API_BASE` at a
proxy, or pin an older model through `RECRUIT_PLAN_MODEL`, verify strict support first.

Changelog:
  2026-08-06  strict structured-output extraction schema; prompt states the legal policy values
              and trait caps; caps/enums derived from search-network-jd-plan.schema.json and
              recruiter_policy instead of hardcoded here. Schema acceptance verified live against
              gpt-4o, gpt-5.4, and gpt-5.6-luna (the two models whose unconstrained plans failed
              production runs both self-clamp to the caps and enums under it).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import recruiter_policy
from .location_scope import (
    LOCATION_FILTER_FIELDS,
    UNSCOPED_LOCATIONS,
    canonical_location_label,
    canonicalize_generated_location_filters,
    location_scope_from_plan,
    validate_generated_location_display,
)

PLAN_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "search-network-jd-plan.schema.json"
)
PLAN_SCHEMA: dict[str, Any] = json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))

_PLAN_TRAITS = PLAN_SCHEMA["properties"]["traits"]["properties"]
MAX_MUST_HAVE: int = _PLAN_TRAITS["must_have"]["maxItems"]
MAX_NICE_TO_HAVE: int = _PLAN_TRAITS["nice_to_have"]["maxItems"]
VALID_TARGET_LEVELS: tuple[str, ...] = tuple(PLAN_SCHEMA["properties"]["target_level"]["enum"])
VALID_TIERS: tuple[str, ...] = tuple(PLAN_SCHEMA["$defs"]["mustTrait"]["properties"]["tier"]["enum"])
# The extractor declares only how IT built a group; 'user' is a reviewer-only provenance value.
DECLARED_GROUP_SOURCES: tuple[str, ...] = ("default", "jd")

PLAN_SYSTEM = (
    "You are a technical recruiter turning a job description into a structured evaluation plan "
    "for an automated candidate judge. Extract ONLY what the JD supports. Hard rules:\n"
    "- must_have traits: the evidence-checkable capabilities the JD demands. Tag EACH must_have with a "
    "`tier`:\n"
    "    * 'core' = a domain-defining differentiator that makes THIS role hard — the specific "
    "capability or domain a generically strong, senior person would NOT automatically have (e.g. "
    "'delivered large fusion/plasma hardware programs', 'built distributed schedulers at scale', "
    "'shipped LLM inference systems in production'). These define the membership gate: someone who "
    "lacks evidence for EVERY complete core path is not a real fit no matter how senior or impressive. "
    "Make core traits as SHARP and "
    "domain-specific as the JD allows — prefer the narrowest true requirement over a broad one.\n"
    "    * 'table_stakes' = generic competence most qualified seniors in this band already have "
    "(leadership, communication, strategic thinking, people/eng management, relocation/logistics). "
    "Real requirements, but NOT what separates a fit from a non-fit.\n"
    "  Core is about WHAT DOMAIN/CAPABILITY the person has built — NOT how senior, how long, or "
    "where. Stage/tenure/experience-amount traits ('early-stage startup experience', '10+ years', "
    "'worked at a big company') are table_stakes, never core. Most roles have only 1-3 core traits. "
    "NEVER mark generic leadership/communication/management/relocation/stage as core. "
    f"Emit AT MOST {MAX_MUST_HAVE} must_have traits in total (core and table_stakes combined) and "
    f"AT MOST {MAX_NICE_TO_HAVE} nice_to_have traits; a plan over either cap is rejected, so keep "
    "the sharpest ones and merge the rest. "
    "nice_to_have: real pluses the JD mentions.\n"
    "- Each trait is a short evidence-checkable phrase, NOT a sentence and NOT a job title.\n"
    "- core_groups: encode the membership gate. Groups are OR alternatives and traits within an "
    "explicit group are AND requirements. Set source='default' for the automatic one-per-core "
    "membership shape; set source='jd' ONLY when the JD explicitly defines the alternative or "
    "conjunctive path. DEFAULT: emit one group PER core trait (any single core "
    "capability qualifies for membership, while all must_have traits still inform ranking — measured against real "
    "shortlists, requiring many core traits at once gates out nearly everyone: an all-of-3 group cut a "
    "validated 22-person shortlist to 1). Define alternative/conjunctive paths ONLY when the JD truly "
    "says each path is independently viable; those explicit paths are scored separately and the best "
    "complete path wins. Never put more than 3 traits in a group. Reference core trait "
    "text exactly.\n"
    "- hire_stage: one of founding_early | scaling_late. Use founding_early for 0-to-1/ambiguous/early "
    "startup work and scaling_late for hardening, scale, mature systems, or later-stage organizations.\n"
    "- target_level: the role's career level — one of senior_ic | staff_ic | lead | manager | "
    "director | vp | exec. Infer from the title/responsibilities (an IC eng role is senior_ic/"
    "staff_ic; a 'VP of Engineering' is vp; a 'Head of X' is director/vp).\n"
    "- usable_cutoff: ONE sentence stating the target level and seniority/track policy. For IC roles, "
    "higher hands-on IC levels (staff/principal/distinguished/lead-IC) remain in-band; current "
    "management/exec/company-running identities are too_senior unless the role asks for that track. "
    "For management/exec roles, in-band is the target and one level below; one+ above is too_senior, "
    "two+ below is too_junior. Name concrete in-band and gated titles for THIS role.\n"
    "- location: the JD's required geographic recruiting scope; empty ONLY for genuinely worldwide "
    "remote/flexible/unstated roles. A country/region-restricted remote role is still geographically "
    "scoped (e.g. remote US -> location='United States'). location_filters: the exact backend scope, "
    "using exactly one supported shape: cities+one country, states+one country, metro_areas only, "
    "countries only, or macro_regions only. Values within a family are OR alternatives. Use "
    "metro_areas for a commuting market, city + country for an exact city, state + country for a "
    "state/province, countries for broad requirements, and macro_regions for explicit regions such "
    "as Europe/APAC. "
    "Europe maps to ['Western Europe','Eurasia']. For multi-office scopes in different countries, "
    "use ORed canonical metro_areas rather than parallel city/country lists. Exact backend macro "
    "values are Americas, Western Europe, Eurasia, APAC, Middle East, South Asia, and Sub-Saharan "
    "Africa. For an explicit broad Africa, Oceania, or Latin America requirement, emit the temporary "
    "macro_regions alias 'Africa', 'Oceania', or 'Latin America'; deterministic normalization expands "
    "it to the complete canonical country OR-list before Review.\n"
    "- normalized_archetype: a 2-4 word canonical role archetype (e.g. 'distributed systems engineer').\n"
    "- recruiter_preferences: OPTIONAL and only for recruiter-ranking preferences the JD states "
    "explicitly. Allowed fields are excellence_weights, pedigree_policy, and "
    "current_founder_c_suite_for_non_exec_ic, and their legal values are exact: pedigree_policy is "
    f"one of {' | '.join(sorted(recruiter_policy.PEDIGREE_POLICIES))}; "
    "current_founder_c_suite_for_non_exec_ic is one of "
    f"{' | '.join(sorted(recruiter_policy.FOUNDER_C_SUITE_POLICIES))}; "
    "excellence_weights holds non-negative numbers keyed "
    f"by {', '.join(recruiter_policy.EXCELLENCE_DIMENSIONS)}. Any other value is rejected. Never "
    "infer brand/pedigree preference or weights from company identity; emit null for the whole "
    "object — and for any individual field — the JD is silent about.\n"
    'Return strict JSON with EVERY field present: {"job_title","normalized_archetype","hire_stage",'
    '"target_level","usable_cutoff","location":"","location_filters":{"cities":[],"states":[],'
    '"countries":[],"metro_areas":[],"macro_regions":[]},'
    f'"must_have":[{{"trait":"...","tier":"{"|".join(VALID_TIERS)}"}}],'
    '"core_groups":[{"name":"<archetype>","all_of":["<exact core trait>"],'
    f'"source":"{"|".join(DECLARED_GROUP_SOURCES)}"}}],'
    '"nice_to_have":["..."],"recruiter_preferences":{...}|null}. Use "" or [] for an empty value '
    "and null only where a field is documented as optional."
)

PLAN_REPAIR_PREFIX = (
    "Your previous plan JSON was REJECTED by the plan contract. Return one corrected plan that "
    "obeys every rule above, changing only what the rejection requires. Rejection reason:\n"
)


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    """Strict structured outputs demand closed objects with every property required."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _optional(schema: dict[str, Any]) -> dict[str, Any]:
    """Strict structured outputs cannot omit a property; absence is spelled as null."""
    return {"anyOf": [schema, {"type": "null"}]}


def _strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


PLAN_EXTRACTION_SCHEMA: dict[str, Any] = _strict_object(
    {
        "job_title": {"type": "string"},
        "normalized_archetype": {"type": "string"},
        "hire_stage": {"type": "string", "enum": list(recruiter_policy.CANONICAL_HIRE_STAGES)},
        "target_level": {"type": "string", "enum": list(VALID_TARGET_LEVELS)},
        "usable_cutoff": {"type": "string"},
        "location": {"type": "string"},
        "location_filters": _strict_object({field: _strings() for field in LOCATION_FILTER_FIELDS}),
        "must_have": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_MUST_HAVE,
            "items": _strict_object(
                {
                    "trait": {"type": "string"},
                    "tier": {"type": "string", "enum": list(VALID_TIERS)},
                }
            ),
        },
        "core_groups": {
            "type": "array",
            "items": _strict_object(
                {
                    "name": {"type": "string"},
                    "all_of": _strings(),
                    "source": {"type": "string", "enum": list(DECLARED_GROUP_SOURCES)},
                }
            ),
        },
        "nice_to_have": {
            "type": "array",
            "maxItems": MAX_NICE_TO_HAVE,
            "items": {"type": "string"},
        },
        "recruiter_preferences": _optional(
            _strict_object(
                {
                    "excellence_weights": _optional(
                        _strict_object(
                            {
                                dimension: {"type": "number", "minimum": 0}
                                for dimension in recruiter_policy.EXCELLENCE_DIMENSIONS
                            }
                        )
                    ),
                    "pedigree_policy": _optional(
                        {"type": "string", "enum": sorted(recruiter_policy.PEDIGREE_POLICIES)}
                    ),
                    "current_founder_c_suite_for_non_exec_ic": _optional(
                        {
                            "type": "string",
                            "enum": sorted(recruiter_policy.FOUNDER_C_SUITE_POLICIES),
                        }
                    ),
                }
            )
        ),
    }
)

PLAN_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "recruiter_plan_extraction",
        "schema": PLAN_EXTRACTION_SCHEMA,
        "strict": True,
    },
}


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
    if not filters:
        if raw_location.lower() not in UNSCOPED_LOCATIONS:
            raise ValueError("a required location must have at least one structured filter")
        location = None
    else:
        if raw_location.lower() not in UNSCOPED_LOCATIONS:
            validate_generated_location_display(raw_location, filters)
        location = canonical_location_label(filters)
    scope = {"location": location, "filters": filters, "source": "jd"}
    location_scope_from_plan({"search_scope": scope})
    return scope


def build_plan_messages(jd: str, *, repair: str | None = None) -> list[dict[str, str]]:
    """Plan-call messages; `repair` replays one rejected attempt's contract error for correction."""
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": f"Job description:\n\n{jd.strip()}"},
    ]
    if repair:
        messages.append({"role": "user", "content": PLAN_REPAIR_PREFIX + repair})
    return messages


def _must_trait(t: Any) -> dict[str, str] | None:
    """Normalize one must_have entry into {trait, tier}. Accepts the tagged object form
    ({"trait","tier"}) and the legacy bare-string form. An unrecognized/absent tier degrades to
    'table_stakes' so a mis-tagged plan falls back to the score gate rather than over-gating
    (the core-gate only fires on traits the model EXPLICITLY marked 'core')."""
    if isinstance(t, dict):
        text = str(t.get("trait") or "").strip()
        tier = str(t.get("tier") or "").strip().lower()
        tier = tier if tier in VALID_TIERS else "table_stakes"
    else:
        text, tier = str(t).strip(), "table_stakes"
    return {"trait": text, "tier": tier, "source": "jd"} if text else None


def _core_groups(obj: dict[str, Any], must: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Normalize alternative all-of gates, falling back to one group PER core trait (any-one)."""
    core_by_norm = {_norm(t["trait"]): t["trait"] for t in must if t["tier"] == "core"}
    groups: list[dict[str, Any]] = []
    for i, raw in enumerate(obj.get("core_groups") or []):
        if not isinstance(raw, dict):
            continue
        traits: list[str] = []
        for value in raw.get("all_of") or []:
            canonical = core_by_norm.get(_norm(str(value)))
            if canonical and canonical not in traits:
                traits.append(canonical)
        if traits:
            groups.append({
                "name": str(raw.get("name") or f"archetype_{i + 1}").strip(),
                "all_of": traits,
                "declared_source": (
                    str(raw.get("source") or "").strip().lower()
                    if str(raw.get("source") or "").strip().lower() in DECLARED_GROUP_SOURCES
                    else None
                ),
            })
    if groups:
        # The extractor emits singleton groups as the measured default. Preserve that provenance so
        # the scorer can distinguish a permissive membership gate from JD-declared alternative paths.
        singleton_traits = [group["all_of"][0] for group in groups if len(group["all_of"]) == 1]
        is_default_shape = (
            len(singleton_traits) == len(groups) == len(core_by_norm)
            and {_norm(trait) for trait in singleton_traits} == set(core_by_norm)
        )
        for group in groups:
            declared_source = group.pop("declared_source")
            if len(group["all_of"]) > 1:
                group["source"] = "jd"
            elif declared_source:
                group["source"] = declared_source
            else:
                group["source"] = "default" if is_default_shape else "jd"
        return groups
    # Fallback = one group PER core trait (any-one semantics). This is the measured default:
    # a single all-of group over every core trait gated a validated 22-person shortlist to 1.
    core = [t["trait"] for t in must if t["tier"] == "core"]
    return [{"name": f"core_{i + 1}", "all_of": [trait], "source": "default"}
            for i, trait in enumerate(core)]


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def plan_from_obj(
    obj: dict[str, Any],
    *,
    set_name: str,
    set_id: str,
    source_url: str | None,
    created_at: str,
    user_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize the model's JSON into a plan.json the judge can read.

    Only the fields the judge consumes are required to be meaningful; the rest are filled with
    sane, schema-shaped defaults so the artifact is self-describing.
    """
    must = [o for o in (_must_trait(t) for t in (obj.get("must_have") or [])) if o]
    nice = [{"trait": str(t).strip(), "source": "jd"} for t in (obj.get("nice_to_have") or []) if str(t).strip()]
    if not must:
        raise ValueError("plan extraction produced no must_have traits")
    target_level = str(obj.get("target_level") or "senior_ic").strip().lower()
    if target_level not in VALID_TARGET_LEVELS:
        target_level = "senior_ic"
    try:
        hire_stage = recruiter_policy.canonicalize_hire_stage(
            str(obj.get("hire_stage") or "founding_early")
        )
    except recruiter_policy.RecruiterPolicyError:
        hire_stage = "founding_early"
    # Strict structured outputs spell "the JD said nothing" as null, so drop nulls before the
    # policy resolver, which only accepts real override values.
    jd_preferences = {
        key: value
        for key, value in (obj.get("recruiter_preferences") or {}).items()
        if value is not None
    }
    jd_preferences["hire_stage"] = hire_stage
    resolved_policy = recruiter_policy.resolve_recruiter_preferences(
        user_preferences=user_preferences,
        jd_preferences=jd_preferences,
    )
    job_title = str(obj.get("job_title") or "role").strip()
    normalized_archetype = str(obj.get("normalized_archetype") or job_title).strip()
    return {
        "route": "deep",
        "parse_only": False,
        "retrieval_ran": False,
        "job_id": "deep",
        "job_title": job_title,
        "normalized_archetype": normalized_archetype,
        "source_url": source_url,
        "source_title": None,
        "set_scope": {"name": set_name, "set_id": set_id},
        "search_scope": _search_scope(obj),
        "hire_stage": resolved_policy["preferences"]["hire_stage"],
        "target_level": target_level,
        "usable_cutoff": str(obj.get("usable_cutoff") or "Senior in-band IC; executives, founders, and advisors are out.").strip(),
        "traits": {"must_have": must, "nice_to_have": nice},
        "core_groups": _core_groups(obj, must),
        "recruiter_policy": resolved_policy,
        "created_at": created_at,
    }
