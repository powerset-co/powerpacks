"""Typed lookup/GTM composition over one explicitly selected concrete runner."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from .filters import hard_filter_validation_artifact, unsupported_hard_filters, validation_findings
from .frontier import CandidateFrontier, CandidateRecord, ProbeMatch, StageResult, lane_yield_counts
from .models import Profile, RankMode, SearchPlan, SearchSpec
from .ranking import SemanticAdapter, deterministic_rank, production_semantic_adapter, semantic_rank


EMPTY = CandidateFrontier((), 0, 0, None, False)


def _capability_dict(capabilities: Any) -> dict[str, Any]:
    value = asdict(capabilities)
    value["backend"] = capabilities.backend.value
    for key, item in value.items():
        if isinstance(item, tuple):
            value[key] = list(item)
    return value


def run_with_runner(
    spec: SearchSpec,
    runner: Any,
    *,
    semantic_adapter: SemanticAdapter | None = None,
    artifact_root: str | None = None,
) -> StageResult:
    capabilities = runner.capabilities(spec)
    if capabilities.backend != spec.backend:
        raise ValueError("selected runner backend does not match SearchSpec")
    unsupported = unsupported_hard_filters(spec, capabilities.supported_hard_filters)
    if unsupported:
        return StageResult(
            "capabilities",
            "unsupported_capability",
            EMPTY,
            capability_report=_capability_dict(capabilities),
            errors=(f"unsupported required hard filters: {', '.join(unsupported)}",),
        )
    if spec.rank_mode == RankMode.SEMANTIC and not spec.rank_approved:
        return StageResult(
            "rank",
            "needs_input",
            EMPTY,
            capability_report=_capability_dict(capabilities),
            errors=("semantic ranking requires rank_approved=true",),
        )
    if spec.profile == Profile.RECRUITING:
        from .recruiting import run_recruiting

        return run_recruiting(spec, runner, artifact_root=artifact_root)
    if spec.profile == Profile.LOOKUP:
        if spec.lookup and spec.lookup.field not in capabilities.lookup_fields:
            return StageResult(
                "lookup",
                "unsupported_capability",
                EMPTY,
                capability_report=_capability_dict(capabilities),
                errors=(f"unsupported lookup field: {spec.lookup.field}",),
            )
        records = tuple(runner.lookup_person(spec.lookup))
        frontier = CandidateFrontier.merge(records, spec.bounds.output_limit)
        hydrated = runner.hydrate(frontier)
        return StageResult(
            "lookup",
            "completed" if hydrated.candidates else "completed_empty",
            hydrated,
            counts={"lookup_matches": len(hydrated.candidates)},
            capability_report=_capability_dict(capabilities),
        )

    sources = runner.resolve_sources(spec)
    if sources.unresolved_required_inputs:
        return StageResult(
            "resolve_sources",
            "needs_input",
            EMPTY,
            resolved_sources=sources.records,
            errors=(
                "required explicit sources could not be resolved: " + ", ".join(sources.unresolved_required_inputs),
            ),
        )
    filters = runner.apply_hard_filters(spec, sources)
    plan = SearchPlan(
        spec, capabilities, sources, ("hard_filter", "retrieve", "sql_fan_in", "hydrate", "validate", "rank")
    )
    if filters.eligible_count == 0:
        return StageResult(
            "hard_filter",
            "completed_empty",
            EMPTY,
            counts={"eligible_pool": 0},
            capability_report=_capability_dict(capabilities),
            resolved_sources=sources.records,
        )
    retrieved = list(runner.retrieve_people(plan, filters))
    eligible_ids = set(filters.eligible_person_ids)
    for rank, sql in enumerate(spec.sql_candidates, start=1):
        if sql.person_id not in eligible_ids:
            continue
        retrieved.append(
            CandidateRecord(
                sql.person_id,
                source_lanes=("sql",),
                found_by=(ProbeMatch("sql", rank, probe_family="sql_fan_in"),),
                backend=spec.backend.value,
                hard_filter_evidence={"sql_evidence": sql.evidence} if sql.evidence else {},
            )
        )
    frontier = CandidateFrontier.merge(retrieved)
    if not frontier.candidates:
        return StageResult("retrieve", "completed_empty", frontier, counts={"eligible_pool": filters.eligible_count})
    hydrated = runner.hydrate(frontier)
    accepted: list[CandidateRecord] = []
    reviewed: list[CandidateRecord] = []
    violations = unknowns = 0
    for row in hydrated.candidates:
        findings = validation_findings(
            row.hydrated_profile, spec, sources, row.source_lanes, row.structured
        )
        violations += len(findings["violations"])
        unknowns += len(findings["unknowns"])
        disposition = "accepted" if not findings["violations"] and not findings["unknowns"] else "quarantined"
        evidence = {
            **row.hard_filter_evidence,
            **findings,
            "validated": disposition == "accepted",
            "disposition": disposition,
        }
        validated = replace(row, hard_filter_evidence=evidence)
        reviewed.append(validated)
        if disposition == "accepted":
            accepted.append(validated)
    if spec.rank_mode == RankMode.SEMANTIC:
        ranked = deterministic_rank(
            CandidateFrontier.merge(accepted),
            spec,
            limit=spec.bounds.semantic_rank_limit,
        )
        ranked, semantic_errors = semantic_rank(
            ranked, spec, semantic_adapter or production_semantic_adapter
        )
    else:
        ranked = deterministic_rank(CandidateFrontier.merge(accepted), spec)
        semantic_errors = ()
    if semantic_errors and all(candidate.semantic_score is None for candidate in ranked.candidates):
        return StageResult(
            "rank",
            "failed_rank",
            ranked,
            counts={"semantic_rank_failures": len(semantic_errors)},
            capability_report=_capability_dict(capabilities),
            resolved_sources=sources.records,
            errors=semantic_errors,
            hard_filter_validation=hard_filter_validation_artifact(tuple(reviewed), spec),
        )
    status = "completed" if ranked.candidates else "completed_empty"
    return StageResult(
        "gtm",
        status,
        ranked,
        counts={
            "eligible_pool": filters.eligible_count,
            "retrieved": len(retrieved),
            "hydrated": sum(row.hydration_disposition == "hydrated" for row in hydrated.candidates),
            "hard_filter_violations": 0,
            "quarantined_violations": violations,
            "quarantined_unknowns": unknowns,
            "semantic_rank_failures": len(semantic_errors),
            **lane_yield_counts(retrieved),
        },
        capability_report=_capability_dict(capabilities),
        resolved_sources=sources.records,
        warnings=(
            *((f"validation_reviewed:{len(reviewed)}",) if reviewed else ()),
            *semantic_errors,
        ),
        hard_filter_validation=hard_filter_validation_artifact(tuple(reviewed), spec),
    )
