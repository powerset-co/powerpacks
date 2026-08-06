"""Backend-neutral hard-filter intent and hydrated-evidence validation."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .models import ResolvedSources, SearchSpec
from ..reflect.snapshots import canonical_hash


HARD_FILTER_FIELDS = (
    "role_ids",
    "cities",
    "states",
    "countries",
    "metro_areas",
    "seniority_bands",
    "role_tracks",
    "education_ids",
    "is_current_role",
    "years_experience_min",
    "years_experience_max",
    "company_ids",
    "investor_names",
    "sector_types",
    "technology_types",
    "entity_types",
    "funding_stage_min",
    "funding_stage_max",
    "headcount_min",
    "headcount_max",
    "is_current_company",
    "tech_skills",
)

_NON_FUNCTIONAL_ROLE_FAMILIES = frozenset({"general", "noise"})
_ROLE_FAMILY_ALIASES = {
    "business_development": "business_dev",
    "data": "data_ml",
    "human_resources": "people_hr",
    "people": "people_hr",
}
_TITLE_SENIORITY_WORDS = frozenset({
    "chief", "director", "distinguished", "executive", "head", "junior", "lead",
    "manager", "mid", "principal", "senior", "sr", "staff", "vice", "president", "vp",
    "i", "ii", "iii", "iv",
})
_TITLE_ROLE_ALIASES = {
    "director_of_engineering": "engineering_manager",
    "engineer": "software_engineer",
    "engineering_director": "engineering_manager",
    "founding_engineer": "software_engineer",
    "head_of_engineering": "engineering_manager",
    "vice_president_of_engineering": "engineering_manager",
    "vp_engineering": "engineering_manager",
    "vp_of_engineering": "engineering_manager",
}


def _normalize_role_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


@lru_cache(maxsize=1)
def _role_taxonomy() -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "roles" / "canonical_role_taxonomy.json").read_text()
    )
    role_families: dict[str, set[str]] = {}
    families = frozenset(str(value) for value in data["departments"])
    for family, payload in data["departments"].items():
        for role_id in payload.get("functions") or ():
            normalized = _normalize_role_value(role_id)
            role_families.setdefault(normalized, set()).add(family)
    return {role_id: frozenset(values) for role_id, values in role_families.items()}, families


def _normalized_role_family(value: Any) -> str | None:
    normalized = _normalize_role_value(value)
    normalized = _ROLE_FAMILY_ALIASES.get(normalized, normalized)
    families = _role_taxonomy()[1]
    return normalized if normalized in families and normalized not in _NON_FUNCTIONAL_ROLE_FAMILIES else None


def _title_role_id(value: Any, role_families: Mapping[str, frozenset[str]]) -> str | None:
    words = _normalize_role_value(value).split("_")
    while words:
        normalized = "_".join(words)
        normalized = _TITLE_ROLE_ALIASES.get(normalized, normalized)
        if normalized in role_families:
            return normalized
        if words[0] in _TITLE_SENIORITY_WORDS:
            words.pop(0)
            continue
        if words[-1] in _TITLE_SENIORITY_WORDS:
            words.pop()
            continue
        return None
    return None


def _target_role_families(spec: SearchSpec) -> frozenset[str]:
    role_families, _ = _role_taxonomy()
    families = {
        family
        for value in spec.person_filters.role_tracks
        if (family := _normalized_role_family(value)) is not None
    }
    role_ids = [*spec.role.role_ids]
    role_ids.extend(
        role_id
        for title in spec.role.titles
        if (role_id := _title_role_id(title, role_families)) is not None
    )
    for role_id in role_ids:
        families.update(
            role_families.get(_normalize_role_value(role_id), frozenset()) - _NON_FUNCTIONAL_ROLE_FAMILIES
        )
    return frozenset(families)


def _current_role_family_mismatch(
    profile: Mapping[str, Any], spec: SearchSpec, structured: Mapping[str, Any] | None = None
) -> bool:
    """Reject only a confident structured current-family mismatch."""
    if spec.person_filters.is_current_role is not True:
        return False
    target = _target_role_families(spec)
    if not target:
        return False
    positions = profile.get("positions") or profile.get("position_history") or []
    if not isinstance(positions, list):
        return False
    current = [row for row in positions if isinstance(row, Mapping) and row.get("is_current") is True]
    if not current:
        return False
    role_families = _role_taxonomy()[0]
    structured_rows = (structured or {}).get("_contributions")
    if not isinstance(structured_rows, (list, tuple)):
        structured_rows = (structured or {},)
    structured_rows = tuple(
        row
        for row in structured_rows
        if isinstance(row, Mapping) and row.get("is_current") is True
    )

    def matching_structured_rows(position: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        position_id = _normalize_role_value(
            position.get("position_id") or position.get("id") or position.get("linkedin_position_id")
        )
        title = _normalize_role_value(
            position.get("position_title") or position.get("title") or position.get("raw_title")
        )
        companies = {
            _normalize_role_value(position.get(field))
            for field in ("company_id", "company_name", "company")
            if position.get(field)
        }
        matches = []
        for row in structured_rows:
            row_id = _normalize_role_value(
                row.get("position_id") or row.get("id") or row.get("linkedin_position_id")
            )
            if position_id and row_id:
                if position_id == row_id:
                    matches.append(row)
                continue
            row_title = _normalize_role_value(
                row.get("position_title") or row.get("title") or row.get("raw_title")
            )
            if not title or row_title != title:
                continue
            row_companies = {
                _normalize_role_value(row.get(field))
                for field in ("company_id", "company_name", "company")
                if row.get(field)
            }
            if not companies or not row_companies or not companies.isdisjoint(row_companies):
                matches.append(row)
        return tuple(matches)

    observed: set[str] = set()
    for row in current:
        row_families: set[str] = set()
        family = _normalized_role_family(row.get("role_track"))
        if family is not None:
            row_families.add(family)
        raw_role_ids = row.get("role_ids") or ()
        for role_id in (raw_role_ids,) if isinstance(raw_role_ids, str) else raw_role_ids:
            row_families.update(
                role_families.get(_normalize_role_value(role_id), frozenset()) - _NON_FUNCTIONAL_ROLE_FAMILIES
            )
        row_title = _normalize_role_value(
            row.get("position_title") or row.get("title") or row.get("raw_title")
        )
        title_role_id = _title_role_id(row_title, role_families)
        if title_role_id is not None:
            row_families.update(role_families[title_role_id] - _NON_FUNCTIONAL_ROLE_FAMILIES)
        for evidence in matching_structured_rows(row):
            family = _normalized_role_family(evidence.get("role_track"))
            if family is not None:
                row_families.add(family)
            raw_role_ids = evidence.get("role_ids") or ()
            for role_id in (raw_role_ids,) if isinstance(raw_role_ids, str) else raw_role_ids:
                row_families.update(
                    role_families.get(_normalize_role_value(role_id), frozenset())
                    - _NON_FUNCTIONAL_ROLE_FAMILIES
                )
        observed.update(row_families)
    return bool(observed) and observed.isdisjoint(target)


def required_hard_filters(spec: SearchSpec) -> tuple[str, ...]:
    values = {
        "role_ids": spec.role.role_ids,
        "cities": spec.person_filters.cities,
        "states": spec.person_filters.states,
        "countries": spec.person_filters.countries,
        "metro_areas": spec.person_filters.metro_areas,
        "seniority_bands": spec.person_filters.seniority_bands,
        "role_tracks": spec.person_filters.role_tracks,
        "education_ids": spec.person_filters.education_ids or spec.person_filters.education_names,
        "is_current_role": spec.person_filters.is_current_role,
        "years_experience_min": spec.person_filters.years_experience_min,
        "years_experience_max": spec.person_filters.years_experience_max,
        "company_ids": spec.company_filters.company_ids or spec.company_filters.company_names,
        "investor_names": spec.company_filters.investor_names,
        "sector_types": spec.company_filters.sector_types,
        "technology_types": spec.company_filters.technology_types,
        "entity_types": spec.company_filters.entity_types,
        "funding_stage_min": spec.company_filters.funding_stage_min,
        "funding_stage_max": spec.company_filters.funding_stage_max,
        "headcount_min": spec.company_filters.headcount_min,
        "headcount_max": spec.company_filters.headcount_max,
        "is_current_company": spec.company_filters.is_current_company,
        "tech_skills": spec.tech_skills,
    }
    return tuple(key for key in HARD_FILTER_FIELDS if values[key] not in (None, (), []))


def unsupported_hard_filters(spec: SearchSpec, supported: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(field for field in required_hard_filters(spec) if field not in supported)


def _validation_findings_for_branch(
    profile: Mapping[str, Any] | None,
    spec: SearchSpec,
    sources: ResolvedSources,
    branch: str | None,
    structured: Mapping[str, Any] | None = None,
) -> dict[str, tuple[str, ...]]:
    if profile is None:
        return {"violations": (), "unknowns": ("missing_hydration",)}
    positions = profile.get("positions") or profile.get("position_history") or []
    if not isinstance(positions, list):
        positions = []
    else:
        positions = [row for row in positions if isinstance(row, Mapping)]
    current = [row for row in positions if row.get("is_current") is True]
    role_branch = branch != "company_union"
    company_branch = branch != "summary" and (branch == "company_union" or spec.role.search_mode != "COMPANY_UNION")
    if role_branch and spec.person_filters.is_current_role is True:
        relevant = current
    elif company_branch and spec.company_filters.is_current_company is True:
        relevant = current
    elif company_branch and spec.company_filters.is_current_company is False:
        relevant = [row for row in positions if row.get("is_current") is False]
    else:
        relevant = positions
    violations: list[str] = []
    unknowns: list[str] = []

    def values(rows: list[Mapping[str, Any]], *fields: str) -> list[Any]:
        out: list[Any] = []
        for row in rows:
            for field in fields:
                raw = row.get(field)
                out.extend(raw if isinstance(raw, (list, tuple)) else [raw] if raw is not None else [])
        return out

    def require_any(code: str, observed: list[Any], wanted: tuple[str, ...]) -> None:
        if not wanted:
            return
        if not observed:
            unknowns.append(f"{code}_unknown")
            return
        expected = {str(value).casefold() for value in wanted}
        if not any(str(value).casefold() in expected for value in observed):
            violations.append(f"{code}_mismatch")

    if role_branch and spec.person_filters.is_current_role is True and not current:
        (unknowns if not positions else violations).append(
            "is_current_role_unknown" if not positions else "is_current_role_mismatch"
        )
    if role_branch:
        require_any("role_ids", values(relevant, "role_ids"), spec.role.role_ids)
        require_any("seniority_band", values(relevant, "seniority_band"), spec.person_filters.seniority_bands)
        require_any("role_track", values(relevant, "role_track"), spec.person_filters.role_tracks)
        if _current_role_family_mismatch(profile, spec, structured):
            violations.append("current_role_family_mismatch")
    if company_branch:
        require_any("company_id", values(relevant, "company_id"), sources.company_ids)
        require_any("investor_names", values(relevant, "investor_names"), sources.investor_names)

    for code, wanted, fields in (
        ("city", spec.person_filters.cities, ("city",)),
        ("state", spec.person_filters.states, ("state",)),
        ("country", spec.person_filters.countries, ("country",)),
        ("metro_areas", spec.person_filters.metro_areas, ("metro_areas", "metro_area")),
    ):
        observed = [profile.get(field) for field in fields if profile.get(field) is not None]
        observed += values(relevant, *fields)
        require_any(code, observed, wanted)

    years = profile.get("total_years_experience")
    if years is None:
        years = next(
            (row.get("total_years_experience") for row in relevant if row.get("total_years_experience") is not None),
            None,
        )
    for code, bound, bad in (
        ("years_experience_min", spec.person_filters.years_experience_min, lambda value, limit: value < limit),
        ("years_experience_max", spec.person_filters.years_experience_max, lambda value, limit: value > limit),
    ):
        if bound is not None:
            if years is None:
                unknowns.append(f"{code}_unknown")
            elif bad(float(years), bound):
                violations.append(f"{code}_mismatch")

    education = profile.get("education") or profile.get("educations") or []
    education_values = values(
        education if isinstance(education, list) else [], "canonical_education_id", "education_id", "school_id"
    )
    require_any("education_ids", education_values, sources.education_ids)

    if company_branch:
        archetype_resolved = any(
            record.get("source") == "company_archetype"
            and record.get("disposition") == "resolved"
            for record in sources.records
        )
        if not archetype_resolved:
            company_checks = (
                ("sector_types", spec.company_filters.sector_types, ("company_sector_types", "sector_types")),
                (
                    "technology_types",
                    spec.company_filters.technology_types,
                    ("company_technology_types", "technology_types"),
                ),
                ("entity_types", spec.company_filters.entity_types, ("company_entity_types", "entity_types")),
            )
            for code, wanted, fields in company_checks:
                require_any(code, values(relevant, *fields), wanted)
        if spec.company_filters.is_current_company is not None:
            observed = [row.get("is_current") for row in relevant if row.get("is_current") is not None]
            if not observed:
                unknowns.append("is_current_company_unknown")
            elif spec.company_filters.is_current_company not in observed:
                violations.append("is_current_company_mismatch")
        for code, bound, field, bad in (() if archetype_resolved else (
            (
                "headcount_min",
                spec.company_filters.headcount_min,
                "company_headcount",
                lambda value, limit: value < limit,
            ),
            (
                "headcount_max",
                spec.company_filters.headcount_max,
                "company_headcount",
                lambda value, limit: value > limit,
            ),
        )):
            if bound is not None:
                observed = values(relevant, field)
                if not observed:
                    unknowns.append(f"{code}_unknown")
                elif all(bad(float(value), bound) for value in observed):
                    violations.append(f"{code}_mismatch")
        stage_order = {
            name: index
            for index, name in enumerate(
                ("pre_seed", "seed", "series_a", "series_b", "series_c", "series_d", "series_e", "public")
            )
        }
        observed_stages = [
            str(value).casefold().replace(" ", "_").replace("-", "_") for value in values(relevant, "company_stage")
        ]
        for code, bound, bad in (() if archetype_resolved else (
            ("funding_stage_min", spec.company_filters.funding_stage_min, lambda value, limit: value < limit),
            ("funding_stage_max", spec.company_filters.funding_stage_max, lambda value, limit: value > limit),
        )):
            if bound:
                normalized = bound.casefold().replace(" ", "_").replace("-", "_")
                if (
                    not observed_stages
                    or normalized not in stage_order
                    or any(value not in stage_order for value in observed_stages)
                ):
                    unknowns.append(f"{code}_unknown")
                elif all(bad(stage_order[value], stage_order[normalized]) for value in observed_stages):
                    violations.append(f"{code}_mismatch")
    if spec.tech_skills:
        observed = profile.get("tech_skills")
        if not observed:
            unknowns.append("tech_skills_unknown")
        elif {str(value).casefold() for value in observed}.isdisjoint(value.casefold() for value in spec.tech_skills):
            violations.append("tech_skills_mismatch")
    return {"violations": tuple(dict.fromkeys(violations)), "unknowns": tuple(dict.fromkeys(unknowns))}


def validation_findings(
    profile: Mapping[str, Any] | None,
    spec: SearchSpec,
    sources: ResolvedSources,
    source_lanes: tuple[str, ...] = (),
    structured: Mapping[str, Any] | None = None,
) -> dict[str, tuple[str, ...]]:
    if spec.role.search_mode != "COMPANY_UNION":
        return _validation_findings_for_branch(profile, spec, sources, None, structured)
    branch_list: list[str] = []
    for lane in source_lanes:
        if lane == "company_union":
            branch_list.append("company_union")
        elif lane in {"role", "summary", "adjacency", "company_signal"}:
            branch_list.append("summary" if lane == "summary" else "role")
        elif lane == "sql":
            branch_list.extend(("role", "company_union"))
    branches = tuple(dict.fromkeys(branch_list))
    if not branches:
        branches = ("role",)
    findings = [
        _validation_findings_for_branch(profile, spec, sources, branch, structured)
        for branch in branches
    ]
    if any(not finding["violations"] and not finding["unknowns"] for finding in findings):
        return {"violations": (), "unknowns": ()}
    return {
        "violations": tuple(dict.fromkeys(code for finding in findings for code in finding["violations"])),
        "unknowns": tuple(dict.fromkeys(code for finding in findings for code in finding["unknowns"])),
    }


def hard_filter_validation_artifact(
    candidates: tuple[Any, ...],
    spec: SearchSpec,
    *,
    case_id: str = "production",
    case_hash: str | None = None,
    corpus_snapshot_hash: str | None = None,
) -> dict[str, Any]:
    violations = []
    for row in candidates:
        reasons = (*row.hard_filter_evidence.get("violations", ()), *row.hard_filter_evidence.get("unknowns", ()))
        if reasons:
            violations.append({"person_id": row.person_id, "reason_code": "|".join(reasons)})
    return {
        "schema_version": "reflect.hard_filter_validation.v1",
        "case_id": case_id,
        "case_hash": case_hash or canonical_hash(spec.to_dict()),
        "corpus_snapshot_hash": corpus_snapshot_hash or canonical_hash(spec.corpus.to_dict()),
        "reviewed_count": len(candidates),
        "violation_count": len(violations),
        "violations": violations,
        "producer": "typed_runner",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
