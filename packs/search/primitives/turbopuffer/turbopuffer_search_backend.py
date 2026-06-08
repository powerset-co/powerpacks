"""Standalone TurboPuffer backend for Powerpacks search primitives.

This module is remote-backend-only. It must not import local DuckDB modules.
Shared query/filter helpers live in search_common.py.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from powerpacks_contracts import TURBOPUFFER_NAMESPACES
import search_common as _search_common
from search_embeddings import embedding
from search_result_merge import base_person_id


ADJACENCY_LIMIT = _search_common.ADJACENCY_LIMIT
BASE_ID_BATCH_CONCURRENCY = _search_common.BASE_ID_BATCH_CONCURRENCY
BASE_ID_BATCH_MIN = _search_common.BASE_ID_BATCH_MIN
BASE_ID_BATCH_SIZE = _search_common.BASE_ID_BATCH_SIZE
K_RRF = _search_common.K_RRF
allowed_operator_ids_from_payload = _search_common.allowed_operator_ids_from_payload
adjacency_family_for_payload = _search_common.adjacency_family_for_payload
apply_role_shortcuts = _search_common.apply_role_shortcuts
bm25_queries_per_field = _search_common.bm25_queries_per_field
comparison = _search_common.comparison
company_filter_applies_to_role_search = _search_common.company_filter_applies_to_role_search
effective_adjacent_role_ids = _search_common.effective_adjacent_role_ids
filters_from_role_payload = _search_common.filters_from_role_payload
get_adjacency_queries = _search_common.get_adjacency_queries
has_company_constraint = _search_common.has_company_constraint
has_company_domain_intent = _search_common.has_company_domain_intent
has_role_constraint = _search_common.has_role_constraint
is_founder_payload = _search_common.is_founder_payload
is_non_operational_title = _search_common.is_non_operational_title
latest_step_output = _search_common.latest_step_output
load_env_file = _search_common.load_env_file
merge_adjacency_queries = _search_common.merge_adjacency_queries
phrase_query_tokenize = _search_common.phrase_query_tokenize
role_payload_from_state = _search_common.role_payload_from_state
row_attrs = _search_common.row_attrs
search_mode_for_payload = _search_common.search_mode_for_payload
seniority_intent = _search_common.seniority_intent
summarize_filter = _search_common.summarize_filter
validate_filter_tuple = _search_common.validate_filter_tuple
word_tokenize = _search_common.word_tokenize


STRONG_CONSISTENCY = {"level": "strong"}
DEFAULT_REGION = "gcp-us-central1"


def ensure_packages() -> None:
    try:
        __import__("turbopuffer")
        return
    except ModuleNotFoundError:
        raise RuntimeError("Missing required package: turbopuffer. Run bin/setup-python.")


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
    bm25_queries = [str(query).strip() for query in payload.get("bm25_queries") or [] if str(query).strip()]
    return not semantic_query and not bm25_queries and payload.get("query_embedding") is None


async def _filter_only_role_rows(filters: tuple | None, *, top_k: int, include_attributes: list[str]) -> list[dict[str, Any]]:
    if filters is None:
        raise ValueError("filter-only search requires at least one TurboPuffer filter")
    max_results = top_k if top_k and top_k > 0 else 0
    rows = await filter_only_rows(filters, include_attributes, max_results=max_results)
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        doc_id = str(row.get("id") or "")
        if not doc_id:
            continue
        row = dict(row)
        row["score"] = 1.0
        row["person_id"] = row.get("base_id") or base_person_id(doc_id)
        row["position_id"] = row.get("position_id") or doc_id
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
    semantic_query = str(payload.get("semantic_query") or "").strip()
    bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
    if query_embedding is None and semantic_query:
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
    if query_embedding is not None:
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
    semantic_query = str(payload.get("semantic_query") or "").strip()
    query_embedding = None if filter_only or not semantic_query else await embedding(semantic_query)
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
