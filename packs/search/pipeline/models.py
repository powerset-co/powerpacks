"""Strict immutable contracts for layered search."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

HASH_RE = re.compile(r"^[a-f0-9]{64}$")

# Reflect review pools are human-reviewed and snapshot in one bounded request.
# Five hundred matches the canonical frontier ceiling without permitting an
# accidentally unbounded hydration; 256 characters covers opaque UUID/URN IDs.
REVIEW_POOL_MAX_PERSON_IDS = 500
REVIEW_POOL_PERSON_ID_MAX_LENGTH = 256

# The recruiting judge is the stage that decides which candidates survive, so it runs
# on the cheap fast Luna model at no reasoning by default and is iterated on, not on a
# premium reasoning model nobody can afford to re-run. Both values stay explicitly
# overridable in the spec; `judge_approved` remains the spend gate either way.
DEFAULT_JUDGE_MODEL = "gpt-5.6-luna"
DEFAULT_JUDGE_REASONING_EFFORT = "none"
# Verified live against gpt-5.6-luna on 2026-08-06: "none", "low", "medium", and "high"
# are all accepted, and "minimal" is the one rejected value for this model family --
# the provider returns HTTP 400 ("Unsupported value: 'reasoning_effort' does not
# support 'minimal'"), so it must never leave this boundary.
JUDGE_REASONING_EFFORTS = ("none", "low", "medium", "high")


def _hash(value: str | None, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hash")
    return value


def _bool(value: Any, name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean or null")
    return value


def _number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric or null")
    return float(value)


class Profile(StrEnum):
    LOOKUP = "lookup"
    GTM = "gtm"
    RECRUITING = "recruiting"


class Backend(StrEnum):
    LOCAL = "local"
    POWERSET = "powerset"


class RankMode(StrEnum):
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"


def _strict(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {name} fields: {', '.join(sorted(unknown))}")


def _tuple(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
        raise ValueError("expected a list of strings")
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass(frozen=True)
class LocalCorpus:
    db_path: str
    content_hash: str | None = None
    schema_hash: str | None = None
    membership_hash: str | None = None
    native_content_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ValueError("local db_path is required")
        for name in ("content_hash", "schema_hash", "membership_hash"):
            _hash(getattr(self, name), name, optional=True)
        if self.native_content_version is not None and (
            not isinstance(self.native_content_version, str) or not self.native_content_version.strip()
        ):
            raise ValueError("native_content_version must be a non-empty string or null")
        if self.native_content_version and self.content_hash:
            raise ValueError("at most one local content identity may be supplied")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LocalCorpus":
        _strict(
            data,
            {"kind", "db_path", "content_hash", "schema_hash", "membership_hash", "native_content_version"},
            "LocalCorpus",
        )
        if data.get("kind") != "local":
            raise ValueError("local corpus kind must be local")
        return cls(
            data.get("db_path"),
            data.get("content_hash"),
            data.get("schema_hash"),
            data.get("membership_hash"),
            data.get("native_content_version"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "local", **asdict(self)}


@dataclass(frozen=True)
class PowersetCorpus:
    set_id: str
    operator_ids: tuple[str, ...]
    operator_scope_hash: str | None = None
    membership_hash: str | None = None
    namespace_schema_hashes: Mapping[str, str] = field(default_factory=dict)
    native_content_version: str | None = None
    scoped_records_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.set_id, str) or not self.set_id.strip() or not self.operator_ids:
            raise ValueError("Powerset set_id and operator_ids are required")
        object.__setattr__(self, "operator_ids", _tuple(self.operator_ids))
        if not self.operator_ids:
            raise ValueError("Powerset set_id and operator_ids are required")
        frozen = MappingProxyType(
            dict(sorted((str(key), str(value)) for key, value in self.namespace_schema_hashes.items()))
        )
        object.__setattr__(self, "namespace_schema_hashes", frozen)
        _hash(self.operator_scope_hash, "operator_scope_hash", optional=True)
        _hash(self.membership_hash, "membership_hash", optional=True)
        for key, value in frozen.items():
            _hash(value, f"namespace_schema_hashes.{key}")
        _hash(self.scoped_records_hash, "scoped_records_hash", optional=True)
        if self.native_content_version is not None and (
            not isinstance(self.native_content_version, str) or not self.native_content_version.strip()
        ):
            raise ValueError("native_content_version must be a non-empty string or null")
        if self.native_content_version and self.scoped_records_hash:
            raise ValueError("at most one Powerset content identity may be supplied")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PowersetCorpus":
        fields = {
            "kind",
            "set_id",
            "operator_ids",
            "operator_scope_hash",
            "membership_hash",
            "namespace_schema_hashes",
            "native_content_version",
            "scoped_records_hash",
        }
        _strict(data, fields, "PowersetCorpus")
        if data.get("kind") != "powerset":
            raise ValueError("powerset corpus kind must be powerset")
        schemas = data.get("namespace_schema_hashes")
        if schemas is None:
            schemas = {}
        if not isinstance(schemas, dict):
            raise ValueError("namespace_schema_hashes must be an object")
        native, scoped = data.get("native_content_version"), data.get("scoped_records_hash")
        return cls(
            data.get("set_id"),
            _tuple(data.get("operator_ids")),
            data.get("operator_scope_hash"),
            data.get("membership_hash"),
            dict(sorted((str(k), str(v)) for k, v in schemas.items())),
            native,
            scoped,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "powerset",
            "set_id": self.set_id,
            "operator_ids": list(self.operator_ids),
            "operator_scope_hash": self.operator_scope_hash,
            "membership_hash": self.membership_hash,
            "namespace_schema_hashes": dict(self.namespace_schema_hashes),
            "native_content_version": self.native_content_version,
            "scoped_records_hash": self.scoped_records_hash,
        }


@dataclass(frozen=True)
class LookupSpec:
    field: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or self.field not in {
            "name",
            "email",
            "phone",
            "handle",
            "profile_url",
            "person_id",
        }:
            raise ValueError(f"unsupported lookup field: {self.field}")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("lookup value is required")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LookupSpec":
        _strict(data, {"field", "value"}, "LookupSpec")
        field_name = data.get("field")
        if field_name not in {"name", "email", "phone", "handle", "profile_url", "person_id"}:
            raise ValueError(f"unsupported lookup field: {field_name}")
        if not isinstance(data.get("value"), str) or not data["value"].strip():
            raise ValueError("lookup value is required")
        return cls(field_name, data["value"].strip())


@dataclass(frozen=True)
class RoleIntent:
    role_ids: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    bm25_queries: tuple[str, ...] = ()
    search_mode: str = "SEARCH_ONLY"

    def __post_init__(self) -> None:
        for name in ("role_ids", "titles", "bm25_queries"):
            object.__setattr__(self, name, _tuple(getattr(self, name)))
        if self.search_mode not in {"SEARCH_ONLY", "COMPANY_INTERSECTION", "COMPANY_UNION"}:
            raise ValueError("invalid role search_mode")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoleIntent":
        _strict(data, {"role_ids", "titles", "bm25_queries", "search_mode"}, "RoleIntent")
        return cls(
            _tuple(data.get("role_ids")),
            _tuple(data.get("titles")),
            _tuple(data.get("bm25_queries")),
            data.get("search_mode") or "SEARCH_ONLY",
        )


@dataclass(frozen=True)
class PersonFilters:
    cities: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    metro_areas: tuple[str, ...] = ()
    seniority_bands: tuple[str, ...] = ()
    role_tracks: tuple[str, ...] = ()
    education_ids: tuple[str, ...] = ()
    education_names: tuple[str, ...] = ()
    is_current_role: bool | None = None
    years_experience_min: float | None = None
    years_experience_max: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "cities",
            "states",
            "countries",
            "metro_areas",
            "seniority_bands",
            "role_tracks",
            "education_ids",
            "education_names",
        ):
            object.__setattr__(self, name, _tuple(getattr(self, name)))
        object.__setattr__(self, "is_current_role", _bool(self.is_current_role, "is_current_role"))
        object.__setattr__(self, "years_experience_min", _number(self.years_experience_min, "years_experience_min"))
        object.__setattr__(self, "years_experience_max", _number(self.years_experience_max, "years_experience_max"))
        if (
            self.years_experience_min is not None
            and self.years_experience_max is not None
            and self.years_experience_min > self.years_experience_max
        ):
            raise ValueError("years_experience_min cannot exceed maximum")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PersonFilters":
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        _strict(data, fields, "PersonFilters")
        kwargs = {
            key: _tuple(data.get(key))
            for key in fields
            if key not in {"is_current_role", "years_experience_min", "years_experience_max"}
        }
        kwargs.update(
            is_current_role=data.get("is_current_role"),
            years_experience_min=data.get("years_experience_min"),
            years_experience_max=data.get("years_experience_max"),
        )
        return cls(**kwargs)


@dataclass(frozen=True)
class CompanyFilters:
    company_ids: tuple[str, ...] = ()
    company_names: tuple[str, ...] = ()
    investor_names: tuple[str, ...] = ()
    sector_types: tuple[str, ...] = ()
    technology_types: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()
    funding_stage_min: str | None = None
    funding_stage_max: str | None = None
    headcount_min: int | None = None
    headcount_max: int | None = None
    is_current_company: bool | None = None

    def __post_init__(self) -> None:
        for name in (
            "company_ids",
            "company_names",
            "investor_names",
            "sector_types",
            "technology_types",
            "entity_types",
        ):
            object.__setattr__(self, name, _tuple(getattr(self, name)))
        object.__setattr__(self, "is_current_company", _bool(self.is_current_company, "is_current_company"))
        for name in ("funding_stage_min", "funding_stage_max"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or null")
        for name in ("headcount_min", "headcount_max"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if (
            self.headcount_min is not None
            and self.headcount_max is not None
            and self.headcount_min > self.headcount_max
        ):
            raise ValueError("headcount_min cannot exceed maximum")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompanyFilters":
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        _strict(data, fields, "CompanyFilters")
        tuple_fields = {
            "company_ids",
            "company_names",
            "investor_names",
            "sector_types",
            "technology_types",
            "entity_types",
        }
        return cls(**{key: (_tuple(data.get(key)) if key in tuple_fields else data.get(key)) for key in fields})


@dataclass(frozen=True)
class EvidenceCriterion:
    name: str
    description: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not isinstance(self.description, str)
            or not self.name.strip()
            or not self.description.strip()
        ):
            raise ValueError("evidence criterion name and description are required")
        weight = _number(self.weight, "weight")
        if weight != 1.0:
            raise ValueError("EvidenceCriterion weight must be 1.0")
        object.__setattr__(self, "weight", weight)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceCriterion":
        _strict(data, {"name", "description", "weight"}, "EvidenceCriterion")
        return cls(
            data.get("name"),
            data.get("description"),
            data.get("weight", 1.0),
        )


@dataclass(frozen=True)
class SqlCandidate:
    person_id: str
    evidence: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.person_id, str) or not self.person_id.strip():
            raise ValueError("SQL candidate person_id is required")
        if self.evidence is not None and not isinstance(self.evidence, str):
            raise ValueError("SQL candidate evidence must be a string or null")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SqlCandidate":
        _strict(data, {"person_id", "evidence"}, "SqlCandidate")
        return cls(data.get("person_id"), data.get("evidence"))


@dataclass(frozen=True)
class SearchBounds:
    retrieval_limit: int = 100
    output_limit: int = 25
    semantic_rank_limit: int = 50
    max_concurrent_probes: int = 5
    per_probe_limit: int = 50
    frontier_limit: int = 500
    triage_threshold: int = 100
    triage_score_threshold: float = 0.2
    judge_candidate_limit: int = 100
    judge_call_limit: int = 200
    exemplar_limit: int = 20
    expansion_thread_limit: int = 6
    epoch_limit: int = 2
    sourced_candidate_limit: int = 1000
    spend_limit_usd: float | None = None
    score_floor: float = 0.4
    sendable_score: float = 0.55

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (
                self.retrieval_limit,
                self.output_limit,
                self.semantic_rank_limit,
                self.max_concurrent_probes,
                self.per_probe_limit,
                self.frontier_limit,
                self.triage_threshold,
                self.judge_candidate_limit,
                self.judge_call_limit,
                self.exemplar_limit,
                self.expansion_thread_limit,
                self.epoch_limit,
                self.sourced_candidate_limit,
            )
        ):
            raise ValueError("search bounds must be integers")
        integer_values = tuple(
            value
            for name, value in asdict(self).items()
            if name not in {"spend_limit_usd", "triage_score_threshold", "score_floor", "sendable_score"}
        )
        if min(integer_values) < 1:
            raise ValueError("search bounds must be positive")
        if self.semantic_rank_limit > self.retrieval_limit:
            raise ValueError("semantic rank limit cannot exceed retrieval limit")
        if self.exemplar_limit < 10 or self.exemplar_limit > 20:
            raise ValueError("recruiting exemplar limit must be between 10 and 20")
        if self.expansion_thread_limit > 6:
            raise ValueError("recruiting expansion thread limit cannot exceed six")
        if self.spend_limit_usd is not None and (
            isinstance(self.spend_limit_usd, bool)
            or not isinstance(self.spend_limit_usd, (int, float))
            or self.spend_limit_usd <= 0
        ):
            raise ValueError("spend_limit_usd must be a positive number or null")
        for name in ("triage_score_threshold", "score_floor", "sendable_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchBounds":
        fields = set(cls.__dataclass_fields__)
        _strict(data, fields, "SearchBounds")
        return cls(**{name: data.get(name, field.default) for name, field in cls.__dataclass_fields__.items()})


@dataclass(frozen=True)
class RecruitingInput:
    source: str
    reviewed_plan_hash: str | None = None
    plan_model: str | None = None
    plan_approved: bool = False
    judge_implementation: str | None = None
    judge_model: str = DEFAULT_JUDGE_MODEL
    judge_reasoning_effort: str = DEFAULT_JUDGE_REASONING_EFFORT
    judge_approved: bool = False
    user_preferences: Mapping[str, Any] = field(default_factory=dict)
    review_pool_person_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("recruiting source is required")
        _hash(self.reviewed_plan_hash, "reviewed_plan_hash", optional=True)
        if self.plan_model is not None and (
            not isinstance(self.plan_model, str) or not self.plan_model.strip()
        ):
            raise ValueError("plan_model must be non-empty or null")
        if not isinstance(self.judge_model, str) or not self.judge_model.strip():
            raise ValueError("judge_model must be a non-empty string")
        if self.judge_reasoning_effort not in JUDGE_REASONING_EFFORTS:
            raise ValueError(
                "judge_reasoning_effort must be one of "
                f"{', '.join(JUDGE_REASONING_EFFORTS)}"
            )
        for name in ("plan_approved", "judge_approved"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if self.judge_implementation not in {None, "profile_evaluator", "codex"}:
            raise ValueError("judge_implementation must be profile_evaluator, codex, or null")
        object.__setattr__(self, "user_preferences", dict(self.user_preferences))
        review_pool = _tuple(self.review_pool_person_ids)
        if len(review_pool) > REVIEW_POOL_MAX_PERSON_IDS:
            raise ValueError(
                f"review_pool_person_ids cannot exceed {REVIEW_POOL_MAX_PERSON_IDS} IDs"
            )
        if any(len(person_id) > REVIEW_POOL_PERSON_ID_MAX_LENGTH for person_id in review_pool):
            raise ValueError(
                "review_pool_person_ids entries cannot exceed "
                f"{REVIEW_POOL_PERSON_ID_MAX_LENGTH} characters"
            )
        object.__setattr__(self, "review_pool_person_ids", review_pool)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecruitingInput":
        fields = set(cls.__dataclass_fields__)
        _strict(data, fields, "RecruitingInput")
        preferences = data.get("user_preferences") or {}
        if not isinstance(preferences, dict):
            raise ValueError("user_preferences must be an object")
        return cls(
            data.get("source"),
            data.get("reviewed_plan_hash"),
            data.get("plan_model"),
            data.get("plan_approved", False),
            data.get("judge_implementation"),
            data.get("judge_model") or DEFAULT_JUDGE_MODEL,
            data.get("judge_reasoning_effort") or DEFAULT_JUDGE_REASONING_EFFORT,
            data.get("judge_approved", False),
            preferences,
            _tuple(data.get("review_pool_person_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "source": self.source,
            "reviewed_plan_hash": self.reviewed_plan_hash,
            "plan_model": self.plan_model,
            "plan_approved": self.plan_approved,
            "judge_implementation": self.judge_implementation,
            "judge_model": self.judge_model,
            "judge_reasoning_effort": self.judge_reasoning_effort,
            "judge_approved": self.judge_approved,
            "user_preferences": dict(self.user_preferences),
        }
        if self.review_pool_person_ids:
            value["review_pool_person_ids"] = list(self.review_pool_person_ids)
        return value


@dataclass(frozen=True)
class SearchSpec:
    schema_version: str
    raw_request: str
    profile: Profile
    backend: Backend
    corpus: LocalCorpus | PowersetCorpus
    lookup: LookupSpec | None = None
    role: RoleIntent = field(default_factory=RoleIntent)
    person_filters: PersonFilters = field(default_factory=PersonFilters)
    company_filters: CompanyFilters = field(default_factory=CompanyFilters)
    tech_skills: tuple[str, ...] = ()
    soft_criteria: tuple[EvidenceCriterion, ...] = ()
    rank_mode: RankMode = RankMode.DETERMINISTIC
    rank_model: str = "gpt-5.6-luna"
    rank_reasoning_effort: str = "medium"
    rank_approved: bool = False
    sql_candidates: tuple[SqlCandidate, ...] = ()
    bounds: SearchBounds = field(default_factory=SearchBounds)
    recruiting: RecruitingInput | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "search.spec.v1":
            raise ValueError("unsupported SearchSpec schema_version")
        if not isinstance(self.raw_request, str):
            raise ValueError("raw_request must be a string")
        object.__setattr__(self, "profile", Profile(self.profile))
        object.__setattr__(self, "backend", Backend(self.backend))
        object.__setattr__(self, "rank_mode", RankMode(self.rank_mode))
        if not isinstance(self.rank_approved, bool):
            raise ValueError("rank_approved must be boolean")
        object.__setattr__(self, "tech_skills", _tuple(self.tech_skills))
        object.__setattr__(self, "soft_criteria", tuple(self.soft_criteria))
        object.__setattr__(self, "sql_candidates", tuple(self.sql_candidates))
        if (self.backend == Backend.LOCAL) != isinstance(self.corpus, LocalCorpus):
            raise ValueError("backend and corpus kind do not match")
        if (self.profile == Profile.LOOKUP) != (self.lookup is not None):
            raise ValueError("lookup is required only for lookup profile")
        if (self.profile == Profile.RECRUITING) != (self.recruiting is not None):
            raise ValueError("recruiting input is required only for recruiting profile")
        if self.backend == Backend.POWERSET and self.sql_candidates:
            raise ValueError("SQL candidates are local-only")
        if (
            self.person_filters.is_current_role is not None
            and self.company_filters.is_current_company is not None
            and self.person_filters.is_current_role != self.company_filters.is_current_company
        ):
            raise ValueError("role and company currentness constraints cannot conflict")
        if self.soft_criteria and self.rank_mode != RankMode.SEMANTIC:
            raise ValueError("soft criteria require semantic rank_mode")
        if self.rank_mode == RankMode.SEMANTIC and not self.soft_criteria:
            raise ValueError("semantic rank_mode requires soft criteria")
        if self.rank_model != "gpt-5.6-luna":
            raise ValueError("typed GTM semantic rank_model must be gpt-5.6-luna")
        if self.rank_reasoning_effort != "medium":
            raise ValueError("typed GTM rank_reasoning_effort must be medium")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchSpec":
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        _strict(data, fields, "SearchSpec")
        if data.get("schema_version") != "search.spec.v1":
            raise ValueError("unsupported SearchSpec schema_version")
        backend, profile = Backend(data["backend"]), Profile(data["profile"])
        corpus_data = data.get("corpus")
        if not isinstance(corpus_data, dict):
            raise ValueError("corpus is required")
        corpus = (
            LocalCorpus.from_dict(corpus_data)
            if corpus_data.get("kind") == "local"
            else PowersetCorpus.from_dict(corpus_data)
        )
        if (backend == Backend.LOCAL) != isinstance(corpus, LocalCorpus):
            raise ValueError("backend and corpus kind do not match")
        lookup = LookupSpec.from_dict(data["lookup"]) if data.get("lookup") else None
        recruiting = RecruitingInput.from_dict(data["recruiting"]) if data.get("recruiting") else None
        if (profile == Profile.LOOKUP) != (lookup is not None):
            raise ValueError("lookup is required only for lookup profile")
        if (profile == Profile.RECRUITING) != (recruiting is not None):
            raise ValueError("recruiting input is required only for recruiting profile")
        sql = tuple(SqlCandidate.from_dict(row) for row in (data.get("sql_candidates") or []))
        if backend == Backend.POWERSET and sql:
            raise ValueError("SQL candidates are local-only")
        return cls(
            "search.spec.v1",
            data.get("raw_request"),
            profile,
            backend,
            corpus,
            lookup,
            RoleIntent.from_dict(data.get("role") or {}),
            PersonFilters.from_dict(data.get("person_filters") or {}),
            CompanyFilters.from_dict(data.get("company_filters") or {}),
            _tuple(data.get("tech_skills")),
            tuple(EvidenceCriterion.from_dict(row) for row in (data.get("soft_criteria") or [])),
            RankMode(data.get("rank_mode", "deterministic")),
            str(data.get("rank_model") or "gpt-5.6-luna"),
            str(data.get("rank_reasoning_effort") or "medium"),
            data.get("rank_approved", False),
            sql,
            SearchBounds.from_dict(data.get("bounds") or {}),
            recruiting,
        )

    def to_dict(self) -> dict[str, Any]:
        def plain(value: Any) -> Any:
            if isinstance(value, StrEnum):
                return value.value
            if hasattr(value, "to_dict"):
                return value.to_dict()
            if hasattr(value, "__dataclass_fields__"):
                return {k: plain(v) for k, v in asdict(value).items()}
            if isinstance(value, tuple):
                return [plain(v) for v in value]
            return value

        return {name: plain(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class RunnerCapabilities:
    backend: Backend
    supported_hard_filters: tuple[str, ...]
    retrieval_lanes: tuple[str, ...]
    supports_tech_skills: bool
    supports_complete_snapshot: bool
    lookup_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSources:
    company_ids: tuple[str, ...] = ()
    education_ids: tuple[str, ...] = ()
    records: tuple[Mapping[str, Any], ...] = ()
    investor_urns: tuple[str, ...] = ()
    investor_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "company_ids", _tuple(self.company_ids))
        object.__setattr__(self, "education_ids", _tuple(self.education_ids))
        object.__setattr__(self, "records", tuple(MappingProxyType(dict(record)) for record in self.records))
        object.__setattr__(self, "investor_urns", _tuple(self.investor_urns))
        object.__setattr__(self, "investor_names", _tuple(self.investor_names))

    @property
    def unresolved_required_inputs(self) -> tuple[str, ...]:
        return tuple(
            str(record.get("input") or "")
            for record in self.records
            if record.get("disposition") == "unresolved" and record.get("required") is True
        )


@dataclass(frozen=True)
class HardFilterSet:
    eligible_count: int
    eligible_person_ids: tuple[str, ...]
    compiled: Mapping[str, Any]


@dataclass(frozen=True)
class SearchPlan:
    spec: SearchSpec
    capabilities: RunnerCapabilities
    resolved_sources: ResolvedSources
    enabled_stages: tuple[str, ...]


Corpus = LocalCorpus | PowersetCorpus
