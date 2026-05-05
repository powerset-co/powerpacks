"""Standalone TurboPuffer client for Powerpacks primitives.

This module uses only checked-in Powerpacks contracts plus external packages
loaded through uv when needed.
"""

from __future__ import annotations

import asyncio
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


def namespace(logical_name: str = "people") -> Any:
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


def filters_from_role_payload(payload: dict[str, Any]) -> tuple | None:
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
    if payload.get("company_ids"):
        filters.append(comparison("company_id", "In", payload["company_ids"]))
    if payload.get("is_current") is not None:
        filters.append(comparison("is_current", "Eq", bool(payload["is_current"])))
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
    if isinstance(prefilters, dict) and prefilters.get("base_candidate_ids") is not None:
        payload["base_candidate_ids"] = list(dict.fromkeys(str(pid) for pid in prefilters["base_candidate_ids"] if pid))

    resolved_set = latest_step_output(state, "resolve_set_operators")
    if isinstance(resolved_set, dict) and resolved_set.get("operator_ids"):
        payload["operator_ids"] = list(dict.fromkeys(str(oid) for oid in resolved_set["operator_ids"] if oid))
        if resolved_set.get("set_id") and not payload.get("set_id"):
            payload["set_id"] = str(resolved_set["set_id"])

    return payload


async def filter_only_rows_for_namespace(
    logical_name: str,
    filters: tuple,
    include_attributes: list[str],
    *,
    page_size: int = 10000,
    max_results: int = 0,
) -> list[dict[str, Any]]:
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


async def hybrid_role_rows(
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    semantic_query = str(payload.get("semantic_query") or "").strip()
    if len(semantic_query) < 80:
        raise ValueError("semantic_query must be dense retrieval prose of at least 80 characters")
    bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
    query_embedding = await embedding(semantic_query)
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


def dedupe_people(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        person_id = str(row.get("person_id") or row.get("base_id") or base_person_id(str(row.get("id"))))
        if person_id in seen:
            continue
        seen.add(person_id)
        candidates.append({
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
        })
        if len(candidates) >= limit:
            break
    return candidates
