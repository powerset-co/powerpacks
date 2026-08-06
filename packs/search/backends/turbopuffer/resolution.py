"""TurboPuffer-owned source resolution."""

from __future__ import annotations

import asyncio
from typing import Any

from ...primitives.turbopuffer.turbopuffer_search_backend import (
    STRONG_CONSISTENCY,
    comparison,
    namespace,
    row_attrs,
)


_ATTRIBUTES = (
    "investor_name",
    "canonical_name",
    "investor_type",
    "investment_count",
    "canonical_urn",
)


async def resolve_turbopuffer_investors(
    names: list[str], *, allowed_operator_ids: list[str], top_k: int
) -> list[dict[str, Any]]:
    """Resolve investor names to canonical identities within an explicit scope."""
    if not names:
        return []
    operator_ids = list(
        dict.fromkeys(str(value) for value in allowed_operator_ids if str(value))
    )
    if not operator_ids:
        raise ValueError("investor resolution requires explicit allowed_operator_ids")

    ns = namespace("investors")
    resolved: list[dict[str, Any]] = []
    for name in names:
        matched_rows: list[Any] = []
        match_type = "exact"
        for field, operator in (
            ("investor_name", "Eq"),
            ("investor_name_tokens", "ContainsAllTokens"),
        ):
            filters = (
                "And",
                [
                    comparison("allowed_operator_ids", "ContainsAny", operator_ids),
                    comparison(field, operator, name),
                ],
            )

            def query() -> Any:
                return ns.query(
                    filters=filters,
                    rank_by=["investment_count", "desc"],
                    top_k=max(top_k, 10),
                    include_attributes=list(_ATTRIBUTES),
                    consistency=STRONG_CONSISTENCY,
                )

            response = await asyncio.to_thread(query)
            matched_rows = list(response.rows or [])
            if matched_rows:
                break
            match_type = "alias"

        ranked = [row_attrs(row, list(_ATTRIBUTES)) for row in matched_rows]
        ranked.sort(key=lambda row: int(row.get("investment_count") or 0), reverse=True)
        for row in ranked[:top_k]:
            canonical_name = str(row.get("canonical_name") or row.get("investor_name") or name)
            row["query_name"] = name
            row["urn"] = row.get("canonical_urn") or row.get("id")
            row["match_type"] = (
                "alias"
                if match_type == "alias" or canonical_name.casefold() != name.casefold()
                else "exact"
            )
            resolved.append(row)
    return resolved
