"""Backend-neutral search helpers shared by local DuckDB and TurboPuffer pipelines."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import search_backend_mode
import search_result_merge
from powerpacks_contracts import TURBOPUFFER_FILTER_OPERATORS


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
ROLE_ADJACENCY_MAP_PATH = Path(__file__).resolve().parents[2] / "data" / "roles" / "role_adjacency.opus.json"
_ROLE_ADJACENCY_MAP: dict[str, list[str]] | None = None
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


def ensure_snowballstemmer() -> None:
    try:
        __import__("snowballstemmer")
        return
    except ModuleNotFoundError:
        raise RuntimeError("Missing required package: snowballstemmer. Run bin/setup-python.")


def stemmer() -> Any:
    ensure_snowballstemmer()
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


def _trait_value(trait: Any, key: str) -> str:
    if isinstance(trait, dict):
        return str(trait.get(key) or "").strip().lower()
    return str(getattr(trait, key, "") or "").strip().lower()


def _temporal_from_traits(traits: list[Any], meaning: str) -> bool | None:
    matching = [trait for trait in traits if _trait_value(trait, "meaning") == meaning]
    if not matching:
        return None
    non_all = {_trait_value(trait, "temporal") for trait in matching if _trait_value(trait, "temporal") != "all"}
    if non_all == {"current"}:
        return True
    if non_all == {"past"}:
        return False
    return None


def _single_trait_temporal(traits: list[Any]) -> bool | None:
    if len(traits) != 1:
        return None
    temporal = _trait_value(traits[0], "temporal")
    if temporal == "current":
        return True
    if temporal == "past":
        return False
    return None


def _derive_role_current_from_traits(traits: list[Any]) -> bool | None:
    single = _single_trait_temporal(traits)
    if single is not None:
        return single
    role_temporal = _temporal_from_traits(traits, "role")
    if role_temporal is not None:
        return role_temporal
    company_temporal = _temporal_from_traits(traits, "company")
    role_and_company = [trait for trait in traits if _trait_value(trait, "meaning") in {"role", "company"}]
    if company_temporal is not None and len(role_and_company) == len([trait for trait in traits if _trait_value(trait, "meaning") == "company"]):
        return company_temporal
    return None


def _derive_company_current_from_traits(traits: list[Any]) -> bool | None:
    single = _single_trait_temporal(traits)
    if single is not None:
        return single
    company_temporal = _temporal_from_traits(traits, "company")
    if company_temporal is not None:
        return company_temporal
    role_temporal = _temporal_from_traits(traits, "role")
    role_and_company = [trait for trait in traits if _trait_value(trait, "meaning") in {"role", "company"}]
    if role_temporal is not None and len(role_and_company) == len([trait for trait in traits if _trait_value(trait, "meaning") == "role"]):
        return role_temporal
    return None


def apply_trait_currentness(filters: dict[str, Any], traits: Any) -> dict[str, Any]:
    """Derive is_current_role / is_current_company from trait temporals.

    Trait temporals are the extractor's source of truth for currentness: when
    traits exist they replace any extractor-emitted is_current_* flags (the
    same semantics role_payload_from_state has always applied on the state
    path). Returns a new dict; *filters* is not mutated.
    """
    out = dict(filters)
    if not isinstance(traits, list) or not traits:
        return out
    out.pop("is_current_role", None)
    out.pop("is_current_company", None)
    role_current = _derive_role_current_from_traits(traits)
    company_current = _derive_company_current_from_traits(traits)
    if role_current is not None:
        out["is_current_role"] = role_current
    if company_current is not None:
        out["is_current_company"] = company_current
    return out


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
    if search_backend_mode.is_local_backend_configured():
        return []

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
    # Local title-cluster keywords are corpus-derived position titles, not
    # operator role intent. network-search-api detects founder/c-suite
    # shortcuts from the raw query before title clustering, so clustered
    # titles never feed shortcut detection there. Mirror that: a clustered
    # title like "Founder & CEO (...)" in bm25_queries must not flip a
    # software-engineer query into a hard founder/c-suite role_ids filter.
    cluster_keywords = {
        str(value).strip().lower()
        for value in payload.get("local_title_cluster_keywords") or []
        if str(value).strip()
    }
    for value in payload.get("bm25_queries") or []:
        text = str(value)
        if text.strip().lower() in cluster_keywords:
            continue
        parts.append(text)
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
    # Role intent is owned by query extraction: network-search-api takes
    # role_ids only from LLM extraction and never re-detects roles from
    # keyword text, so a free-text fallback here can misread corpus-derived
    # keywords (e.g. a clustered "Founder & CEO (...)" title) as operator
    # intent. Canonical role_ids are the only founder signal.
    role_ids = {str(value).lower() for value in payload.get("role_ids") or []}
    return bool(role_ids & {"founder", "cofounder", "co-founder"})


def detect_csuite_shortcut(payload: dict[str, Any], query: str | None = None) -> dict[str, Any] | None:
    # Like the founder shortcut, c-suite intent is owned by the user's query:
    # scanning payload text (bm25 aliases, clustered titles) or
    # extraction-emitted role_ids misreads leaders-style queries (e.g. "sales
    # leaders" bm25 contains CRO aliases) as explicit c-suite asks. Deployed
    # network-search-api only role-gates when the query names the role.
    text = str(query or "").lower()
    if not text:
        return None
    words = set(TOKEN_RE.findall(text))
    for abbrev, spec in CSUITE_SHORTCUTS.items():
        if abbrev in words or f"{abbrev}s" in words or str(spec["display"]).lower() in text:
            return spec
    return None


def shortcut_role_id_filter(payload: dict[str, Any]) -> list[str]:
    # Deployed network-search-api expand rarely emits role_ids, so prod's hard
    # role_ids filter effectively only ever carries founder/c-suite shortcut
    # roles. Precise role_ids from role_agent_v2-style extraction stay in the
    # payload for shortcut detection and title clustering, but only shortcut
    # roles become a hard retrieval filter. The company-union adjacency stage
    # deliberately filters by adjacent role_ids and opts back in via
    # role_ids_hard_filter.
    role_ids = payload.get("role_ids") or []
    if payload.get("role_ids_hard_filter"):
        return list(role_ids)
    shortcut_ids = {"founder", "cofounder", "co-founder"}
    # C-suite roles only gate retrieval when the user's query explicitly named
    # the role (apply_role_shortcuts sets the flag); extraction freely adds
    # c-suite role_ids to leaders-style queries where a hard gate is wrong.
    csuite_id = str(payload.get("csuite_shortcut_role_id") or "").lower()
    if csuite_id:
        shortcut_ids.add(csuite_id)
    return [rid for rid in role_ids if str(rid).lower() in shortcut_ids]


def apply_role_shortcuts(payload: dict[str, Any], query: str | None = None) -> dict[str, Any]:
    payload = dict(payload)
    if detects_founder_shortcut(payload, query):
        payload["role_ids"] = _dedupe_strings([*(payload.get("role_ids") or []), "founder"])
        payload["bm25_queries"] = _dedupe_strings([*(payload.get("bm25_queries") or []), *FOUNDER_BM25_QUERIES])
        if len(str(payload.get("semantic_query") or "")) < 80:
            payload["semantic_query"] = FOUNDER_SEMANTIC_QUERY
        # Founder exists at all seniority levels; copying c-suite/owner bands hurts recall.
        # Exception: bands pinned via --seniority-bands are an explicit JD-level
        # hard constraint and must survive role shortcuts.
        if not payload.get("seniority_bands_pinned"):
            payload.pop("seniority_bands", None)
        return payload

    csuite = detect_csuite_shortcut(payload, query)
    if csuite:
        payload["role_ids"] = _dedupe_strings([*(payload.get("role_ids") or []), csuite["role_id"]])
        payload["csuite_shortcut_role_id"] = csuite["role_id"]
        payload["bm25_queries"] = _dedupe_strings([*(payload.get("bm25_queries") or []), *csuite["bm25"]])
        if not payload.get("seniority_bands"):
            payload["seniority_bands"] = ["c-suite"]
        if len(str(payload.get("semantic_query") or "")) < 80:
            payload["semantic_query"] = (
                f"Executive leader serving as {csuite['display']}, responsible for strategic direction, "
                "organizational leadership, senior decision-making, cross-functional execution, and accountability "
                "for company or department outcomes. Profile evidence should include a current or past C-suite title."
            )
    return payload


def location_filter_from_payload(payload: dict[str, Any], mapping: list[tuple[str, str, str]]) -> tuple | None:
    clauses: list[tuple] = []
    for payload_key, field, op in mapping:
        values = payload.get(payload_key)
        if values:
            clauses.append(comparison(field, op, values))
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else ("Or", clauses)


def filters_from_role_payload(payload: dict[str, Any]) -> tuple | None:
    payload = apply_role_shortcuts(payload)
    hard = filter_expression_to_tuple(payload.get("hard_filters"))
    if hard is not None:
        return hard

    filters: list[tuple] = []
    location_filter = location_filter_from_payload(payload, [
        ("cities", "city", "In"),
        ("states", "state", "In"),
        ("countries", "country", "In"),
        ("macro_regions", "macro_region", "In"),
        ("metro_areas", "metro_areas", "ContainsAny"),
    ])
    if location_filter is not None:
        filters.append(location_filter)

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
    shortcut_role_ids = shortcut_role_id_filter(payload)
    if shortcut_role_ids:
        filters.append(comparison("role_ids", "ContainsAny", shortcut_role_ids))
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
    traits = expansion.get("traits") if isinstance(expansion, dict) else []
    payload = apply_trait_currentness(payload, traits)

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
        "technology_types",
        "customer_types",
        "customer_type",
        "company_locations",
        "company_cities",
        "company_states",
        "company_countries",
        "company_metro_areas",
        "company_macro_regions",
        "funding_stage",
        "funding_stages",
        "funding_stage_min",
        "funding_stage_max",
        "funding_amount_min",
        "funding_amount_max",
        "headcount_min",
        "headcount_max",
        "employee_count_min",
        "employee_count_max",
        "valuation_min",
        "valuation_max",
        "founded_year_min",
        "founded_year_max",
        "last_funding_before",
        "last_funding_after",
        "yc_batches",
        "accelerators",
        "stages",
        "company_stages",
        "stage",
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
    # Accept both hyphen (canonical index/prod) and underscore (legacy) spellings.
    bands = {str(value).lower().replace("-", "_") for value in payload.get("seniority_bands") or []}
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


def extract_base_ids(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for row in rows:
        raw_id = row.get("person_id") or row.get("base_id") or row.get("id")
        if not raw_id:
            continue
        person_id = search_result_merge.base_person_id(str(raw_id))
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


def _strip_field_filter(expr: Any, field: str) -> Any:
    if expr is None:
        return None
    if isinstance(expr, tuple):
        expr = list(expr)
    if not isinstance(expr, list) or not expr:
        return expr
    if expr[0] in {"And", "Or"}:
        clauses = [_strip_field_filter(clause, field) for clause in (expr[1] or [])]
        clauses = [clause for clause in clauses if clause is not None]
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else (expr[0], clauses)
    if len(expr) >= 3 and expr[0] == field:
        return None
    return tuple(expr)


def strip_base_candidate_filter(expr: Any) -> Any:
    """Remove base_id filters from a TurboPuffer filter tuple.

    Batched retrieval injects a per-batch base_id filter. This removes the large
    original base_id clause so each query only carries one small batch filter.
    """
    return _strip_field_filter(expr, "base_id")


def strip_is_current_filter(expr: Any) -> Any:
    """Remove position-currentness clauses from a filter tuple.

    network-search-api's summary vertical is person-level (bio text), not
    position-level: its eligibility prefilter is built with is_current=None so
    a past position can qualify a person whose current role does not match
    (e.g. a co-founder with prior software-engineering positions). Mirror that
    contract for the local summary vertical.
    """
    return _strip_field_filter(expr, "is_current")


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
    bm25_queries = [str(query).strip() for query in payload.get("bm25_queries") or [] if str(query).strip()]
    return not semantic_query and not bm25_queries and payload.get("query_embedding") is None
