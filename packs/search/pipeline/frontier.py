"""Canonical person-grain frontier and provenance-preserving merge."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping


def _union(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


@dataclass(frozen=True)
class ProbeMatch:
    lane: str
    rank: int
    probe_id: str | None = None
    probe_family: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class CandidateRecord:
    person_id: str
    retrieval_score: float = 0.0
    rank_components: Mapping[str, float] = field(default_factory=dict)
    matched_position_ids: tuple[str, ...] = ()
    matched_position_indexes: tuple[int, ...] = ()
    source_lanes: tuple[str, ...] = ()
    found_by: tuple[ProbeMatch, ...] = ()
    backend: str = ""
    hard_filter_evidence: Mapping[str, Any] = field(default_factory=dict)
    structured: Mapping[str, Any] = field(default_factory=dict)
    tech_skills: tuple[str, ...] = ()
    hydrated_profile: Mapping[str, Any] | None = None
    hydration_disposition: str = "pending"
    deterministic_score: float = 0.0
    semantic_score: float | None = None
    triage: Mapping[str, Any] | None = None
    judge: Mapping[str, Any] | None = None
    deterministic_gates: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["found_by"] = [asdict(item) for item in self.found_by]
        for key in ("matched_position_ids", "matched_position_indexes", "source_lanes", "tech_skills"):
            value[key] = list(value[key])
        return value

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateRecord":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown CandidateRecord fields: {', '.join(sorted(unknown))}")
        return cls(
            person_id=str(data["person_id"]),
            retrieval_score=float(data.get("retrieval_score", 0)),
            rank_components=dict(data.get("rank_components") or {}),
            matched_position_ids=tuple(data.get("matched_position_ids") or ()),
            matched_position_indexes=tuple(int(v) for v in (data.get("matched_position_indexes") or ())),
            source_lanes=tuple(data.get("source_lanes") or ()),
            found_by=tuple(ProbeMatch(**row) for row in (data.get("found_by") or ())),
            backend=str(data.get("backend") or ""),
            hard_filter_evidence=dict(data.get("hard_filter_evidence") or {}),
            structured=dict(data.get("structured") or {}),
            tech_skills=tuple(data.get("tech_skills") or ()),
            hydrated_profile=data.get("hydrated_profile"),
            hydration_disposition=str(data.get("hydration_disposition") or "pending"),
            deterministic_score=float(data.get("deterministic_score", 0)),
            semantic_score=data.get("semantic_score"),
            triage=dict(data["triage"]) if data.get("triage") is not None else None,
            judge=dict(data["judge"]) if data.get("judge") is not None else None,
            deterministic_gates=dict(data.get("deterministic_gates") or {}),
        )


def merge_candidate(left: CandidateRecord, right: CandidateRecord) -> CandidateRecord:
    if left.person_id != right.person_id:
        raise ValueError("cannot merge different people")
    found = tuple(dict.fromkeys((*left.found_by, *right.found_by)))
    return replace(
        left,
        retrieval_score=max(left.retrieval_score, right.retrieval_score),
        rank_components={**left.rank_components, **right.rank_components},
        matched_position_ids=_union(left.matched_position_ids, right.matched_position_ids),
        matched_position_indexes=tuple(
            dict.fromkeys((*left.matched_position_indexes, *right.matched_position_indexes))
        ),
        source_lanes=_union(left.source_lanes, right.source_lanes),
        found_by=found,
        hard_filter_evidence={**left.hard_filter_evidence, **right.hard_filter_evidence},
        structured={**left.structured, **right.structured},
        tech_skills=_union(left.tech_skills, right.tech_skills),
        hydrated_profile=right.hydrated_profile or left.hydrated_profile,
        hydration_disposition=right.hydration_disposition
        if right.hydration_disposition != "pending"
        else left.hydration_disposition,
        semantic_score=right.semantic_score if right.semantic_score is not None else left.semantic_score,
        triage=right.triage if right.triage is not None else left.triage,
        judge=right.judge if right.judge is not None else left.judge,
        deterministic_gates={**left.deterministic_gates, **right.deterministic_gates},
    )


def lane_yield_counts(records: tuple[CandidateRecord, ...] | list[CandidateRecord]) -> dict[str, int]:
    """Report unique and exclusive person yield for every retrieval lane."""
    person_lanes: dict[str, set[str]] = {}
    for record in records:
        person_lanes.setdefault(record.person_id, set()).update(record.source_lanes)
    lanes = sorted({lane for values in person_lanes.values() for lane in values})
    counts: dict[str, int] = {}
    for lane in lanes:
        counts[f"lane_{lane}_people"] = sum(lane in values for values in person_lanes.values())
        counts[f"lane_{lane}_marginal"] = sum(values == {lane} for values in person_lanes.values())
    return counts


@dataclass(frozen=True)
class CandidateFrontier:
    candidates: tuple[CandidateRecord, ...]
    input_count: int
    output_count: int
    limit: int | None
    truncated: bool

    @classmethod
    def merge(
        cls, records: tuple[CandidateRecord, ...] | list[CandidateRecord], limit: int | None = None
    ) -> "CandidateFrontier":
        by_id: dict[str, CandidateRecord] = {}
        order: list[str] = []
        for record in records:
            if record.person_id not in by_id:
                order.append(record.person_id)
                by_id[record.person_id] = record
            else:
                by_id[record.person_id] = merge_candidate(by_id[record.person_id], record)
        merged = [by_id[key] for key in order]
        truncated = bool(limit and len(merged) > limit)
        if limit:
            merged = merged[:limit]
        return cls(tuple(merged), len(records), len(merged), limit, truncated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "candidate.frontier.v1",
            "candidates": [row.to_dict() for row in self.candidates],
            "input_count": self.input_count,
            "output_count": self.output_count,
            "limit": self.limit,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    frontier: CandidateFrontier
    counts: Mapping[str, int] = field(default_factory=dict)
    reason_histogram: Mapping[str, int] = field(default_factory=dict)
    capability_report: Mapping[str, Any] = field(default_factory=dict)
    resolved_sources: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    artifact_paths: Mapping[str, str] = field(default_factory=dict)
    hard_filter_validation: Mapping[str, Any] = field(default_factory=dict)
    corpus_observation: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "search.stage_result.v1",
            "stage": self.stage,
            "status": self.status,
            "frontier": self.frontier.to_dict(),
            "counts": dict(self.counts),
            "reason_histogram": dict(self.reason_histogram),
            "capability_report": dict(self.capability_report),
            "resolved_sources": list(self.resolved_sources),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "artifact_paths": dict(self.artifact_paths),
            "hard_filter_validation": dict(self.hard_filter_validation),
            "corpus_observation": dict(self.corpus_observation),
        }
