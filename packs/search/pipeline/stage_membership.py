"""Typed per-person stage membership emitted once by recruiting."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .frontier import CandidateRecord, ProbeMatch
from .models import SearchBounds


STAGE_MEMBERSHIP_NAME = "stage-membership.json"
SCHEMA_VERSION = "search.stage_membership.v1"
DISPOSITIONS = {
    "hydration_missing",
    "hard_filter_quarantined",
    "triage_dropped",
    "never_judged",
    "shortlisted",
    "gate_passed_not_shortlisted",
    "seniority_gated",
    "below_floor",
    "core_gated",
    "location_gated",
    "founder_c_suite_gated",
    "judge_out",
}
JUDGE_STATUSES = {"not_run", "judged", "error"}
GATE_DISPOSITIONS = {
    "gate_passed_not_shortlisted",
    "seniority_gated",
    "below_floor",
    "core_gated",
    "location_gated",
    "founder_c_suite_gated",
    "judge_out",
}


@dataclass(frozen=True)
class StageMembershipRecord:
    person_id: str
    name: str | None
    found_by: tuple[ProbeMatch, ...]
    hydrated: bool
    hard_filter_passed: bool
    triage_survived: bool
    judge_status: str
    shortlisted: bool
    disposition: str
    detail: str

    def __post_init__(self) -> None:
        if not self.person_id:
            raise ValueError("stage membership person_id is required")
        if len(self.found_by) != len(set(self.found_by)):
            raise ValueError("stage membership found_by contains duplicates")
        if self.disposition not in DISPOSITIONS:
            raise ValueError(f"unsupported stage disposition: {self.disposition}")
        if self.judge_status not in JUDGE_STATUSES:
            raise ValueError(f"unsupported judge status: {self.judge_status}")
        if self.hard_filter_passed and not self.hydrated:
            raise ValueError("hard-filter-passed membership must be hydrated")
        if self.shortlisted and self.judge_status != "judged":
            raise ValueError("shortlisted stage membership must be judged")
        if self.triage_survived and not self.hard_filter_passed:
            raise ValueError("triage survivor must pass hard filters")
        if self.judge_status == "judged" and not self.triage_survived:
            raise ValueError("judged stage membership must survive triage")
        if self.shortlisted != (self.disposition == "shortlisted"):
            raise ValueError("shortlisted membership and disposition must match")
        if not self.hydrated:
            expected = "hydration_missing"
        elif not self.hard_filter_passed:
            expected = "hard_filter_quarantined"
        elif not self.triage_survived:
            expected = "triage_dropped"
        elif self.judge_status != "judged":
            expected = "never_judged"
        elif self.shortlisted:
            expected = "shortlisted"
        else:
            expected = None
        if expected is not None and self.disposition != expected:
            raise ValueError(f"stage membership disposition must be {expected}")
        if expected is None and self.disposition not in GATE_DISPOSITIONS:
            raise ValueError("judged non-shortlisted membership must have a gate disposition")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StageMembershipRecord":
        expected = set(cls.__dataclass_fields__)
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"invalid stage membership fields: missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        for field in ("person_id", "judge_status", "disposition", "detail"):
            if not isinstance(value[field], str):
                raise ValueError(f"stage membership {field} must be a string")
        for field in ("hydrated", "hard_filter_passed", "triage_survived", "shortlisted"):
            if not isinstance(value[field], bool):
                raise ValueError(f"stage membership {field} must be boolean")
        if value["name"] is not None and not isinstance(value["name"], str):
            raise ValueError("stage membership name must be a string or null")
        if not isinstance(value["found_by"], list):
            raise ValueError("stage membership found_by must be an array")
        probe_fields = set(ProbeMatch.__dataclass_fields__)
        for index, item in enumerate(value["found_by"]):
            if not isinstance(item, Mapping) or set(item) != probe_fields:
                raise ValueError(f"stage membership found_by[{index}] must contain exact ProbeMatch fields")
        return cls(
            person_id=value["person_id"],
            name=value["name"],
            found_by=tuple(ProbeMatch.from_dict(item) for item in value["found_by"]),
            hydrated=value["hydrated"],
            hard_filter_passed=value["hard_filter_passed"],
            triage_survived=value["triage_survived"],
            judge_status=str(value["judge_status"]),
            shortlisted=value["shortlisted"],
            disposition=str(value["disposition"]),
            detail=str(value["detail"]),
        )


@dataclass(frozen=True)
class SearchStageMembership:
    schema_version: str
    status: str
    epochs: int
    total_sourced: int
    score_floor: float
    sendable_score: float
    frontier_limit: int
    candidates: tuple[StageMembershipRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported search stage membership: {self.schema_version}")
        candidate_ids = tuple(row.person_id for row in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("search stage membership contains duplicate candidates")
        if self.total_sourced != len(candidate_ids):
            raise ValueError("total_sourced must equal candidate membership count")
        if not self.status:
            raise ValueError("stage membership status is required")
        if isinstance(self.epochs, bool) or not isinstance(self.epochs, int) or self.epochs < 0:
            raise ValueError("stage membership epochs must be a non-negative integer")
        for name in ("score_floor", "sendable_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"stage membership {name} must be between zero and one")
        if isinstance(self.frontier_limit, bool) or not isinstance(self.frontier_limit, int) or self.frontier_limit < 1:
            raise ValueError("stage membership frontier_limit must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidates"] = [
            asdict(row) | {"found_by": [asdict(match) for match in row.found_by]}
            for row in self.candidates
        ]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SearchStageMembership":
        expected = set(cls.__dataclass_fields__)
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"invalid search stage membership fields: missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        if not isinstance(value["status"], str):
            raise ValueError("stage membership status must be a string")
        if isinstance(value["epochs"], bool) or not isinstance(value["epochs"], int):
            raise ValueError("stage membership epochs must be an integer")
        if isinstance(value["total_sourced"], bool) or not isinstance(value["total_sourced"], int):
            raise ValueError("stage membership total_sourced must be an integer")
        if not isinstance(value["candidates"], list):
            raise ValueError("stage membership candidates must be an array")
        return cls(
            schema_version=str(value["schema_version"]),
            status=str(value["status"]),
            epochs=value["epochs"],
            total_sourced=value["total_sourced"],
            score_floor=value["score_floor"],
            sendable_score=value["sendable_score"],
            frontier_limit=value["frontier_limit"],
            candidates=tuple(StageMembershipRecord.from_dict(row) for row in value["candidates"]),
        )

    @classmethod
    def read(cls, path: Path) -> "SearchStageMembership":
        from packs.search.primitives.validate_artifact.validate_artifact import validate_file

        return cls.from_dict(validate_file("stage-membership", path))


def gate_disposition(
    candidate: CandidateRecord,
    *,
    score_floor: float,
    sendable_score: float,
) -> tuple[str, str]:
    gates = candidate.deterministic_gates
    judge = candidate.judge or {}
    judge_status = str(judge.get("status") or "not_run")
    if judge_status != "judged":
        return "never_judged", f"judge_status={judge_status}"
    score = float(judge.get("score") if judge.get("score") is not None else candidate.deterministic_score)
    expected_keys = {
        "location", "core_groups", "seniority_track", "founder_c_suite_hireable",
        "categorical_not_out", "score_floor", "shortlist", "sendable",
    }
    if set(gates) != expected_keys or any(not isinstance(gates[key], bool) for key in expected_keys):
        raise ValueError("ranked candidate deterministic gates must contain exact boolean fields")
    if abs(candidate.deterministic_score - score) > 1e-12:
        raise ValueError("ranked candidate deterministic score does not match judge score")
    expected_floor = score >= score_floor
    if gates["score_floor"] != expected_floor:
        raise ValueError("ranked candidate score_floor gate contradicts SearchSpec bounds")
    prerequisite_gates = (
        "location", "core_groups", "seniority_track", "founder_c_suite_hireable",
        "categorical_not_out", "score_floor",
    )
    expected_shortlist = all(gates[key] for key in prerequisite_gates)
    if gates["shortlist"] != expected_shortlist:
        raise ValueError("ranked candidate shortlist gate contradicts prerequisite gates")
    known_seniority = judge.get("seniority_fit") in {"ideal", "acceptable", "in_band"}
    expected_sendable = expected_shortlist and known_seniority and score >= sendable_score
    if gates["sendable"] != expected_sendable:
        raise ValueError("ranked candidate sendable gate contradicts prerequisites or threshold")
    detail = f"score={score:.2f} gates={json.dumps(dict(gates), sort_keys=True)}"
    if not gates.get("seniority_track"):
        return "seniority_gated", detail
    if not gates.get("score_floor"):
        return "below_floor", detail
    if not gates.get("core_groups"):
        return "core_gated", detail
    if not gates.get("location"):
        return "location_gated", detail
    if not gates.get("founder_c_suite_hireable"):
        return "founder_c_suite_gated", detail
    if not gates.get("categorical_not_out"):
        return "judge_out", detail
    return "gate_passed_not_shortlisted", detail


def build_stage_membership(
    *,
    sourced: list[CandidateRecord],
    hydrated: list[CandidateRecord],
    triaged: list[CandidateRecord],
    ranked: tuple[CandidateRecord, ...],
    shortlist_person_ids: set[str],
    status: str,
    epochs: int,
    bounds: SearchBounds,
) -> SearchStageMembership:
    hydrated_by_id = {row.person_id: row for row in hydrated}
    triaged_ids = {row.person_id for row in triaged}
    ranked_by_id = {row.person_id: row for row in ranked}
    rows = []
    for source in sourced:
        reviewed = hydrated_by_id.get(source.person_id)
        hydrated = bool(reviewed and reviewed.hydration_disposition == "hydrated")
        hard_filter_passed = bool(hydrated and reviewed.hard_filter_evidence.get("disposition") == "accepted")
        final = ranked_by_id.get(source.person_id)
        judge_status = str((final.judge or {}).get("status") or "not_run") if final else "not_run"
        shortlisted = source.person_id in shortlist_person_ids
        gate_result = None
        gate_detail = ""
        if final is not None and judge_status == "judged":
            gate_result, gate_detail = gate_disposition(
                final,
                score_floor=bounds.score_floor,
                sendable_score=bounds.sendable_score,
            )
            if shortlisted and not final.deterministic_gates["shortlist"]:
                raise ValueError("presented shortlist candidate is not deterministically eligible")
        if not hydrated:
            disposition, detail = "hydration_missing", "sourced candidate did not produce a hydrated profile"
        elif not hard_filter_passed:
            reasons = (*reviewed.hard_filter_evidence.get("violations", ()), *reviewed.hard_filter_evidence.get("unknowns", ()))
            disposition, detail = "hard_filter_quarantined", "|".join(reasons)
        elif source.person_id not in triaged_ids:
            disposition, detail = "triage_dropped", "candidate did not survive deterministic triage"
        elif judge_status != "judged":
            disposition, detail = "never_judged", f"judge_status={judge_status}"
        elif shortlisted:
            disposition, detail = "shortlisted", ""
        else:
            assert gate_result is not None
            disposition, detail = gate_result, gate_detail
        profile = (reviewed or final).hydrated_profile if reviewed or final else None
        rows.append(
            StageMembershipRecord(
                person_id=source.person_id,
                name=str((profile or {}).get("name")) if (profile or {}).get("name") else None,
                found_by=source.found_by,
                hydrated=hydrated,
                hard_filter_passed=hard_filter_passed,
                triage_survived=source.person_id in triaged_ids,
                judge_status=judge_status,
                shortlisted=shortlisted,
                disposition=disposition,
                detail=detail,
            )
        )
    return SearchStageMembership(
        schema_version=SCHEMA_VERSION,
        status=status,
        epochs=epochs,
        total_sourced=len(rows),
        score_floor=bounds.score_floor,
        sendable_score=bounds.sendable_score,
        frontier_limit=bounds.frontier_limit,
        candidates=tuple(rows),
    )
