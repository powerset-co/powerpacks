"""Dataclass projections of the GTM API request and result contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal

from pydantic import ConfigDict

Source = Literal["network", "company", "sales_nav"]
FitStatus = Literal["qualified", "unqualified", "unknown"]
Sort = Literal["fit", "company_headcount", "name"]
PathType = Literal["direct", "teammate", "mutual", "inferred_coworker"]


@dataclass(frozen=True)
class TitleFilter:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    id: int
    name: str


@dataclass(frozen=True)
class Filters:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    company_ids: tuple[str, ...] = ()
    company_names: tuple[str, ...] = ()
    company_urls: tuple[str, ...] = ()
    company_queries: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    title_ids: tuple[TitleFilter, ...] = ()
    role_function: str | None = None
    seniority_bands: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    is_current: bool | None = True
    excluded_titles: tuple[str, ...] = ()
    excluded_company_terms: tuple[str, ...] = ()
    company_entity_types: tuple[str, ...] = ()
    headcount_min: int | None = None
    headcount_max: int | None = None
    semantic_traits: tuple[str, ...] = ()


@dataclass(frozen=True)
class Position:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    position_title: str
    company_id: str | None = None
    company_name: str | None = None
    company_headcount: int | None = None
    is_current: bool | None = None


@dataclass(frozen=True)
class Profile:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    person_id: str
    name: str
    linkedin_url: str | None = None
    positions: tuple[Position, ...] = ()


@dataclass(frozen=True)
class Evidence:
    source: str
    reference: str
    detail: str = ""
    observed_at: str | None = None


@dataclass(frozen=True)
class Candidate:
    profile: Profile
    member_id: int | None = None
    profile_id: str | None = None
    sources: tuple[Source, ...] = ()
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class SourceCursor:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    source: Source
    offset: int = 0
    account_id: str | None = None
    company_id: str | None = None


@dataclass(frozen=True)
class DiscoverRequest:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    query: str = ""
    set_id: str | None = None
    target: str | None = None
    filters: Filters = field(default_factory=Filters)
    sources: tuple[Source, ...] = ("network", "company")
    max_pages: int = 1
    page_size: int = 25
    offset: int = 0
    allow_provider_calls: bool = False
    max_provider_calls: int = 0
    max_semantic_calls: int = 0
    semantic_model: str | None = None
    selection: tuple[Candidate, ...] = ()
    cursor: SourceCursor | None = None


@dataclass(frozen=True)
class SalesNavQuery:
    filters: tuple[str, ...]
    start: int
    count: int
    keywords: str | None


@dataclass(frozen=True)
class Coverage:
    source: Source | Literal["semantic"]
    loaded: int = 0
    total: int | None = None
    total_is_exact: bool = True
    has_more: bool = False
    next_offset: int | None = None
    cache_hits: int = 0
    provider_calls: int = 0
    semantic_calls: int = 0
    model: str | None = None
    max_output_tokens: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    provider: Literal["sales_nav", "rapidapi", "unipile"] | None = None
    company_id: str | None = None
    queries: tuple[SalesNavQuery, ...] = ()
    error: str | None = None
    account_id: str | None = None
    filters_applied: tuple[str, ...] = ()
    postfilters: tuple[str, ...] = ()
    status: Literal["complete", "partial", "unavailable", "cache_miss"] = "complete"


@dataclass(frozen=True)
class Fit:
    candidate: Candidate
    status: FitStatus
    matched_position_index: int | None = None
    reasons: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    score: float | None = None


@dataclass(frozen=True)
class PathLink:
    from_id: str
    to_id: str
    evidence: tuple[Evidence, ...]
    interaction_count: int | None = None
    last_interaction_at: str | None = None
    overlap_start: str | None = None
    overlap_end: str | None = None
    company_id: str | None = None
    company_headcount: int | None = None
    strength: float | None = None


@dataclass(frozen=True)
class IntroPath:
    kind: PathType
    links: tuple[PathLink, ...]
    intermediary_name: str | None = None


@dataclass(frozen=True)
class RankedCandidate:
    fit: Fit
    paths: tuple[IntroPath, ...] = ()


@dataclass(frozen=True)
class SortRequest:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    candidates: tuple[RankedCandidate, ...]
    sort: Sort = "fit"
    descending: bool = True


@dataclass(frozen=True)
class StageTiming:
    stage: Literal["resolve", "discover", "assess"]
    elapsed_ms: int


@dataclass(frozen=True)
class Result:
    query: str
    filters: Filters
    sources: tuple[Source, ...]
    candidates: tuple[RankedCandidate, ...]
    coverage: tuple[Coverage, ...]
    warnings: tuple[str, ...] = ()
    elapsed_ms: int = 0
    stage_timings: tuple[StageTiming, ...] = ()


@dataclass(frozen=True)
class Artifacts:
    request: str
    response: str
    candidates: str


@dataclass(frozen=True)
class Manifest:
    status: Literal["complete", "partial", "failed"]
    operation: Literal["discover", "refine", "sort"]
    query: str
    filters: Filters | None
    count: int
    qualified: int
    sources: tuple[Source, ...]
    coverage: tuple[Coverage, ...]
    warnings: tuple[str, ...]
    elapsed_ms: int
    artifacts: Artifacts
    stage_timings: tuple[StageTiming, ...] = ()
