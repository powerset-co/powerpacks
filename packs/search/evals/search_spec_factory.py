"""Deterministic recall-case compilation and typed search evaluation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from packs.search.pipeline.frontier import StageResult
from packs.search.pipeline.models import (
    Backend,
    CompanyFilters,
    LocalCorpus,
    PersonFilters,
    PowersetCorpus,
    Profile,
    RoleIntent,
    SearchBounds,
    SearchSpec,
)

RESULT_LIMIT_CAP = 1000

ROLE_SPECS: tuple[tuple[tuple[str, ...], dict[str, tuple[str, ...]]], ...] = (
    (
        ("founder", "cofounder", "co-founder"),
        {
            "role_ids": ("founder",),
            "bm25_queries": ("founder", "co-founder", "cofounder", "founding CEO", "founding CTO"),
        },
    ),
    (
        ("devops", "kubernetes"),
        {
            "role_tracks": ("engineering",),
            "bm25_queries": (
                "devops engineer",
                "site reliability engineer",
                "SRE",
                "infrastructure engineer",
                "platform engineer",
            ),
        },
    ),
    (
        ("ai researcher", "ai engineer", "machine learning", " ml "),
        {
            "role_tracks": ("engineering",),
            "bm25_queries": (
                "AI engineer",
                "machine learning engineer",
                "ML engineer",
                "applied scientist",
                "research scientist",
            ),
        },
    ),
    (
        ("data scientist", "data science"),
        {
            "role_tracks": ("data",),
            "bm25_queries": ("data scientist", "machine learning scientist", "analytics scientist", "data science"),
        },
    ),
    (
        ("software engineer", "engineer", "developer"),
        {
            "role_tracks": ("engineering",),
            "bm25_queries": ("software engineer", "software developer", "SWE", "backend engineer", "frontend engineer"),
        },
    ),
    (
        ("product", " pm "),
        {
            "role_tracks": ("product",),
            "bm25_queries": ("product manager", "PM", "product lead", "technical product manager"),
        },
    ),
    (
        ("operations", " ops "),
        {
            "role_tracks": ("operations",),
            "bm25_queries": ("operations", "business operations", "strategy and operations", "ops"),
        },
    ),
    (
        ("finance", "accounting", "banking"),
        {
            "role_tracks": ("finance",),
            "bm25_queries": ("finance", "strategic finance", "accounting", "FP&A", "financial analyst"),
        },
    ),
    (
        ("sales", "gtm", "business development"),
        {
            "role_tracks": ("sales",),
            "bm25_queries": ("sales", "account executive", "business development", "GTM", "revenue"),
        },
    ),
    (
        ("marketing",),
        {
            "role_tracks": ("marketing",),
            "bm25_queries": ("marketing", "growth marketing", "product marketing", "demand generation"),
        },
    ),
    (
        ("investor", "venture capitalist"),
        {
            "role_tracks": ("finance",),
            "bm25_queries": ("investor", "venture capitalist", "general partner", "angel investor"),
        },
    ),
    (
        ("leader", "executive", "cto", "ceo"),
        {
            "seniority_bands": ("director", "vice-president", "c-suite", "partner", "owner"),
            "bm25_queries": ("executive", "CEO", "COO", "CTO", "VP", "head of", "director"),
        },
    ),
)

COMPANY_ALIASES = {
    "facebook": ("Facebook", "Meta"),
    "meta": ("Meta", "Facebook"),
    "twitter": ("Twitter", "X"),
    "google": ("Google", "Alphabet"),
    "airbnb": ("Airbnb",),
    "thumbtack": ("Thumbtack",),
    "vercel": ("Vercel",),
    "insight partners": ("Insight Partners",),
    "jpmorgan chase": ("JPMorgan Chase",),
    "bank of america": ("Bank of America",),
}
INVESTOR_ALIASES = {
    "sequoia": "Sequoia Capital",
    "amplify": "Amplify Partners",
    "elad gil": "Elad Gil",
    "naval ravikant": "Naval Ravikant",
    "peter thiel": "Peter Thiel",
    "sam altman": "Sam Altman",
}
SECTOR_ALIASES = {
    "database": "data",
    "fintech": "fintech",
    "semiconductor": "semiconductors",
    "climate": "climate",
    "crypto": "crypto_web3",
    "cybersecurity": "security",
    "healthcare": "healthcare",
    "mental health": "healthcare",
    "logistics": "logistics",
    "saas": "enterprise_saas",
    "developer": "infra_devtools",
    "infrastructure": "infra_devtools",
    "artificial intelligence": "ai_ml",
}

SUPPORTED_OVERRIDE_FIELDS = {
    "role_ids",
    "titles",
    "bm25_queries",
    "cities",
    "states",
    "countries",
    "metro_areas",
    "seniority_bands",
    "role_tracks",
    "education_ids",
    "education_names",
    "is_current",
    "is_current_role",
    "years_experience_min",
    "years_experience_max",
    "company_ids",
    "company_names",
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
}
UNSUPPORTED_LEGACY_FIELDS = {
    "company_macro_regions",
    "company_sector_strategy",
    "company_semantic_queries",
    "degree_levels",
    "education_op",
    "fields_of_study",
    "funding_amount_max",
    "funding_amount_min",
    "graduation_year_max",
    "graduation_year_min",
    "li_followers_min",
    "macro_regions",
    "position_after_date",
    "position_before_date",
    "x_followers_min",
}


class UnsupportedCaseError(ValueError):
    """Raised when a legacy case cannot be represented without losing intent."""

    def __init__(self, fields: Sequence[str]):
        self.fields = tuple(sorted(dict.fromkeys(fields)))
        super().__init__(f"unsupported legacy filter fields: {', '.join(self.fields)}")


@dataclass(frozen=True)
class CaseMeta:
    path: Path
    relpath: str
    bucket: str
    query: str
    limit: int
    expected_count: int
    min_recall: float
    expected_ids: tuple[str, ...]
    ignored_v4_ids: tuple[str, ...]
    data: dict[str, Any]


def base_uuid(value: str) -> str:
    parts = str(value).split("-")
    return "-".join(parts[:5]) if len(parts) == 6 and parts[5].isdigit() else str(value)


def uuid_version(value: str) -> str | None:
    raw = base_uuid(value).lower()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw):
        return None
    return raw.split("-")[2][0]


def bucket_for(relpath: str) -> str:
    if relpath.startswith("staging/"):
        return "staging"
    stem = Path(relpath).stem
    for prefix in (
        "date_range",
        "company",
        "education",
        "founders",
        "funding",
        "industry",
        "investor",
        "leaders",
        "location",
        "mixed",
        "role",
        "skills",
        "social",
    ):
        if stem == prefix or stem.startswith(prefix + "_"):
            return prefix
    return stem.split("_", 1)[0]


def load_case(path: Path, recall_dir: Path) -> CaseMeta:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"recall case must be an object: {path}")
    relpath = path.relative_to(recall_dir).as_posix()
    expected = [base_uuid(str(value)) for value in data.get("expected_person_ids") or ()]
    return CaseMeta(
        path=path,
        relpath=relpath,
        bucket=bucket_for(relpath),
        query=str(data.get("query") or path.stem.replace("_", " ")),
        limit=int(data.get("limit") or RESULT_LIMIT_CAP),
        expected_count=int(data.get("expected_count") or 0),
        min_recall=float(data.get("min_recall") or 0.5),
        expected_ids=tuple(dict.fromkeys(value for value in expected if uuid_version(value) != "4")),
        ignored_v4_ids=tuple(dict.fromkeys(value for value in expected if uuid_version(value) == "4")),
        data=data,
    )


def select_cases(recall_dir: Path, bucket: str | None, case_glob: str | None, include_staging: bool) -> list[CaseMeta]:
    cases = [load_case(path, recall_dir) for path in sorted(recall_dir.rglob("*.yaml"))]
    if not include_staging:
        cases = [case for case in cases if case.bucket != "staging"]
    if bucket:
        cases = [case for case in cases if case.bucket == bucket]
    if case_glob:
        regex = re.compile(case_glob)
        cases = [case for case in cases if regex.search(case.relpath)]
    return cases


def case_id(meta: CaseMeta) -> str:
    return Path(meta.relpath).with_suffix("").as_posix().replace("/", "__")


def _add(payload: dict[str, Any], key: str, values: Sequence[Any]) -> None:
    combined = [*(payload.get(key) or ()), *(value for value in values if value not in (None, ""))]
    if combined:
        payload[key] = list(dict.fromkeys(combined))


def decompose_case(meta: CaseMeta) -> dict[str, Any]:
    """Compile structured legacy query conventions without model dispatch."""
    query = f" {meta.query.casefold()} "
    role = next((value for needles, value in ROLE_SPECS if any(needle in query for needle in needles)), None)
    payload = {key: list(value) for key, value in (role or {"bm25_queries": (meta.query,)}).items()}

    if "argentina" in query:
        _add(payload, "countries", ("Argentina",))
    if "san francisco" in query or re.search(r"\bsf\b", query):
        _add(payload, "cities", ("San Francisco",))
        _add(payload, "states", ("California",))
    if "california" in query:
        _add(payload, "states", ("California",))
    if "new york" in query or "nyc" in query:
        _add(payload, "cities", ("New York",))
        _add(payload, "states", ("New York",))
    if "united states" in query or " usa " in query:
        _add(payload, "countries", ("United States",))
    if "middle east" in query:
        _add(payload, "macro_regions", ("Middle East",))
    if "europe" in query:
        _add(payload, "company_macro_regions" if "headquarter" in query else "macro_regions", ("Europe",))

    for needle, names in COMPANY_ALIASES.items():
        if needle in query:
            _add(payload, "company_names", names)
    for needle, name in INVESTOR_ALIASES.items():
        if needle in query and any(word in query for word in ("backed", "funded", "investor")):
            _add(payload, "investor_names", (name,))
    for needle, sector in SECTOR_ALIASES.items():
        if needle in query:
            _add(payload, "sector_types", (sector,))
    if re.search(r"\bai\b", query):
        _add(payload, "sector_types", ("ai_ml",))
    if "startup" in query:
        _add(payload, "entity_types", ("venture_backed_startup",))
    if "public compan" in query:
        _add(payload, "entity_types", ("public_company",))
    if "series a" in query:
        payload.update(funding_stage_min="series_a", funding_stage_max="series_a")
    if "series b or later" in query or "series b plus" in query:
        payload["funding_stage_min"] = "series_b"
    elif "series b" in query:
        payload.update(funding_stage_min="series_b", funding_stage_max="series_b")
    if "seed or earlier" in query or "early stage" in query:
        payload["funding_stage_max"] = "seed"
    if "50 headcount" in query:
        payload["headcount_max"] = 50
    if "over 2mm" in query or "over 2m" in query:
        payload["funding_amount_min"] = 2_000_000

    for needle, name in {
        "stanford": "Stanford University",
        "berkeley": "University of California, Berkeley",
        "wharton": "Wharton School",
        "harvard": "Harvard University",
    }.items():
        if needle in query:
            _add(payload, "education_names", (name,))
    if re.search(r"\bmit\b", query):
        _add(payload, "education_names", ("Massachusetts Institute of Technology",))
    if "both stanford and berkeley" in query or "both stanford and cal" in query:
        payload["education_op"] = "and"
    if "phd" in query:
        _add(payload, "degree_levels", ("phd",))
    if "psychology" in query:
        _add(payload, "fields_of_study", ("psychology",))
    if "graduat" in query or " grads " in query:
        if "recent stanford" in query:
            payload.update(graduation_year_min=2023, graduation_year_max=2026)
        elif "recent grads" in query or "last 5 years" in query:
            payload.update(graduation_year_min=2021, graduation_year_max=2026)
    else:
        around = re.search(r"around\s+(20\d{2})", query)
        between = re.search(r"between\s+(20\d{2})\s+(?:and|to|-)\s+(20\d{2})", query)
        if around:
            year = int(around.group(1))
            payload.update(position_after_date=str(year - 1), position_before_date=str(year + 1))
        if between:
            payload.update(position_after_date=between.group(1), position_before_date=between.group(2))
        after = re.search(r"(?:after|since)\s+(20\d{2})", query)
        if after:
            payload["position_after_date"] = after.group(1)
    if "at least 10 years" in query:
        payload["years_experience_min"] = 10
    elif "at least 2 years" in query:
        payload["years_experience_min"] = 2

    for needle, skill in (
        ("kubernetes", "kubernetes"),
        ("python", "python"),
        ("machine learning", "machine_learning"),
        ("credit risk", "credit_risk"),
        ("blockchain", "blockchain"),
    ):
        if needle in query:
            _add(payload, "tech_skills", (skill,))
    if "100k linkedin" in query:
        payload["li_followers_min"] = 100_000
    elif "50k linkedin" in query:
        payload["li_followers_min"] = 50_000
    if "10k+ twitter" in query or "10k x" in query:
        payload["x_followers_min"] = 10_000
    if "current" in query or "currently" in query:
        payload.setdefault("is_current", True)

    explicit = meta.data.get("role_search_filters") or meta.data.get("filters") or {}
    if explicit and not isinstance(explicit, dict):
        raise UnsupportedCaseError(("role_search_filters",))
    for key in SUPPORTED_OVERRIDE_FIELDS | UNSUPPORTED_LEGACY_FIELDS:
        if key in meta.data:
            payload[key] = meta.data[key]
    if meta.data.get("use_expand_seniority") and not payload.get("seniority_bands"):
        payload["seniority_bands"] = ["director", "vice-president", "c-suite", "partner", "owner"]
    payload.update(explicit)
    return payload


def make_corpus(
    backend: Backend | str, *, db_path: str | None = None, set_id: str | None = None, operator_ids: Sequence[str] = ()
) -> LocalCorpus | PowersetCorpus:
    backend = Backend(backend)
    if backend == Backend.LOCAL:
        if not db_path:
            raise ValueError("local evals require an explicit db_path")
        return LocalCorpus(db_path)
    if not set_id or not operator_ids:
        raise ValueError("Powerset evals require explicit set_id and operator_ids")
    return PowersetCorpus(set_id, tuple(operator_ids))


def build_search_spec(
    meta: CaseMeta,
    *,
    backend: Backend | str,
    db_path: str | None = None,
    set_id: str | None = None,
    operator_ids: Sequence[str] = (),
    limit_cap: int = RESULT_LIMIT_CAP,
) -> SearchSpec:
    payload = decompose_case(meta)
    unsupported = sorted(key for key in payload if key not in SUPPORTED_OVERRIDE_FIELDS)
    if unsupported:
        raise UnsupportedCaseError(unsupported)
    limit = max(1, min(meta.limit, limit_cap))
    current = payload.get("is_current_role", payload.get("is_current"))
    return SearchSpec(
        "search.spec.v1",
        meta.query,
        Profile.GTM,
        Backend(backend),
        make_corpus(backend, db_path=db_path, set_id=set_id, operator_ids=operator_ids),
        role=RoleIntent(
            tuple(payload.get("role_ids") or ()),
            tuple(payload.get("titles") or ()),
            tuple(payload.get("bm25_queries") or ()),
        ),
        person_filters=PersonFilters(
            tuple(payload.get("cities") or ()),
            tuple(payload.get("states") or ()),
            tuple(payload.get("countries") or ()),
            tuple(payload.get("metro_areas") or ()),
            tuple(payload.get("seniority_bands") or ()),
            tuple(payload.get("role_tracks") or ()),
            tuple(payload.get("education_ids") or ()),
            tuple(payload.get("education_names") or ()),
            current,
            payload.get("years_experience_min"),
            payload.get("years_experience_max"),
        ),
        company_filters=CompanyFilters(
            tuple(payload.get("company_ids") or ()),
            tuple(payload.get("company_names") or ()),
            tuple(payload.get("investor_names") or ()),
            tuple(payload.get("sector_types") or ()),
            tuple(payload.get("technology_types") or ()),
            tuple(payload.get("entity_types") or ()),
            payload.get("funding_stage_min"),
            payload.get("funding_stage_max"),
            payload.get("headcount_min"),
            payload.get("headcount_max"),
            payload.get("is_current_company"),
        ),
        tech_skills=tuple(payload.get("tech_skills") or ()),
        bounds=SearchBounds(
            retrieval_limit=limit,
            output_limit=limit,
            semantic_rank_limit=min(50, limit),
            frontier_limit=max(500, limit),
            sourced_candidate_limit=max(1000, limit),
        ),
    )


def evaluate_result(meta: CaseMeta, stage: StageResult) -> dict[str, Any]:
    candidate_ids = {base_uuid(row.person_id) for row in stage.frontier.candidates}
    expected = set(meta.expected_ids)
    hits = sorted(expected & candidate_ids)
    recall = len(hits) / len(expected) if expected else None
    passed = recall >= meta.min_recall if recall is not None else stage.frontier.output_count >= meta.expected_count
    hydrated = stage.counts.get("hydrated")
    if hydrated is None:
        hydrated = sum(row.hydration_disposition == "hydrated" for row in stage.frontier.candidates)
    return {
        "id": case_id(meta),
        "source": meta.relpath,
        "bucket": meta.bucket,
        "query": meta.query,
        "status": "pass" if stage.status in {"completed", "completed_empty"} and passed else "fail",
        "pipeline_status": stage.status,
        "stage": stage.stage,
        "expected_id_count": len(expected),
        "ignored_v4_count": len(meta.ignored_v4_ids),
        "expected_count": meta.expected_count,
        "eligible_pool": stage.counts.get("eligible_pool"),
        "returned_people": stage.frontier.output_count,
        "hydrated": hydrated,
        "hit_count": len(hits),
        "recall": recall,
        "missed_ids": sorted(expected - candidate_ids)[:20],
        "counts": dict(stage.counts),
        "artifacts": dict(stage.artifact_paths),
        "reason": "; ".join((*stage.errors, *stage.warnings)),
    }


def run_case(
    meta: CaseMeta,
    *,
    output_root: Path,
    backend: Backend | str,
    db_path: str | None = None,
    set_id: str | None = None,
    operator_ids: Sequence[str] = (),
    limit_cap: int = RESULT_LIMIT_CAP,
    run_search_fn: Callable[..., StageResult] | None = None,
) -> dict[str, Any]:
    base = {
        "id": case_id(meta),
        "source": meta.relpath,
        "bucket": meta.bucket,
        "query": meta.query,
        "expected_id_count": len(meta.expected_ids),
        "ignored_v4_count": len(meta.ignored_v4_ids),
        "expected_count": meta.expected_count,
    }
    if not meta.expected_ids and not meta.expected_count:
        return {**base, "status": "ignored", "reason": "no comparable expected IDs or expected_count"}
    try:
        spec = build_search_spec(
            meta, backend=backend, db_path=db_path, set_id=set_id, operator_ids=operator_ids, limit_cap=limit_cap
        )
    except UnsupportedCaseError as exc:
        return {**base, "status": "unsupported_case", "unsupported_fields": list(exc.fields), "reason": str(exc)}
    if run_search_fn is None:
        from packs.search.pipeline.search import run_search

        run_search_fn = run_search
    stage = run_search_fn(spec, output_dir=output_root / case_id(meta))
    row = evaluate_result(meta, stage)
    if stage.status == "unsupported_capability":
        row["status"] = "unsupported_capability"
    return row


def write_report(results: Sequence[dict[str, Any]], report_path: Path, title: str) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {title}",
        "",
        "Scope: deterministic recall YAML decomposition → typed `SearchSpec` → `run_search` → canonical frontier/artifacts.",
        "",
        "| Case | Bucket | Disposition | Engine status | Eligible | Returned | Hydrated | Hits/Expected | Recall | Notes |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in results:
        recall = row.get("recall")
        note = str(row.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| {row['source']} | {row['bucket']} | {row['status']} | {row.get('pipeline_status', '')} | {row.get('eligible_pool', '')} | {row.get('returned_people', '')} | {row.get('hydrated', '')} | {row.get('hit_count', 0)}/{row.get('expected_id_count', 0)} | {'' if recall is None else f'{recall:.0%}'} | {note} |"
        )
    report_path.write_text("\n".join(lines) + "\n")


def list_payload(cases: Sequence[CaseMeta]) -> list[dict[str, Any]]:
    return [
        {"id": case_id(case), "bucket": case.bucket, "query": case.query, "expected_ids": len(case.expected_ids)}
        for case in cases
    ]


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))
