"""Explicit local DuckDB backend adapter for local-only search tooling."""

from __future__ import annotations

import functools
import threading
from pathlib import Path
from typing import Any

LOCAL_BACKEND_TABLES = {
    "people": "local_people_positions",
    "summaries": "local_summaries",
    "company_signals": "local_company_signals",
    "education": "local_people_education",
    "schools": "local_education",
    "companies": "local_companies",
}
LOCAL_BACKEND_NAMESPACES = set(LOCAL_BACKEND_TABLES)


_STORE_LOCK = threading.Lock()


@functools.lru_cache(maxsize=None)
def _local_store_for_path(path: str) -> Any:
    from local_duckdb_store import LocalDuckDBSearchStore

    return LocalDuckDBSearchStore(path)


def local_store(db_path: str | Path) -> Any:
    """Return a per-call store forked from one shared root connection.

    A DuckDB connection must not execute queries concurrently from multiple
    threads. The chunked prefilter paths fan out via asyncio.to_thread, and
    handing every caller the same cached connection corrupted catalog reads
    under load (PRAGMA table_info coming back empty, _table_exists reporting
    existing tables as missing). Each call therefore gets a cursor fork of
    the cached root, which is DuckDB's sanctioned per-thread pattern.
    """
    with _STORE_LOCK:
        return _local_store_for_path(str(db_path)).fork()


def local_namespace_has_vectors(db_path: str | Path, logical_name: str, field: str = "vector") -> bool:
    if logical_name not in LOCAL_BACKEND_NAMESPACES:
        return False
    try:
        return bool(local_store(db_path).has_nonempty_vectors(logical_name, field))
    except Exception:
        return False


def local_namespace_exists(db_path: str | Path, logical_name: str) -> bool:
    if logical_name not in LOCAL_BACKEND_NAMESPACES:
        return False
    try:
        return bool(local_store(db_path).namespace_exists(logical_name))
    except Exception:
        return False


def local_namespace_row_count(db_path: str | Path, logical_name: str) -> int:
    if logical_name not in LOCAL_BACKEND_NAMESPACES:
        return 0
    try:
        return int(local_store(db_path).namespace_row_count(logical_name))
    except Exception:
        return 0


def namespace_name(logical_name: str = "people") -> str:
    return LOCAL_BACKEND_TABLES[logical_name]


def namespace(db_path: str | Path, logical_name: str = "people") -> Any:
    return local_store(db_path).namespace(logical_name)


async def filter_only_rows_for_namespace(
    db_path: str | Path,
    logical_name: str,
    filters: tuple,
    include_attributes: list[str],
    *,
    page_size: int = 10000,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    import asyncio

    return await asyncio.to_thread(
        local_store(db_path).filter_only_rows_for_namespace,
        logical_name,
        filters,
        include_attributes,
        page_size,
        max_results,
    )


async def filter_only_rows(
    db_path: str | Path,
    filters: tuple,
    include_attributes: list[str],
    *,
    page_size: int = 10000,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    return await filter_only_rows_for_namespace(
        db_path,
        "people",
        filters,
        include_attributes,
        page_size=page_size,
        max_results=max_results,
    )


async def bm25_adjacency_rows(
    db_path: str | Path,
    queries: list[str],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    import asyncio

    return await asyncio.to_thread(
        local_store(db_path).bm25_adjacency_rows,
        queries,
        filters,
        top_k,
        include_attributes,
    )


async def _filter_only_role_rows(db_path: str | Path, filters: tuple | None, *, top_k: int, include_attributes: list[str]) -> list[dict[str, Any]]:
    if filters is None:
        raise ValueError("filter-only search requires at least one local DuckDB filter")
    from search_result_merge import base_person_id

    max_results = top_k if top_k and top_k > 0 else 0
    rows = await filter_only_rows(db_path, filters, include_attributes, max_results=max_results)
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        doc_id = str(row.get("id") or "")
        if not doc_id:
            continue
        item = dict(row)
        item["score"] = 1.0
        item["person_id"] = item.get("base_id") or base_person_id(doc_id)
        item["position_id"] = item.get("position_id") or doc_id
        item["retrieval_mode"] = "filter_only"
        item["filter_rank"] = index
        out.append(item)
    return out


async def _hybrid_role_rows_single(
    db_path: str | Path,
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
    query_embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    from search_embeddings import embedding

    local_payload = dict(payload)
    semantic_query = str(payload.get("semantic_query") or "").strip()
    if query_embedding is None and semantic_query:
        query_embedding = await embedding(semantic_query)
    if query_embedding is not None:
        local_payload["query_embedding"] = query_embedding
    return await local_store(db_path).hybrid_role_rows(local_payload, filters, top_k, include_attributes)


async def _batched_base_id_rows(
    db_path: str | Path,
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    import asyncio

    from search_common import (
        BASE_ID_BATCH_CONCURRENCY,
        BASE_ID_BATCH_SIZE,
        and_filters,
        chunks,
        comparison,
        is_filter_only_payload,
        merge_ranked_rows,
        strip_base_candidate_filter,
    )
    from search_embeddings import embedding

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
                rows = await _filter_only_role_rows(db_path, batch_filters, top_k=top_k, include_attributes=include_attributes)
            else:
                rows = await _hybrid_role_rows_single(
                    db_path,
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
    db_path: str | Path,
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    from search_common import is_filter_only_payload, should_batch_base_ids

    if should_batch_base_ids(payload):
        return await _batched_base_id_rows(db_path, payload, filters, top_k=top_k, include_attributes=include_attributes)
    if is_filter_only_payload(payload):
        return await _filter_only_role_rows(db_path, filters, top_k=top_k, include_attributes=include_attributes)
    return await _hybrid_role_rows_single(db_path, payload, filters, top_k=top_k, include_attributes=include_attributes)
