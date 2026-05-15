"""Standalone TurboPuffer client for Powerpacks primitives.

This module uses only checked-in Powerpacks contracts plus external packages
loaded through uv when needed.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from powerpacks_contracts import TURBOPUFFER_FILTER_OPERATORS, TURBOPUFFER_NAMESPACES


STRONG_CONSISTENCY = {"level": "strong"}
DEFAULT_REGION = "gcp-us-central1"
K_RRF = 60
TOKEN_RE = re.compile(r"[a-z0-9]+")
ARRAY_FILTER_FIELDS = {"metro_areas", "allowed_operator_ids", "role_ids"}
LOCATION_FIELDS = ["city", "state", "country", "macro_region", "metro_areas"]
BASE_ID_BATCH_SIZE = int(os.getenv("POWERPACKS_SEARCH_BASE_ID_BATCH_SIZE", "500"))
BASE_ID_BATCH_MIN = int(os.getenv("POWERPACKS_SEARCH_BASE_ID_BATCH_MIN", "501"))
BASE_ID_BATCH_CONCURRENCY = int(os.getenv("POWERPACKS_SEARCH_BASE_ID_BATCH_CONCURRENCY", "8"))
SEARCH_ONLY = "SEARCH_ONLY"
COMPANY_INTERSECTION = "COMPANY_INTERSECTION"
COMPANY_UNION = "COMPANY_UNION"
ADJACENCY_LIMIT = int(os.getenv("POWERPACKS_COMPANY_ADJACENCY_LIMIT", "1000"))
ADJACENCY_EXCLUDE_SENIORITY = ["entry", "trainee"]
ROLE_ADJACENCY_MAP_PATH = Path(__file__).resolve().parents[3] / "data" / "roles" / "role_adjacency.opus.json"
_ROLE_ADJACENCY_MAP: dict[str, list[str]] | None = None
LOCAL_BACKEND_NAMESPACES = {"people", "summaries", "education", "schools"}


ADJACENCY_QUERIES: dict[tuple[str | None, str | None], list[str]] = {
    ("engineer", "leader"): [
        "CTO", "Chief Technology Officer", "VP of Engineering", "VP Engineering",
        "Vice President of Engineering", "Director of Engineering", "Engineering Director",
        "Head of Engineering", "Engineering Manager", "Senior Engineering Manager",
        "Chief Architect", "Principal Architect",
    ],
    ("engineer", "ic"): [
        "Staff Engineer", "Staff Software Engineer", "Principal Engineer", "Principal Software Engineer",
        "Distinguished Engineer", "Technical Lead", "Data Scientist", "Research Scientist",
        "Machine Learning Engineer", "ML Engineer", "Engineering Manager",
    ],
    ("engineer", None): [
        "CTO", "Chief Technology Officer", "VP of Engineering", "VP Engineering",
        "Director of Engineering", "Engineering Director", "Head of Engineering",
        "Data Scientist", "Research Scientist", "Machine Learning Engineer", "Staff Engineer",
        "Principal Engineer", "Technical Lead", "Staff Software Engineer", "Engineering Manager",
    ],
    ("data_ml", "leader"): [
        "Chief Data Officer", "CDO", "CTO", "Chief Technology Officer", "VP of Data",
        "VP of Data Science", "VP Data", "VP of Analytics", "Director of Data Science",
        "Director of Analytics", "Director of Machine Learning", "Head of Data Science",
        "Head of Data", "Head of AI",
    ],
    ("data_ml", "ic"): [
        "Staff Data Scientist", "Principal Data Scientist", "Research Scientist", "Applied Scientist",
        "Machine Learning Engineer", "ML Engineer", "Senior Data Engineer", "Platform Engineer",
        "Software Engineer", "Data Engineer",
    ],
    ("data_ml", None): [
        "CTO", "Chief Data Officer", "VP of Data Science", "VP Data", "Director of Data Science",
        "Head of Data Science", "Head of Data", "Head of AI", "Data Engineer", "ML Engineer",
        "Machine Learning Engineer", "Research Scientist", "Applied Scientist",
    ],
    ("product", "leader"): [
        "CPO", "Chief Product Officer", "VP of Product", "VP Product", "Vice President of Product",
        "Director of Product", "Product Director", "Head of Product", "General Manager",
        "CTO", "Chief Technology Officer",
    ],
    ("product", "ic"): [
        "Senior Product Manager", "Staff Product Manager", "Technical Program Manager",
        "Program Manager", "Product Designer", "UX Lead", "Engineering Manager", "Product Analyst",
    ],
    ("product", None): [
        "CPO", "Chief Product Officer", "VP of Product", "VP Product", "Director of Product",
        "Product Director", "Head of Product", "Engineering Manager", "Technical Program Manager",
        "Program Manager", "UX Lead", "Design Director", "Head of Design", "Product Designer",
    ],
    ("founder", None): [
        "CTO", "Chief Technology Officer", "VP of Engineering", "VP Engineering",
        "Director of Engineering", "Head of Product", "Head of Growth", "Chief of Staff", "COO",
    ],
    ("marketing", "leader"): [
        "CMO", "Chief Marketing Officer", "VP of Marketing", "VP Marketing",
        "Director of Marketing", "Head of Marketing", "VP of Growth", "Head of Growth",
        "Chief Revenue Officer", "CRO",
    ],
    ("marketing", "ic"): [
        "Senior Marketing Manager", "Growth Lead", "Demand Generation Manager", "Content Lead",
        "Brand Manager", "Product Marketing Manager", "Marketing Analyst",
    ],
    ("marketing", None): [
        "CMO", "Chief Marketing Officer", "VP of Marketing", "VP Marketing", "Director of Marketing",
        "Head of Marketing", "Growth Lead", "Demand Generation Manager", "Content Lead", "Brand Manager",
    ],
    ("sales", "leader"): [
        "CRO", "Chief Revenue Officer", "VP of Sales", "VP Sales", "Vice President of Sales",
        "Director of Sales", "Head of Sales", "VP of Business Development", "Head of Business Development",
    ],
    ("sales", None): [
        "CRO", "Chief Revenue Officer", "VP of Sales", "VP Sales", "Director of Sales",
        "Head of Sales", "Account Executive", "Sales Manager",
    ],
    ("leader", None): [
        "CTO", "CEO", "COO", "CPO", "CFO", "Chief Executive Officer",
        "Chief Technology Officer", "Chief Operating Officer", "Chief Product Officer",
        "Founder", "Co-Founder", "Cofounder", "VP of Engineering", "VP Engineering",
        "VP of Product", "VP Product", "Director of Engineering", "Engineering Director",
        "Director of Product", "General Manager", "Chief of Staff",
    ],
    (None, "leader"): [
        "CTO", "CEO", "COO", "CPO", "CFO", "Chief Executive Officer",
        "Chief Technology Officer", "Founder", "Co-Founder", "VP of Engineering",
        "VP Engineering", "VP of Product", "VP Product", "Director of Engineering",
        "Engineering Director", "Director of Product", "General Manager", "Chief of Staff",
    ],
    (None, "ic"): [
        "Staff Engineer", "Staff Software Engineer", "Principal Engineer", "Data Scientist",
        "Research Scientist", "Senior Product Manager", "Technical Lead", "Engineering Manager",
    ],
    (None, None): [
        "CTO", "CEO", "Chief Technology Officer", "Chief Executive Officer", "VP of Engineering",
        "VP Engineering", "Director of Engineering", "Data Scientist", "Research Scientist",
        "Staff Engineer", "Principal Engineer", "Product Manager", "Engineering Manager",
    ],
}

NON_OPERATIONAL_PATTERN = re.compile(
    r"\binvestor\b|\bboard\b|\badvisor\b|\badvisory\b|\bventure partner\b|"
    r"\blimited partner\b|\bgeneral partner\b|^mentor$|^volunteer\b",
    re.IGNORECASE,
)
RESCUE_OPERATIONAL_PATTERN = re.compile(
    r"\bfounder\b|\bco-?founder\b|\bcofounder\b|\bceo\b|\bcto\b|\bcoo\b|\bcpo\b|\bcfo\b|"
    r"\bchief\b|\bpresident\b|\bhead of\b|\bdirector of\b|\bvp of\b|\bvice president\b|"
    r"\bengineer\b|\bscientist\b|\bdeveloper\b|\bmanager\b",
    re.IGNORECASE,
)

FOUNDER_SEMANTIC_QUERY = (
    "Started, founded, or built a company from scratch, took entrepreneurial risk, "
    "made early strategic decisions, hired initial teams, raised funding or bootstrapped, "
    "and owned company-building outcomes. Profile evidence may include founder, "
    "co-founder, founding executive, founding CEO, founding CTO, or founding team experience."
)
FOUNDER_BM25_QUERIES = ["founder", "co-founder", "cofounder", "founding", "founding CEO", "founding CTO", "founder CEO"]
FOUNDER_PATTERN = re.compile(r"\b(co-?founders?|cofounders?|founders?|founding\s+(?:ceo|cto|team|engineer|member))\b", re.IGNORECASE)
CSUITE_SHORTCUTS = {
    "ceo": {
        "role_id": "chief_executive_officer",
        "display": "Chief Executive Officer",
        "bm25": ["CEO", "Chief Executive Officer", "president", "managing director"],
    },
    "cto": {
        "role_id": "chief_technology_officer",
        "display": "Chief Technology Officer",
        "bm25": ["CTO", "Chief Technology Officer", "SVP Engineering"],
    },
    "cfo": {
        "role_id": "chief_financial_officer",
        "display": "Chief Financial Officer",
        "bm25": ["CFO", "Chief Financial Officer", "head of finance"],
    },
    "cmo": {
        "role_id": "chief_marketing_officer",
        "display": "Chief Marketing Officer",
        "bm25": ["CMO", "Chief Marketing Officer", "VP Marketing"],
    },
    "coo": {
        "role_id": "chief_operating_officer",
        "display": "Chief Operating Officer",
        "bm25": ["COO", "Chief Operating Officer", "head of operations"],
    },
    "cpo": {
        "role_id": "chief_product_officer",
        "display": "Chief Product Officer",
        "bm25": ["CPO", "Chief Product Officer", "VP Product"],
    },
    "cro": {
        "role_id": "chief_revenue_officer",
        "display": "Chief Revenue Officer",
        "bm25": ["CRO", "Chief Revenue Officer", "head of revenue"],
    },
    "ciso": {
        "role_id": "chief_information_security_officer",
        "display": "Chief Information Security Officer",
        "bm25": ["CISO", "Chief Information Security Officer"],
    },
}


def load_env_file(path: Path | None) -> None:
    if not path or not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ensure_packages() -> None:
    missing: list[str] = []
    for module_name in ["turbopuffer", "openai", "snowballstemmer"]:
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            missing.append(module_name)
    if not missing:
        return
    if os.getenv("POWERPACKS_TPUF_UV_REEXEC") == "1":
        raise RuntimeError(f"Missing required packages: {', '.join(missing)}")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError(f"Missing required packages: {', '.join(missing)}. Install uv for auto re-exec.")
    env = os.environ.copy()
    env["POWERPACKS_TPUF_UV_REEXEC"] = "1"
    args = [uv, "run"]
    for package in ["turbopuffer", "openai", "snowballstemmer"]:
        args.extend(["--with", package])
    args.extend(["python", str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
    os.execvpe(uv, args, env)


def namespace_name(logical_name: str = "people") -> str:
    env_key = f"POWERPACKS_TURBOPUFFER_{logical_name.upper()}_NAMESPACE"
    configured = os.getenv(env_key)
    if configured:
        return configured
    base = TURBOPUFFER_NAMESPACES[logical_name]
    env = os.getenv("ALEPH_ENV", "").strip().lower()
    if not env or env == "prod":
        return base
    if env == "staging":
        env = "dev"
    return base if base.endswith(f"_{env}") else f"{base}_{env}"


def client() -> Any:
    ensure_packages()
    import turbopuffer

    api_key = os.getenv("TURBOPUFFER_API_KEY")
    if not api_key:
        raise RuntimeError("TURBOPUFFER_API_KEY is required")
    return turbopuffer.Turbopuffer(api_key=api_key, region=os.getenv("TURBOPUFFER_REGION", DEFAULT_REGION))


def is_local_backend() -> bool:
    return os.getenv("POWERPACKS_SEARCH_BACKEND", "").strip().lower() == "local"


@functools.lru_cache(maxsize=None)
def _local_store_for_path(path: str) -> Any:
    from local_duckdb_store import LocalDuckDBSearchStore

    return LocalDuckDBSearchStore(path)


def local_store() -> Any:
    db_path = os.getenv("POWERPACKS_LOCAL_SEARCH_DB")
    if not db_path:
        raise RuntimeError("POWERPACKS_LOCAL_SEARCH_DB is required when POWERPACKS_SEARCH_BACKEND=local")
    return _local_store_for_path(db_path)


def namespace(logical_name: str = "people") -> Any:
    if is_local_backend() and logical_name in LOCAL_BACKEND_NAMESPACES:
        return local_store().namespace(logical_name)
    # Companies and investors remain TurboPuffer-backed in local mode unless the
    # caller provides resolved IDs; local DuckDB covers people, summaries,
    # education, and schools only.
    return client().namespace(namespace_name(logical_name))


def stemmer() -> Any:
    ensure_packages()
    import snowballstemmer

    return snowballstemmer.stemmer("english")


def phrase_query_tokenize(text: str) -> list[str]:
    stems = [stemmer().stemWord(token) for token in TOKEN_RE.findall(text.lower())]
    return [" ".join(stems)] if stems else []


def word_tokenize(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text.lower())
    result = list(tokens)
    for idx in range(len(tokens) - 1):
        result.append(f"{tokens[idx]} {tokens[idx + 1]}")
    return result


def bm25_queries_per_field(queries: list[str]) -> dict[str, list[str]]:
    phrase_tokens: list[str] = []
    word_tokens: list[str] = []
    for query in queries:
        phrase_tokens.extend(phrase_query_tokenize(query))
        word_tokens.extend(word_tokenize(query))
    result: dict[str, list[str]] = {}
    if phrase_tokens:
        result["phrase_tokens"] = list(dict.fromkeys(phrase_tokens))
    if word_tokens:
        result["word_tokens"] = list(dict.fromkeys(word_tokens))
    return result


async def embedding(text: str) -> list[float]:
    ensure_packages()
    import openai

    client = openai.AsyncOpenAI()
    response = await client.embeddings.create(input=[text], model=os.getenv("POWERPACKS_EMBEDDING_MODEL", "text-embedding-3-small"))
    return response.data[0].embedding


def base_person_id(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 6 and parts[5].isdigit():
        return "-".join(parts[:5])
    return str(value)


def row_attrs(row: Any, include_attributes: list[str]) -> dict[str, Any]:
    attrs: dict[str, Any] = {"id": str(row.id)}
    extra = getattr(row, "model_extra", {}) or {}
    for key in include_attributes:
        if key in extra:
            attrs[key] = extra[key]
        else:
            attrs[key] = getattr(row, key, None)
    return attrs


def validate_filter_tuple(value: Any) -> None:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"invalid TurboPuffer filter tuple: {value!r}")
    if value[0] in {"And", "Or"}:
        if not isinstance(value[1], list):
            raise ValueError(f"{value[0]} filter must contain a list of clauses")
        for clause in value[1]:
            validate_filter_tuple(clause)
        return
    if len(value) < 3:
        raise ValueError(f"invalid TurboPuffer filter tuple: {value!r}")
    field, op = str(value[0]), str(value[1])
    allowed_ops = TURBOPUFFER_FILTER_OPERATORS.get(field)
    if not allowed_ops:
        raise ValueError(f"unsupported TurboPuffer filter field: {field}")
    if op not in allowed_ops:
        raise ValueError(f"unsupported operator {op!r} for field {field!r}; allowed={allowed_ops}")


def filter_expression_to_tuple(expr: dict[str, Any] | None) -> tuple | None:
    if not expr:
        return None
    op = expr.get("op")
    if op in {"And", "Or"}:
        clauses = [filter_expression_to_tuple(clause) for clause in expr.get("clauses", [])]
        clauses = [clause for clause in clauses if clause is not None]
        if not clauses:
            return None
        result = clauses[0] if len(clauses) == 1 else (op, clauses)
        validate_filter_tuple(result)
        return result
    field = expr.get("field")
    operator = expr.get("op")
    result = (field, operator, expr.get("value"))
    validate_filter_tuple(result)
    return result


def comparison(field: str, op: str, value: Any) -> tuple:
    result = (field, op, value)
    validate_filter_tuple(result)
    return result


def allowed_operator_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    explicit = payload.get("operator_ids") or payload.get("allowed_operator_ids")
    if explicit:
        return list(dict.fromkeys(str(value) for value in explicit if value))

    # `set_id` is a Powerset set UUID, not a TurboPuffer operator id. Resolve it
    # through Postgres before applying the `allowed_operator_ids` filter. When no
    # set_id is present, low-level filters only inherit explicit env defaults;
    # personal-set fallback lives in the resolve_set_operators primitive so
    # import-time/unit-test filter construction never unexpectedly hits Postgres.
    set_id = str(payload["set_id"]) if payload.get("set_id") else (
        os.getenv("POWERPACKS_DEFAULT_SET_ID") or os.getenv("POWERSET_DEFAULT_SET_ID")
    )
    if not set_id:
        return []
    try:
        from postgres_client import fetch_set_operator_ids  # type: ignore

        resolved = fetch_set_operator_ids(set_id)
        return list(dict.fromkeys(str(value) for value in resolved.get("operator_ids") or [] if value))
    except RuntimeError:
        raise


def _dedupe_strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _payload_text(payload: dict[str, Any], query: str | None = None) -> str:
    parts: list[str] = []
    if query:
        parts.append(str(query))
    for key in ["semantic_query", "role_semantic_query"]:
        if payload.get(key):
            parts.append(str(payload[key]))
    parts.extend(str(value) for value in payload.get("bm25_queries") or [])
    parts.extend(str(value) for value in payload.get("role_ids") or [])
    return " ".join(parts)


def _query_without_named_entities(payload: dict[str, Any], query: str | None) -> str:
    text = str(query or "")
    for key in ["investor_names", "company_names"]:
        for value in payload.get(key) or []:
            phrase = str(value).strip()
            if phrase:
                text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    return text


def detects_founder_shortcut(payload: dict[str, Any], query: str | None = None) -> bool:
    # Powerpacks uses canonical role_ids as the founder signal.
    role_ids = {str(value).lower() for value in payload.get("role_ids") or []}
    if role_ids & {"founder", "cofounder", "co-founder"}:
        return True

    # Fallback for Powerpacks agents that have not emitted role_ids yet: inspect
    # only role-ish fields plus query text with explicit company/investor names
    # removed, so an investor like "Founders Fund" does not become founder role.
    role_text = _payload_text({k: v for k, v in payload.items() if k not in {"investor_names", "company_names"}})
    query_text = _query_without_named_entities(payload, query)
    return bool(FOUNDER_PATTERN.search(f"{role_text} {query_text}"))


def detect_csuite_shortcut(payload: dict[str, Any], query: str | None = None) -> dict[str, Any] | None:
    text = _payload_text(payload, query).lower()
    words = set(TOKEN_RE.findall(text))
    for abbrev, spec in CSUITE_SHORTCUTS.items():
        if abbrev in words or f"{abbrev}s" in words or str(spec["display"]).lower() in text:
            return spec
    role_ids = {str(value).lower() for value in payload.get("role_ids") or []}
    for spec in CSUITE_SHORTCUTS.values():
        if str(spec["role_id"]).lower() in role_ids:
            return spec
    return None


def apply_role_shortcuts(payload: dict[str, Any], query: str | None = None) -> dict[str, Any]:
    payload = dict(payload)
    if detects_founder_shortcut(payload, query):
        payload["role_ids"] = _dedupe_strings([*(payload.get("role_ids") or []), "founder"])
        payload["bm25_queries"] = _dedupe_strings([*(payload.get("bm25_queries") or []), *FOUNDER_BM25_QUERIES])
        if len(str(payload.get("semantic_query") or "")) < 80:
            payload["semantic_query"] = FOUNDER_SEMANTIC_QUERY
        # Founder exists at all seniority levels; copying c-suite/owner bands hurts recall.
        payload.pop("seniority_bands", None)
        return payload

    csuite = detect_csuite_shortcut(payload, query)
    if csuite:
        payload["role_ids"] = _dedupe_strings([*(payload.get("role_ids") or []), csuite["role_id"]])
        payload["bm25_queries"] = _dedupe_strings([*(payload.get("bm25_queries") or []), *csuite["bm25"]])
        if not payload.get("seniority_bands"):
            payload["seniority_bands"] = ["c_suite"]
        if len(str(payload.get("semantic_query") or "")) < 80:
            payload["semantic_query"] = (
                f"Executive leader serving as {csuite['display']}, responsible for strategic direction, "
                "organizational leadership, senior decision-making, cross-functional execution, and accountability "
                "for company or department outcomes. Profile evidence should include a current or past C-suite title."
            )
    return payload


def filters_from_role_payload(payload: dict[str, Any]) -> tuple | None:
    payload = apply_role_shortcuts(payload)
    hard = filter_expression_to_tuple(payload.get("hard_filters"))
    if hard is not None:
        return hard

    filters: list[tuple] = []
    field_map = {
        "cities": ("city", "In"),
        "states": ("state", "In"),
        "countries": ("country", "In"),
        "macro_regions": ("macro_region", "In"),
        "metro_areas": ("metro_areas", "ContainsAny"),
    }
    for payload_key, (field, op) in field_map.items():
        values = payload.get(payload_key)
        if values:
            filters.append(comparison(field, op, values))

    if payload.get("seniority_bands"):
        filters.append(comparison("seniority_band", "In", payload["seniority_bands"]))
    if payload.get("company_ids") and company_filter_applies_to_role_search(payload):
        filters.append(comparison("company_id", "In", payload["company_ids"]))
    current_value = None
    if payload.get("company_ids") and payload.get("is_current_company") is not None:
        current_value = payload.get("is_current_company")
    elif is_filter_only_payload(payload) and payload.get("is_current_company") is not None:
        current_value = payload.get("is_current_company")
    elif payload.get("is_current_role") is not None:
        current_value = payload.get("is_current_role")
    if current_value is not None:
        filters.append(comparison("is_current", "Eq", bool(current_value)))
    if payload.get("years_experience_min") is not None:
        filters.append(comparison("total_years_experience", "Gte", payload["years_experience_min"]))
    if payload.get("years_experience_max") is not None:
        filters.append(comparison("total_years_experience", "Lte", payload["years_experience_max"]))
    if payload.get("role_tracks"):
        filters.append(comparison("role_track", "In", payload["role_tracks"]))
    if payload.get("role_ids"):
        filters.append(comparison("role_ids", "ContainsAny", payload["role_ids"]))
    if payload.get("base_candidate_ids"):
        filters.append(comparison("base_id", "In", payload["base_candidate_ids"]))
    operator_ids = allowed_operator_ids_from_payload(payload)
    if operator_ids:
        filters.append(comparison("allowed_operator_ids", "ContainsAny", operator_ids))
    if payload.get("position_before_date"):
        filters.append(comparison("start_date_epoch", "Lte", epoch_for_date(payload["position_before_date"], end_of_period=True)))
    if payload.get("position_after_date"):
        start_epoch = epoch_for_date(payload["position_after_date"])
        filters.append(("Or", [
            comparison("end_date_epoch", "Gte", start_epoch),
            comparison("end_date_epoch", "Eq", 0),
        ]))
    if payload.get("age_min") is not None:
        # age_min means born on/before this year.
        filters.append(comparison("inferred_birth_year", "Lte", _birth_year_for_age(int(payload["age_min"]))))
    if payload.get("age_max") is not None:
        # age_max means born on/after this year.
        filters.append(comparison("inferred_birth_year", "Gte", _birth_year_for_age(int(payload["age_max"]))))
    if not payload.get("base_candidate_ids"):
        for payload_key, field, op in [
            ("x_followers_min", "x_twitter_followers", "Gte"),
            ("x_followers_max", "x_twitter_followers", "Lte"),
            ("li_followers_min", "linkedin_followers", "Gte"),
            ("li_followers_max", "linkedin_followers", "Lte"),
            ("li_connections_min", "linkedin_connections", "Gte"),
            ("li_connections_max", "linkedin_connections", "Lte"),
            ("ig_followers_min", "ig_followers", "Gte"),
            ("ig_followers_max", "ig_followers", "Lte"),
        ]:
            if payload.get(payload_key) is not None:
                filters.append(comparison(field, op, payload[payload_key]))

    if not filters:
        return None
    result = filters[0] if len(filters) == 1 else ("And", filters)
    validate_filter_tuple(result)
    return result


def summarize_filter(value: Any, *, max_list_values: int = 20) -> Any:
    if isinstance(value, tuple):
        return [summarize_filter(item, max_list_values=max_list_values) for item in value]
    if isinstance(value, list):
        if len(value) > max_list_values and all(isinstance(item, str) for item in value):
            return {
                "count": len(value),
                "sample": value[:max_list_values],
                "truncated": True,
            }
        return [summarize_filter(item, max_list_values=max_list_values) for item in value]
    return value


def _birth_year_for_age(age: int) -> int:
    from datetime import date

    return date.today().year - age


def epoch_for_date(value: Any, *, end_of_period: bool = False) -> int:
    text = str(value).strip()
    if not text:
        raise ValueError("empty date value")
    if re.fullmatch(r"\d{4}", text):
        suffix = "-12-31T23:59:59+00:00" if end_of_period else "-01-01T00:00:00+00:00"
        dt = datetime.fromisoformat(text + suffix)
    else:
        normalized = text.replace("Z", "+00:00")
        if "T" not in normalized:
            normalized += "T23:59:59+00:00" if end_of_period else "T00:00:00+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def latest_step_output(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def role_payload_from_state(state: dict[str, Any]) -> dict[str, Any]:
    expansion = latest_step_output(state, "expand_search_request")
    payload = expansion.get("role_search_filters") if isinstance(expansion, dict) else None
    if not isinstance(payload, dict):
        raise RuntimeError("state does not contain expand_search_request.output.role_search_filters")
    payload = dict(payload)

    resolved_companies = latest_step_output(state, "resolve_companies")
    if isinstance(resolved_companies, dict) and resolved_companies.get("company_ids"):
        payload["company_ids"] = list(dict.fromkeys(str(cid) for cid in resolved_companies["company_ids"] if cid))

    resolved_investors = latest_step_output(state, "resolve_investors")
    if isinstance(resolved_investors, dict) and resolved_investors.get("investor_urns"):
        payload["investors"] = list(dict.fromkeys(str(iid) for iid in resolved_investors["investor_urns"] if iid))

    resolved_education = latest_step_output(state, "resolve_education")
    if isinstance(resolved_education, dict) and resolved_education.get("education_ids"):
        payload["education_ids"] = list(dict.fromkeys(str(eid) for eid in resolved_education["education_ids"] if eid))

    prefilters = latest_step_output(state, "apply_prefilters")
    if (
        isinstance(prefilters, dict)
        and prefilters.get("base_candidate_ids") is not None
        and prefilters.get("role_prefilter_ran", True)
    ):
        payload["base_candidate_ids"] = list(dict.fromkeys(str(pid) for pid in prefilters["base_candidate_ids"] if pid))

    resolved_set = latest_step_output(state, "resolve_set_operators")
    if isinstance(resolved_set, dict) and resolved_set.get("operator_ids"):
        payload["operator_ids"] = list(dict.fromkeys(str(oid) for oid in resolved_set["operator_ids"] if oid))
        if resolved_set.get("set_id") and not payload.get("set_id"):
            payload["set_id"] = str(resolved_set["set_id"])

    payload = apply_role_shortcuts(payload, state.get("query"))
    payload.setdefault("search_mode", search_mode_for_payload(payload))
    return payload


def has_company_constraint(payload: dict[str, Any]) -> bool:
    return any(payload.get(key) for key in [
        "company_ids",
        "company_names",
        "current_company_names",
        "company_semantic_queries",
        "sector_types",
        "entity_types",
        "company_locations",
        "company_cities",
        "company_states",
        "company_countries",
        "funding_stage",
        "funding_stages",
        "headcount_min",
        "headcount_max",
        "employee_count_min",
        "employee_count_max",
        "investors",
        "investor_names",
    ])


def has_role_constraint(payload: dict[str, Any]) -> bool:
    semantic_query = str(payload.get("semantic_query") or "").strip()
    return bool(
        len(semantic_query) >= 80
        or payload.get("role_tracks")
        or payload.get("role_ids")
        or payload.get("bm25_queries")
        or payload.get("role_core_patterns")
        or payload.get("role_adjacent_patterns")
        or payload.get("adjacent_role_ids")
        or payload.get("adjacent_departments")
        or payload.get("company_adjacency_queries")
        or payload.get("seniority_bands")
        or payload.get("years_experience_min") is not None
        or payload.get("years_experience_max") is not None
    )


def has_company_domain_intent(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("has_domain_intent")
        or (payload.get("company_semantic_queries") and payload.get("sector_types"))
    )


def is_founder_payload(payload: dict[str, Any]) -> bool:
    return detects_founder_shortcut(payload)


def search_mode_for_payload(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("search_mode") or "").strip().upper()
    if explicit in {SEARCH_ONLY, COMPANY_INTERSECTION, COMPANY_UNION}:
        return explicit
    if not has_company_constraint(payload):
        return SEARCH_ONLY
    if is_founder_payload(payload):
        return COMPANY_INTERSECTION
    if has_role_constraint(payload) and has_company_domain_intent(payload):
        return COMPANY_UNION
    if not has_role_constraint(payload):
        if payload.get("company_ids") and len([key for key in ["company_ids", "company_names", "current_company_names"] if payload.get(key)]) == 1:
            return COMPANY_UNION
        return COMPANY_UNION
    if is_filter_only_payload(payload):
        # Pure company + hard-filter queries (e.g. "who worked at Meta after 2020")
        # should intersect directly, not union with an empty semantic search.
        return COMPANY_INTERSECTION
    return COMPANY_INTERSECTION


def company_filter_applies_to_role_search(payload: dict[str, Any]) -> bool:
    return search_mode_for_payload(payload) != COMPANY_UNION


def seniority_intent(payload: dict[str, Any]) -> str | None:
    bands = {str(value).lower() for value in payload.get("seniority_bands") or []}
    if bands & {"manager", "director", "vice_president", "c_suite"}:
        return "leader"
    if bands & {"entry", "junior", "mid", "senior", "staff", "principal"}:
        return "ic"
    return None


def adjacency_family_for_payload(payload: dict[str, Any]) -> str | None:
    role_ids = {str(value).lower() for value in payload.get("role_ids") or []}
    if role_ids & {"founder", "cofounder", "co-founder"}:
        return "founder"
    if role_ids & {"software_engineer", "backend_engineer", "frontend_engineer", "full_stack_engineer", "engineering_manager", "chief_technology_officer"}:
        return "engineer"
    if role_ids & {"data_scientist", "ml_engineer", "machine_learning_engineer", "data_science_manager"}:
        return "data_ml"
    if role_ids & {"product_manager", "chief_product_officer"}:
        return "product"
    if role_ids & {"marketing_manager", "chief_marketing_officer", "growth_marketer"}:
        return "marketing"
    if role_ids & {"sales_manager", "account_executive", "chief_revenue_officer"}:
        return "sales"
    if role_ids & {"chief_executive_officer", "president", "director", "vice_president", "head_of"}:
        return "leader"
    tracks = {str(value).lower() for value in payload.get("role_tracks") or []}
    if "engineering" in tracks:
        return "engineer"
    if "data" in tracks:
        return "data_ml"
    if "product" in tracks:
        return "product"
    if "marketing" in tracks:
        return "marketing"
    if "sales" in tracks or "business_dev" in tracks:
        return "sales"
    return None


def get_adjacency_queries(adjacency_family: str | None, intent: str | None) -> list[str]:
    for key in [(adjacency_family, intent), (adjacency_family, None), (None, intent), (None, None)]:
        queries = ADJACENCY_QUERIES.get(key)
        if queries:
            return list(queries)
    return []


def load_role_adjacency_map(path: Path = ROLE_ADJACENCY_MAP_PATH) -> dict[str, list[str]]:
    global _ROLE_ADJACENCY_MAP
    if _ROLE_ADJACENCY_MAP is not None:
        return _ROLE_ADJACENCY_MAP
    if not path.exists():
        _ROLE_ADJACENCY_MAP = {}
        return _ROLE_ADJACENCY_MAP
    try:
        data = json.loads(path.read_text())
    except Exception:
        _ROLE_ADJACENCY_MAP = {}
        return _ROLE_ADJACENCY_MAP
    if isinstance(data, dict):
        data.pop("_metadata", None)
        _ROLE_ADJACENCY_MAP = {
            str(key): [str(value) for value in values if value]
            for key, values in data.items()
            if isinstance(values, list)
        }
    else:
        _ROLE_ADJACENCY_MAP = {}
    return _ROLE_ADJACENCY_MAP


def adjacent_role_ids_for(role_ids: list[str]) -> list[str]:
    adjacency = load_role_adjacency_map()
    base = {str(value) for value in role_ids if value}
    out: set[str] = set()
    for role_id in base:
        out.update(value for value in adjacency.get(role_id, []) if value not in base)
    return sorted(out)


def effective_adjacent_role_ids(payload: dict[str, Any]) -> list[str]:
    explicit = [str(value) for value in payload.get("adjacent_role_ids") or [] if value]
    if explicit:
        return list(dict.fromkeys(explicit))
    role_ids = [str(value) for value in payload.get("role_ids") or [] if value]
    return adjacent_role_ids_for(role_ids) if role_ids else []


def merge_adjacency_queries(llm_queries: list[str], static_queries: list[str]) -> tuple[list[str], str]:
    if not llm_queries:
        return list(static_queries), "static_map"
    seen: set[str] = set()
    merged: list[str] = []
    for query in [*llm_queries, *static_queries]:
        key = str(query).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(str(query).strip())
    return merged, "llm+static"


def is_non_operational_title(title: str) -> bool:
    text = str(title or "").strip().lower()
    if not text or not NON_OPERATIONAL_PATTERN.search(text):
        return False
    return not bool(RESCUE_OPERATIONAL_PATTERN.search(text))


async def filter_only_rows_for_namespace(
    logical_name: str,
    filters: tuple,
    include_attributes: list[str],
    *,
    page_size: int = 10000,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    if is_local_backend() and logical_name in LOCAL_BACKEND_NAMESPACES:
        return await asyncio.to_thread(
            local_store().filter_only_rows_for_namespace,
            logical_name,
            filters,
            include_attributes,
            page_size,
            max_results,
        )

    ns = namespace(logical_name)
    page_size = min(page_size, 10000)
    max_batches = int(os.getenv("TURBOPUFFER_FILTER_ONLY_MAX_BATCHES", "100"))
    all_rows: list[dict[str, Any]] = []
    last_id: str | None = None
    batch_count = 0

    while True:
        paginated = filters
        if last_id is not None:
            paginated = ("And", list(filters[1]) + [("id", "Gt", last_id)]) if filters[0] == "And" else ("And", [filters, ("id", "Gt", last_id)])

        def run_query() -> Any:
            return ns.query(
                rank_by=["id", "asc"],
                filters=paginated,
                top_k=page_size,
                include_attributes=include_attributes,
                consistency=STRONG_CONSISTENCY,
            )

        response = await asyncio.to_thread(run_query)
        batch_count += 1
        if not response or not response.rows:
            break
        for row in response.rows:
            all_rows.append(row_attrs(row, include_attributes))
        if max_results and len(all_rows) >= max_results:
            all_rows = all_rows[:max_results]
            break
        if len(response.rows) < page_size:
            break
        last_id = str(response.rows[-1].id)
        if batch_count >= max_batches:
            break
    return all_rows


async def filter_only_rows(filters: tuple, include_attributes: list[str], *, page_size: int = 10000, max_results: int = 0) -> list[dict[str, Any]]:
    return await filter_only_rows_for_namespace("people", filters, include_attributes, page_size=page_size, max_results=max_results)


def extract_base_ids(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for row in rows:
        raw_id = row.get("person_id") or row.get("base_id") or row.get("id")
        if not raw_id:
            continue
        person_id = base_person_id(str(raw_id))
        if person_id in seen:
            continue
        seen.add(person_id)
        result.append(person_id)
    return result


def reciprocal_rank_fusion(result_lists: list[list[Any]], weights: list[float]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for rows, weight in zip(result_lists, weights):
        for rank, row in enumerate(rows, start=1):
            scores[str(row.id)] = scores.get(str(row.id), 0.0) + weight / (K_RRF + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index:index + size] for index in range(0, len(values), max(1, size))]


def should_batch_base_ids(payload: dict[str, Any]) -> bool:
    ids = payload.get("base_candidate_ids") or []
    return len(ids) >= BASE_ID_BATCH_MIN


def strip_base_candidate_filter(expr: Any) -> Any:
    """Remove base_id filters from a TurboPuffer filter tuple.

    Batched retrieval injects a per-batch base_id filter. This removes the large
    original base_id clause so each query only carries one small batch filter.
    """
    if expr is None:
        return None
    if isinstance(expr, tuple):
        expr = list(expr)
    if not isinstance(expr, list) or not expr:
        return expr
    if expr[0] in {"And", "Or"}:
        clauses = [strip_base_candidate_filter(clause) for clause in (expr[1] or [])]
        clauses = [clause for clause in clauses if clause is not None]
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else (expr[0], clauses)
    if len(expr) >= 3 and expr[0] == "base_id":
        return None
    return tuple(expr)


def and_filters(*clauses: Any) -> tuple | None:
    kept = [clause for clause in clauses if clause is not None]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else ("And", kept)


def merge_ranked_rows(row_batches: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for batch_index, rows in enumerate(row_batches):
        for rank, row in enumerate(rows, start=1):
            doc_id = str(row.get("position_id") or row.get("id") or "")
            if not doc_id:
                continue
            existing = by_id.get(doc_id)
            score = float(row.get("score") or 0.0)
            if existing is None or score > float(existing.get("score") or 0.0):
                merged = dict(row)
                merged["batch_index"] = batch_index
                merged["batch_rank"] = rank
                by_id[doc_id] = merged
    return sorted(by_id.values(), key=lambda row: float(row.get("score") or 0.0), reverse=True)


def is_filter_only_payload(payload: dict[str, Any]) -> bool:
    semantic_query = str(payload.get("semantic_query") or "").strip()
    return len(semantic_query) < 80


async def _filter_only_role_rows(filters: tuple | None, *, top_k: int, include_attributes: list[str]) -> list[dict[str, Any]]:
    if filters is None:
        raise ValueError("filter-only search requires at least one TurboPuffer filter")
    if is_local_backend():
        rows = await asyncio.to_thread(
            local_store().filter_only_rows_for_namespace,
            "people",
            filters,
            include_attributes,
            10000,
            0,
        )
    else:
        rows = await filter_only_rows(filters, include_attributes, max_results=0)
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        doc_id = str(row.get("id") or "")
        if not doc_id:
            continue
        row = dict(row)
        row["score"] = 1.0
        row["person_id"] = row.get("base_id") or base_person_id(doc_id)
        row["position_id"] = doc_id
        row["retrieval_mode"] = "filter_only"
        row["filter_rank"] = index
        out.append(row)
    return out


async def bm25_adjacency_rows(
    queries: list[str],
    filters: tuple | None,
    *,
    top_k: int = ADJACENCY_LIMIT,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    """BM25-only title adjacency retrieval for company-domain UNION mode."""
    if is_local_backend():
        return await asyncio.to_thread(
            local_store().bm25_adjacency_rows,
            queries,
            filters,
            top_k,
            include_attributes,
        )

    prepared: list[tuple[str, list[str]]] = []
    for query in queries:
        tokens = phrase_query_tokenize(str(query))
        if tokens:
            prepared.append((str(query), tokens))
    if not prepared:
        return []

    tp_queries = [
        {
            "rank_by": ("phrase_tokens", "BM25", tokens),
            "top_k": top_k,
            "include_attributes": include_attributes,
            "filters": filters,
        }
        for _, tokens in prepared
    ]
    ns = namespace("people")

    def run_multi_query() -> Any:
        return ns.multi_query(queries=tp_queries, consistency=STRONG_CONSISTENCY)

    response = await asyncio.to_thread(run_multi_query)
    result_sets = response.results or []
    result_lists = [result_set.rows or [] for result_set in result_sets]
    fused = reciprocal_rank_fusion(result_lists, [1.0] * len(result_lists))

    attrs: dict[str, dict[str, Any]] = {}
    for query_index, result_set in enumerate(result_sets):
        for row in result_set.rows or []:
            doc_id = str(row.id)
            item = attrs.setdefault(doc_id, row_attrs(row, include_attributes))
            item.setdefault("adjacency_query_indexes", []).append(query_index)

    rows: list[dict[str, Any]] = []
    for rank, (doc_id, score) in enumerate(fused[:top_k], start=1):
        row = dict(attrs.get(doc_id) or {"id": doc_id})
        row["score"] = score
        row["person_id"] = row.get("base_id") or base_person_id(doc_id)
        row["position_id"] = doc_id
        row["retrieval_mode"] = "company_adjacency_bm25"
        row["company_adjacency_rank"] = rank
        rows.append(row)
    return rows


async def _hybrid_role_rows_single(
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
    query_embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    if is_local_backend():
        local_payload = dict(payload)
        if query_embedding is not None:
            local_payload["query_embedding"] = query_embedding
            embedding_fn = None
        else:
            embedding_fn = embedding
        return await local_store().hybrid_role_rows(
            local_payload,
            filters,
            top_k,
            include_attributes,
            embedding_fn=embedding_fn,
        )

    semantic_query = str(payload.get("semantic_query") or "").strip()
    bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
    query_embedding = query_embedding or await embedding(semantic_query)
    per_field = bm25_queries_per_field(bm25_queries)

    queries: list[dict[str, Any]] = []
    weights: list[float] = []
    for field, tokens, weight in [
        ("phrase_tokens", per_field.get("phrase_tokens"), 1.5),
        ("word_tokens", per_field.get("word_tokens"), 1.0),
    ]:
        if tokens:
            queries.append({
                "rank_by": (field, "BM25", tokens),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters,
            })
            weights.append(weight)
    queries.append({
        "rank_by": ("vector", "kNN", query_embedding),
        "top_k": top_k,
        "include_attributes": include_attributes,
        "filters": filters if filters is not None else ("id", "NotEq", "__impossible__"),
    })
    weights.append(0.6)

    ns = namespace("people")

    def run_multi_query() -> Any:
        return ns.multi_query(queries=queries, consistency=STRONG_CONSISTENCY)

    response = await asyncio.to_thread(run_multi_query)
    result_sets = response.results or []
    result_lists = [result_set.rows or [] for result_set in result_sets]
    fused = reciprocal_rank_fusion(result_lists, weights[: len(result_lists)])

    attrs: dict[str, dict[str, Any]] = {}
    for result_set in result_sets:
        for row in result_set.rows or []:
            attrs.setdefault(str(row.id), row_attrs(row, include_attributes))

    rows: list[dict[str, Any]] = []
    for doc_id, score in fused:
        row = dict(attrs.get(doc_id) or {"id": doc_id})
        row["score"] = score
        row["person_id"] = row.get("base_id") or base_person_id(doc_id)
        row["position_id"] = doc_id
        rows.append(row)
    return rows


async def _batched_base_id_rows(
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    """Run retrieval in base_id batches, following network-search-api V3 parity.

    Reference: `RoleSearchVerticalV3` in `../network-search-api` batches large
    base_candidate_ids into 500-ID chunks so TurboPuffer kNN/BM25 does not carry
    one huge `base_id In [...]` filter. Company-id batching happens earlier in
    `apply_prefilters.company_base_ids`, mirroring `CompanyPeoplePreFilterStage`.
    """
    base_ids = [str(value) for value in payload.get("base_candidate_ids") or [] if value]
    batch_values = chunks(base_ids, BASE_ID_BATCH_SIZE)
    base_filter = strip_base_candidate_filter(filters)
    filter_only = is_filter_only_payload(payload)
    query_embedding = None if filter_only else await embedding(str(payload.get("semantic_query") or "").strip())
    semaphore = asyncio.Semaphore(max(1, BASE_ID_BATCH_CONCURRENCY))

    async def run_batch(batch_index: int, batch: list[str]) -> list[dict[str, Any]]:
        async with semaphore:
            batch_filters = and_filters(base_filter, comparison("base_id", "In", batch))
            if filter_only:
                rows = await _filter_only_role_rows(batch_filters, top_k=top_k, include_attributes=include_attributes)
            else:
                rows = await _hybrid_role_rows_single(
                    payload,
                    batch_filters,
                    top_k=top_k,
                    include_attributes=include_attributes,
                    query_embedding=query_embedding,
                )
            for row in rows:
                row["base_id_batch_index"] = batch_index
            return rows

    row_batches = await asyncio.gather(*(run_batch(index, batch) for index, batch in enumerate(batch_values)))
    rows = merge_ranked_rows(row_batches)
    for row in rows:
        row["retrieval_batched_base_ids"] = True
        row["base_id_batch_size"] = BASE_ID_BATCH_SIZE
        row["base_id_batch_count"] = len(batch_values)
    return rows


async def hybrid_role_rows(
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    if should_batch_base_ids(payload):
        return await _batched_base_id_rows(payload, filters, top_k=top_k, include_attributes=include_attributes)
    if is_filter_only_payload(payload):
        return await _filter_only_role_rows(filters, top_k=top_k, include_attributes=include_attributes)
    return await _hybrid_role_rows_single(payload, filters, top_k=top_k, include_attributes=include_attributes)


def dedupe_people(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Collapse position rows to unique people.

    limit <= 0 means keep the full retrieved frontier. This is the Powerpacks
    default so agents can save/query local artifacts instead of truncating data
    for chat display.
    """
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        person_id = str(row.get("person_id") or row.get("base_id") or base_person_id(str(row.get("id"))))
        if person_id in seen:
            continue
        seen.add(person_id)
        vertical_sources = list(row.get("vertical_sources") or [])
        retrieval_mode = row.get("retrieval_mode")
        if retrieval_mode and retrieval_mode not in vertical_sources:
            vertical_sources.append(str(retrieval_mode))
        candidate = {
            "person_id": person_id,
            "position_id": row.get("position_id") or row.get("id"),
            "score": row.get("score"),
            "position_title": row.get("position_title"),
            "city": row.get("city"),
            "state": row.get("state"),
            "role_track": row.get("role_track"),
            "seniority_band": row.get("seniority_band"),
            "company_id": row.get("company_id"),
            "is_current": row.get("is_current"),
            "vertical_sources": vertical_sources,
            "matched_position_ids": [row.get("position_id") or row.get("id")] if (row.get("position_id") or row.get("id")) else [],
        }
        if row.get("retrieval_batched_base_ids"):
            candidate["retrieval_batched_base_ids"] = True
            candidate["base_id_batch_count"] = row.get("base_id_batch_count")
            candidate["base_id_batch_size"] = row.get("base_id_batch_size")
        candidates.append(candidate)
        if limit and limit > 0 and len(candidates) >= limit:
            break
    return candidates


def merge_company_union_candidates(
    candidates: list[dict[str, Any]],
    union_candidates: list[Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not union_candidates:
        return candidates
    merged = [dict(candidate) for candidate in candidates]
    by_person = {str(candidate.get("person_id")): candidate for candidate in merged if candidate.get("person_id")}
    for rank, raw in enumerate(union_candidates, start=1):
        item = raw if isinstance(raw, dict) else {"person_id": raw}
        person_id = base_person_id(str(item.get("person_id") or item.get("base_id") or item.get("id") or ""))
        if not person_id:
            continue
        existing = by_person.get(person_id)
        if existing is not None:
            sources = list(existing.get("vertical_sources") or [])
            if "company_filter" not in sources:
                sources.append("company_filter")
            existing["vertical_sources"] = sources
            existing.setdefault("company_union_rank", rank)
            if item.get("position_id") or item.get("id"):
                matched = list(existing.get("matched_position_ids") or [])
                position_id = item.get("position_id") or item.get("id")
                if position_id and position_id not in matched:
                    matched.append(position_id)
                existing["matched_position_ids"] = matched
            continue
        candidate = {
            "person_id": person_id,
            "position_id": item.get("position_id") or item.get("id"),
            "score": item.get("score"),
            "position_title": item.get("position_title"),
            "city": item.get("city"),
            "state": item.get("state"),
            "role_track": item.get("role_track"),
            "seniority_band": item.get("seniority_band"),
            "company_id": item.get("company_id"),
            "is_current": item.get("is_current"),
            "vertical_sources": ["company_filter"],
            "company_union_rank": rank,
        }
        if candidate.get("position_id"):
            candidate["matched_position_ids"] = [candidate["position_id"]]
        merged.append(candidate)
        by_person[person_id] = candidate
        if limit and limit > 0 and len(merged) >= limit:
            break
    return merged
