#!/usr/bin/env python3
"""Resolve company names and company-attribute filters to company IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from turbopuffer_client import (  # noqa: E402
    STRONG_CONSISTENCY,
    allowed_operator_ids_from_payload,
    comparison,
    embedding,
    filter_only_rows_for_namespace,
    is_local_backend,
    load_env_file,
    local_namespace_has_vectors,
    namespace,
    namespace_name,
    reciprocal_rank_fusion,
    role_payload_from_state,
    row_attrs,
)


FUNDING_STAGE_MAP = {
    "pre_seed": 1,
    "pre-seed": 1,
    "seed": 2,
    "series_a": 3,
    "series a": 3,
    "series-a": 3,
    "series_b": 4,
    "series b": 4,
    "series-b": 4,
    "series_c": 5,
    "series c": 5,
    "series_d": 6,
    "series d": 6,
    "late_stage": 50,
    "ipo": 90,
    "public": 91,
    "exited": 99,
}

COMPANY_ALIASES = {
    "facebook": ["Facebook", "Meta"],
    "meta": ["Meta", "Facebook"],
    "twitter": ["Twitter", "X"],
    "x": ["X", "Twitter"],
    "google": ["Google", "Alphabet"],
    "alphabet": ["Alphabet", "Google"],
}

SECTOR_STRATEGIES = {"hard_filter", "soft_union", "semantic_only", "staged"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    log_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def normalize_stage(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return FUNDING_STAGE_MAP.get(str(value).strip().lower().replace("-", "_")) or FUNDING_STAGE_MAP.get(str(value).strip().lower())


def date_to_yyyymmdd(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 4 and text.isdigit():
        return int(f"{text}0101")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.strftime("%Y%m%d"))


def expanded_company_names(names: list[str]) -> list[str]:
    expanded: list[str] = []
    for name in names:
        key = name.strip().lower()
        expanded.extend(COMPANY_ALIASES.get(key, [name]))
    return list(dict.fromkeys(name for name in expanded if name))


def company_attribute_filters(payload: dict[str, Any], *, include_soft: bool = True, only_soft: bool = False) -> tuple | None:
    filters: list[tuple] = []
    mapping = [
        ("company_cities", "city", "In"),
        ("company_states", "state", "In"),
        ("company_countries", "country", "In"),
        ("company_metro_areas", "metro_area", "In"),
        ("company_macro_regions", "macro_region", "In"),
        ("entity_types", "entity_types", "ContainsAny"),
        ("technology_types", "technology_types", "ContainsAny"),
        ("customer_types", "customer_type", "ContainsAny"),
        ("investors", "investor_urns", "ContainsAny"),
        ("yc_batches", "yc_batches", "ContainsAny"),
    ]
    soft_mapping = [
        ("sector_types", "sector_types", "ContainsAny"),
    ]
    active_mapping = soft_mapping if only_soft else mapping + (soft_mapping if include_soft else [])
    for payload_key, field, op in active_mapping:
        values = payload.get(payload_key)
        if values:
            filters.append(comparison(field, op, values))

    operator_ids = allowed_operator_ids_from_payload(payload)
    if operator_ids:
        filters.append(comparison("allowed_operator_ids", "ContainsAny", operator_ids))

    if only_soft:
        return None if not filters else filters[0] if len(filters) == 1 else ("And", filters)

    for payload_key, field, op in [
        ("funding_amount_min", "funding_total", "Gte"),
        ("funding_amount_max", "funding_total", "Lte"),
        ("headcount_min", "headcount", "Gte"),
        ("headcount_max", "headcount", "Lte"),
        ("valuation_min", "valuation", "Gte"),
        ("valuation_max", "valuation", "Lte"),
        ("founded_year_min", "founded_year", "Gte"),
        ("founded_year_max", "founded_year", "Lte"),
    ]:
        if payload.get(payload_key) is not None:
            filters.append(comparison(field, op, payload[payload_key]))

    last_funding_after = date_to_yyyymmdd(payload.get("last_funding_after"))
    last_funding_before = date_to_yyyymmdd(payload.get("last_funding_before"))
    if last_funding_after is not None:
        filters.append(comparison("last_funding_at", "Gte", last_funding_after))
    if last_funding_before is not None:
        filters.append(comparison("last_funding_at", "Lte", last_funding_before))

    min_stage = normalize_stage(payload.get("funding_stage_min"))
    max_stage = normalize_stage(payload.get("funding_stage_max"))
    if min_stage is not None:
        filters.append(comparison("funding_stage", "Gte", min_stage))
    if max_stage is not None:
        filters.append(comparison("funding_stage", "Lte", max_stage))

    if not filters:
        return None
    return filters[0] if len(filters) == 1 else ("And", filters)


def combine_filters(*filters: tuple | None) -> tuple | None:
    active = [flt for flt in filters if flt is not None]
    if not active:
        return None
    return active[0] if len(active) == 1 else ("And", active)


def sector_strategy(payload: dict[str, Any], default: str) -> str:
    value = str(payload.get("company_sector_strategy") or default).strip().lower()
    if value not in SECTOR_STRATEGIES:
        raise ValueError(f"invalid company_sector_strategy: {value}")
    return value


def sector_min_results(payload: dict[str, Any], default: int) -> int:
    value = payload.get("company_sector_min_results", default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid company_sector_min_results: {value}") from exc
    return max(parsed, 0)


COMPANY_INCLUDE_ATTRIBUTES = [
    "company_urn",
    "company_name",
    "name_aliases_text",
    "semantic_text",
    "doc2query_text",
    "entity_sector_text",
    "headcount",
    "funding_stage",
    "funding_total",
    "sector_types",
    "entity_types",
    "city",
    "state",
    "country",
    "metro_area",
    "macro_region",
    "website_domain",
    "linkedin_url",
    "logo_url",
    "description",
]


def company_attrs(row: Any) -> dict[str, Any]:
    return row_attrs(row, COMPANY_INCLUDE_ATTRIBUTES)


async def exact_name_lookup(names: list[str], filters: tuple | None, *, top_k: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ns = namespace("companies")
    for name in names:
        name_filter = comparison("company_name", "Eq", name)
        query_filter = ("And", [filters, name_filter]) if filters else name_filter

        def run_query() -> Any:
            return ns.query(
                filters=query_filter,
                rank_by=["id", "asc"],
                top_k=top_k,
                include_attributes=COMPANY_INCLUDE_ATTRIBUTES,
                consistency=STRONG_CONSISTENCY,
            )

        response = await asyncio.to_thread(run_query)
        rows.extend(company_attrs(row) for row in (response.rows or []))
    return rows


async def name_bm25_lookup(names: list[str], filters: tuple | None, *, top_k: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ns = namespace("companies")
    for name in names:
        def run_query() -> Any:
            return ns.query(
                rank_by=("name_aliases_text", "BM25", name),
                filters=filters,
                top_k=top_k,
                include_attributes=COMPANY_INCLUDE_ATTRIBUTES,
                consistency=STRONG_CONSISTENCY,
            )

        response = await asyncio.to_thread(run_query)
        rows.extend(company_attrs(row) for row in (response.rows or []))
    return rows


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        cid = str(row.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        result.append(row)
    return result


async def semantic_lookup(queries: list[str], filters: tuple | None, *, top_k: int) -> list[dict[str, Any]]:
    query = " ".join(str(value).strip() for value in queries if str(value).strip())
    if not query:
        return []

    ns = namespace("companies")
    include_attributes = COMPANY_INCLUDE_ATTRIBUTES
    subqueries = []
    weights = []
    if is_local_backend():
        for field, weight in [
            ("name_aliases_text", 0.25),
            ("semantic_text", 0.45),
            ("doc2query_text", 0.35),
            ("entity_sector_text", 0.25),
            ("word_text", 0.25),
        ]:
            subqueries.append({
                "rank_by": (field, "BM25", query),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters,
            })
            weights.append(weight)
        if local_namespace_has_vectors("companies") and os.getenv("POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH") == "1":
            query_embedding = await embedding(query)
            subqueries.append({
                "rank_by": ("vector", "kNN", query_embedding),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters if filters is not None else ("id", "NotEq", "__impossible__"),
            })
            weights.append(0.6)
    else:
        query_embedding = await embedding(query)
        subqueries = [
            {
                "rank_by": ("semantic_text", "BM25", query),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters,
            },
            {
                "rank_by": ("doc2query_text", "BM25", query),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters,
            },
            {
                "rank_by": ("entity_sector_text", "BM25", query),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters,
            },
            {
                "rank_by": ("vector", "kNN", query_embedding),
                "top_k": top_k,
                "include_attributes": include_attributes,
                "filters": filters if filters is not None else ("id", "NotEq", "__impossible__"),
            },
        ]
        weights = [0.45, 0.35, 0.25, 0.6]

    def run_query() -> Any:
        return ns.multi_query(queries=subqueries, consistency=STRONG_CONSISTENCY)

    response = await asyncio.to_thread(run_query)
    result_sets = response.results or []
    result_lists = [result_set.rows or [] for result_set in result_sets]
    fused = reciprocal_rank_fusion(result_lists, weights[: len(result_lists)])

    attrs: dict[str, dict[str, Any]] = {}
    for result_set in result_sets:
        for row in result_set.rows or []:
            attrs.setdefault(str(row.id), company_attrs(row))

    rows: list[dict[str, Any]] = []
    for cid, score in fused[:top_k]:
        row = dict(attrs.get(cid) or {"id": cid})
        row["score"] = score
        row["source"] = "semantic"
        rows.append(row)
    return rows


async def filter_only_company_rows(filters: tuple | None, *, page_size: int, max_results: int) -> list[dict[str, Any]]:
    if filters is None:
        return []
    return await filter_only_rows_for_namespace(
        "companies",
        filters,
        COMPANY_INCLUDE_ATTRIBUTES,
        page_size=page_size,
        max_results=max_results,
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file) if args.env_file else None)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)

    existing = [str(cid) for cid in payload.get("company_ids") or [] if cid]
    names = expanded_company_names([str(name) for name in payload.get("company_names") or [] if name])
    semantic_queries = [str(query).strip() for query in payload.get("company_semantic_queries") or [] if str(query).strip()]
    filters = company_attribute_filters(payload)
    hard_filters = company_attribute_filters(payload, include_soft=False)
    soft_filters = company_attribute_filters(payload, only_soft=True)
    soft_union_filters = combine_filters(hard_filters, soft_filters)
    strategy = sector_strategy(payload, args.company_sector_strategy)
    min_results = sector_min_results(payload, args.company_sector_min_results)

    rows: list[dict[str, Any]] = []
    hard_semantic_count = 0
    sector_strategy_broadened = False
    ce_result = None
    if names:
        exact_rows = await exact_name_lookup(names, filters, top_k=args.name_top_k)
        rows.extend(exact_rows)
        if not exact_rows:
            rows.extend(await name_bm25_lookup(names, filters, top_k=args.name_top_k))
    if semantic_queries:
        # Collect semantic rows separately for CE filtering
        semantic_rows_all: list[dict[str, Any]] = []
        if strategy == "hard_filter":
            semantic_rows = await semantic_lookup(semantic_queries, filters, top_k=args.semantic_top_k)
            hard_semantic_count = len(dedupe_rows(semantic_rows))
            semantic_rows_all.extend(semantic_rows)
        elif strategy == "semantic_only" or soft_filters is None:
            semantic_rows = await semantic_lookup(semantic_queries, hard_filters, top_k=args.semantic_top_k)
            hard_semantic_count = len(dedupe_rows(semantic_rows))
            semantic_rows_all.extend(semantic_rows)
        elif strategy == "staged":
            hard_rows = await semantic_lookup(semantic_queries, filters, top_k=args.semantic_top_k)
            hard_semantic_count = len(dedupe_rows(hard_rows))
            semantic_rows_all.extend(hard_rows)
            sector_strategy_broadened = hard_semantic_count < min_results
            if sector_strategy_broadened:
                semantic_rows_all.extend(await semantic_lookup(semantic_queries, hard_filters, top_k=args.semantic_top_k))
        else:
            semantic_rows = await semantic_lookup(semantic_queries, hard_filters, top_k=args.semantic_top_k)
            hard_semantic_count = len(dedupe_rows(semantic_rows))
            semantic_rows_all.extend(semantic_rows)

        # CE rerank semantic rows before union with sector filter rows
        semantic_rows_deduped = dedupe_rows(semantic_rows_all)
        if len(semantic_rows_deduped) > args.ce_threshold and not args.no_ce:
            query_text = " ".join(semantic_queries)
            from ce_rerank import ce_rerank_companies
            ce_result = await ce_rerank_companies(
                query_text,
                semantic_rows_deduped,
                top_n=args.ce_top_n or len(semantic_rows_deduped),
                model=args.ce_model,
                batch_size=args.ce_batch_size,
                concurrency=args.ce_concurrency,
            )
            rows.extend(ce_result["scored_companies"])
        else:
            rows.extend(semantic_rows_all)

        # Soft filter rows always pass through (sector/entity type matches)
        if soft_filters is not None and (strategy == "soft_union" or sector_strategy_broadened):
            soft_rows = await filter_only_company_rows(
                soft_union_filters,
                page_size=args.page_size,
                max_results=args.max_soft_companies,
            )
            for row in soft_rows:
                row["source"] = row.get("source") or "soft_filter"
            rows.extend(soft_rows)
    if filters is not None and not names and not semantic_queries:
        rows.extend(await filter_only_company_rows(filters, page_size=args.page_size, max_results=args.max_companies))

    rows = dedupe_rows(rows)

    # Optional: CE on all rows (semantic + sector) for testing
    if semantic_queries and args.ce_all and len(rows) > args.ce_threshold and not args.no_ce:
        query_text = " ".join(semantic_queries)
        from ce_rerank import ce_rerank_companies
        ce_all_result = await ce_rerank_companies(
            query_text,
            rows,
            top_n=args.ce_top_n or len(rows),
            model=args.ce_model,
            batch_size=args.ce_batch_size,
            concurrency=args.ce_concurrency,
        )
        rows = ce_all_result["scored_companies"]
        if not ce_result:
            ce_result = ce_all_result
        else:
            ce_result["ce_all"] = {
                "pre_ce_count": ce_all_result["total_scored"],
                "post_ce_count": ce_all_result["kept"],
                "threshold": ce_all_result.get("threshold"),
                "mean_score": ce_all_result.get("mean_score"),
                "std_score": ce_all_result.get("std_score"),
            }

    company_ids = list(dict.fromkeys([*existing, *(str(row["id"]) for row in rows if row.get("id"))]))
    result = {
        "namespace": namespace_name("companies"),
        "company_names": names,
        "company_semantic_queries": semantic_queries,
        "company_ids": company_ids[:args.max_companies] if args.max_companies else company_ids,
        "resolved_count": min(len(company_ids), args.max_companies) if args.max_companies else len(company_ids),
        "truncated": bool(args.max_companies and len(company_ids) > args.max_companies),
        "sample_companies": rows[:20],
        "used_attribute_filters": filters is not None,
        "used_semantic_search": bool(semantic_queries),
        "company_sector_strategy": strategy,
        "company_sector_min_results": min_results,
        "hard_semantic_count": hard_semantic_count,
        "sector_strategy_broadened": sector_strategy_broadened,
        "soft_filter_unioned": bool(semantic_queries and soft_filters is not None and (strategy == "soft_union" or sector_strategy_broadened)),
    }
    if ce_result:
        result["ce_rerank"] = {
            "pre_ce_count": ce_result["total_scored"],
            "post_ce_count": ce_result["kept"],
            "model": ce_result["ce_model"],
            "elapsed_ms": ce_result["elapsed_ms"],
            "threshold": ce_result.get("threshold"),
            "mean_score": ce_result.get("mean_score"),
            "std_score": ce_result.get("std_score"),
            "score_distribution": ce_result["score_distribution"],
        }
    return result


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "resolve_companies",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "resolve_companies",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "resolved_count": output.get("resolved_count"),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve company constraints to company IDs")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--name-top-k", type=int, default=20)
    parser.add_argument("--semantic-top-k", type=int, default=2500)
    parser.add_argument("--company-sector-strategy", choices=sorted(SECTOR_STRATEGIES), default="soft_union")
    parser.add_argument("--company-sector-min-results", type=int, default=500)
    parser.add_argument("--max-soft-companies", type=int, default=0, help="Max soft filter companies (0 = paginate all)")
    parser.add_argument("--page-size", type=int, default=10000)
    parser.add_argument("--max-companies", type=int, default=0, help="Max companies in output (0 = no cap)")
    # CE rerank args
    parser.add_argument("--no-ce", action="store_true", help="Disable cross-encoder reranking")
    parser.add_argument("--ce-all", action="store_true", help="Run CE on ALL rows (semantic + sector filter), not just semantic")
    parser.add_argument("--ce-threshold", type=int, default=500, help="Min companies to trigger CE rerank")
    parser.add_argument("--ce-top-n", type=int, default=0, help="Hard cap after CE (0 = use adaptive threshold only)")
    parser.add_argument("--ce-model", default=None, help="Model for CE scoring (default: gpt-4.1-nano)")
    parser.add_argument("--ce-batch-size", type=int, default=20, help="Companies per CE API call")
    parser.add_argument("--ce-concurrency", type=int, default=10, help="Max parallel CE API calls")
    args = parser.parse_args()

    started = time.time()
    output = asyncio.run(run(args))
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        if not args.state:
            raise SystemExit("--write-state requires --state")
        state_path = Path(args.state)
        record_step(state_path, read_json(state_path), output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
