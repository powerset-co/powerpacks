"""Local DuckDB-only verticals used by search execution.

The remote TurboPuffer primitive historically searched the people/role namespace
only.  These helpers make the added local summary and company-signal pools
explicitly local instead of hiding them in the TurboPuffer client module.
"""

from __future__ import annotations

import asyncio
from typing import Any

from local_search_backend import is_local_backend, local_namespace_has_vectors, local_store
from search_embeddings import embedding


async def summary_search_rows(
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    if not is_local_backend():
        return []
    local_payload = dict(payload)
    semantic_query = str(payload.get("semantic_query") or "").strip()
    if semantic_query and local_payload.get("query_embedding") is None and local_namespace_has_vectors("summaries"):
        local_payload["query_embedding"] = await embedding(semantic_query)
    return await asyncio.to_thread(
        local_store().summary_search_rows,
        local_payload,
        filters,
        top_k,
        include_attributes,
    )


async def company_signal_rows(
    payload: dict[str, Any],
    filters: tuple | None,
    *,
    top_k: int,
    include_attributes: list[str],
) -> list[dict[str, Any]]:
    if not is_local_backend():
        return []
    local_payload = dict(payload)
    semantic_query = str(payload.get("semantic_query") or "").strip()
    if semantic_query and local_payload.get("query_embedding") is None and local_namespace_has_vectors("company_signals"):
        local_payload["query_embedding"] = await embedding(semantic_query)
    return await asyncio.to_thread(
        local_store().company_signal_rows,
        local_payload,
        filters,
        top_k,
        include_attributes,
    )
