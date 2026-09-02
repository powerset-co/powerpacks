"""Measure exact-filter population floors against the bound search corpus."""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[4]
PRIMITIVES = ROOT / "packs/search/primitives"
for _path in (PRIMITIVES / "lib", PRIMITIVES / "local", PRIMITIVES / "turbopuffer"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

try:  # direct script imports
    from location_scope import location_scope_from_plan
except ImportError:  # pragma: no cover - package imports
    from .location_scope import location_scope_from_plan


BATCH_SIZE = 16
GROUP_CAP = 10_000
NEAR_ZERO_MAX = 3
_TERM_STOPWORDS = {"a", "an", "and", "for", "of", "or", "the", "to"}
_LOCATION_FIELDS = {
    "cities": ("city", "In"),
    "states": ("state", "In"),
    "countries": ("country", "In"),
    "metro_areas": ("metro_areas", "ContainsAny"),
    "macro_regions": ("macro_region", "In"),
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def population_terms(population: str) -> list[str]:
    """Turn the population noun phrase into exact title/headline keyword terms."""
    noun_phrase = re.split(r"\s+(?:with|who)\s+|[,;—|/]", population, maxsplit=1, flags=re.I)[0]
    return list(dict.fromkeys(
        term for term in re.findall(r"[a-z0-9]+", noun_phrase.casefold())
        if term not in _TERM_STOPWORDS
    ))


def candidate_populations(plan: Mapping[str, Any]) -> list[str]:
    populations: list[str] = []
    for row in plan.get("candidate_populations") or []:
        if not isinstance(row, Mapping):
            continue
        population = " ".join(str(row.get("population") or "").split())
        if population and population.casefold() not in {item.casefold() for item in populations}:
            populations.append(population)
    return populations


def _location_filter(filters: Mapping[str, Sequence[str]]) -> list[Any] | None:
    clauses = [
        [field, operator, list(filters[key])]
        for key, (field, operator) in _LOCATION_FIELDS.items()
        if filters.get(key)
    ]
    if not clauses:
        return None
    exact_place = frozenset(filters) in {
        frozenset({"cities", "countries"}),
        frozenset({"states", "countries"}),
    }
    return clauses[0] if len(clauses) == 1 else ["And" if exact_place else "Or", clauses]


def population_filter(
    population: str,
    geography_filters: Mapping[str, Sequence[str]],
    *,
    operator_ids: Sequence[str] = (),
) -> list[Any]:
    """Build the same exact current-title/headline + geography filter for either backend."""
    terms = population_terms(population)
    if not terms:
        raise ValueError(f"population has no title/headline keyword terms: {population!r}")
    clauses: list[Any] = [
        ["word_tokens", "ContainsAllTokens", terms],
        ["is_current", "Eq", True],
    ]
    location = _location_filter(geography_filters)
    if location is not None:
        clauses.append(location)
    if operator_ids:
        clauses.append(["allowed_operator_ids", "ContainsAny", list(operator_ids)])
    return ["And", clauses]


def floor_binding(
    plan: Mapping[str, Any], backend: str, retrieval_identity: Mapping[str, Any]
) -> dict[str, Any]:
    geography, geography_filters = location_scope_from_plan(dict(plan))
    return {
        "backend": backend,
        "retrieval": dict(retrieval_identity),
        "populations": candidate_populations(plan),
        "geography": geography,
        "geography_filters": geography_filters,
    }


def _filter_text(terms: Sequence[str], geography_filters: Mapping[str, Sequence[str]]) -> str:
    parts = [f"title/headline contains all [{', '.join(terms)}]", "is_current = true"]
    for key, (field, operator) in _LOCATION_FIELDS.items():
        values = geography_filters.get(key)
        if values:
            parts.append(f"{field} {operator} [{', '.join(values)}]")
    return " AND ".join(parts)


def _floor_row(
    *,
    population: str,
    geography: str | None,
    geography_filters: Mapping[str, Sequence[str]],
    filter_expression: list[Any],
    count: int,
    capped: bool,
    backend: str,
    observed_at: str,
) -> dict[str, Any]:
    display_count = "10k+" if capped else str(count)
    filter_text = _filter_text(population_terms(population), geography_filters)
    scope = "this set" if backend == "powerset" else "this local index"
    return {
        "population": population,
        "geography": geography or "global",
        "geography_filters": dict(geography_filters),
        "title_headline_terms": population_terms(population),
        "filter_expression": filter_expression,
        "filter": filter_text,
        "count": count,
        "display_count": display_count,
        "capped": capped,
        "observed_at": observed_at,
        "label": (
            "exact-filter floor (lower bound; semantic availability unknown): "
            f"{display_count} current people matching {filter_text} in {scope} at {observed_at}"
        ),
    }


def _powerset_counts(namespace: Any, filters: list[list[Any]]) -> tuple[list[int], list[bool], list[float]]:
    counts: list[int] = []
    capped: list[bool] = []
    timings: list[float] = []
    for start in range(0, len(filters), BATCH_SIZE):
        batch = filters[start:start + BATCH_SIZE]
        queries = [{
            "filters": expression,
            "group_by": ["base_id"],
            "aggregate_by": {"positions": ("Count",)},
            "limit": GROUP_CAP,
            "include_attributes": False,
        } for expression in batch]
        began = time.monotonic()
        response = namespace.multi_query(
            queries=queries,
            consistency={"level": "strong"},
            timeout=2.0,
        )
        timings.append(round(time.monotonic() - began, 3))
        results = list(getattr(response, "results", None) or [])
        if len(results) != len(batch):
            raise RuntimeError(f"population floor batch returned {len(results)} of {len(batch)} results")
        for result in results:
            groups = list(getattr(result, "aggregation_groups", None) or [])
            counts.append(min(len(groups), GROUP_CAP))
            capped.append(len(groups) >= GROUP_CAP)
    return counts, capped, timings


def _local_counts(store: Any, filters: list[list[Any]]) -> tuple[list[int], list[bool], list[float]]:
    counts: list[int] = []
    capped: list[bool] = []
    timings: list[float] = []
    for start in range(0, len(filters), BATCH_SIZE):
        batch = filters[start:start + BATCH_SIZE]
        began = time.monotonic()
        table = store._table_for_namespace("people")
        columns = store._table_columns(table)
        if "base_id" not in columns:
            raise ValueError("local people index must contain base_id for population floors")
        queries: list[str] = []
        params: list[Any] = []
        for expression in batch:
            where_sql, where_params = store._compile_people_where_sql(expression, columns)
            queries.append(
                f"select count(*) from (select distinct _pp_role.base_id "
                f"from {table} as _pp_role where {where_sql} limit {GROUP_CAP + 1})"
            )
            params.extend(where_params)
        actual_counts = [int(row[0] or 0) for row in store.conn.execute(
            " union all ".join(queries), params).fetchall()]
        for actual in actual_counts:
            counts.append(min(actual, GROUP_CAP))
            capped.append(actual > GROUP_CAP)
        timings.append(round(time.monotonic() - began, 3))
    return counts, capped, timings


def probe_populations(
    plan: Mapping[str, Any],
    *,
    backend: str,
    retrieval_identity: Mapping[str, Any],
    env_file: str | Path = ".env",
    powerset_namespace: Any | None = None,
    resolved_operator_ids: Sequence[str] | None = None,
    local_store: Any | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Probe every population in one <=16-count batch and return the persisted artifact shape."""
    if backend not in {"powerset", "local"}:
        raise ValueError(f"unsupported floor backend: {backend!r}")
    binding = floor_binding(plan, backend, retrieval_identity)
    populations = binding["populations"]
    geography = binding["geography"]
    geography_filters = binding["geography_filters"]
    observed_at = observed_at or _now()

    operator_ids: list[str] = []
    namespace_name = None
    close_local = False
    if backend == "powerset":
        set_id = str(retrieval_identity.get("set_id") or "")
        if not set_id:
            raise ValueError("powerset population floors require the bound set id")
        if resolved_operator_ids is None:
            from postgres_client import fetch_set_operator_ids, load_env_file  # type: ignore

            env_path = Path(env_file)
            load_env_file(env_path)
            resolved_operator_ids = fetch_set_operator_ids(set_id=set_id, env_file=env_path)["operator_ids"]
        operator_ids = list(dict.fromkeys(str(value) for value in resolved_operator_ids if value))
        if not operator_ids:
            raise ValueError(f"bound set {set_id!r} resolved no allowed operator ids")
        from turbopuffer_search_backend import namespace, namespace_name as resolve_namespace_name  # type: ignore

        namespace_name = resolve_namespace_name("people")
        powerset_namespace = powerset_namespace or namespace("people")
    elif local_store is None:
        from local_duckdb_store import LocalDuckDBSearchStore  # type: ignore

        local_store = LocalDuckDBSearchStore(str(retrieval_identity.get("db_path") or ""), read_only=True)
        close_local = True

    expressions = [
        population_filter(population, geography_filters, operator_ids=operator_ids)
        for population in populations
    ]
    try:
        if backend == "powerset":
            counts, capped, batch_seconds = _powerset_counts(powerset_namespace, expressions)
        else:
            counts, capped, batch_seconds = _local_counts(local_store, expressions)
    finally:
        if close_local:
            local_store.conn.close()

    floors = [
        _floor_row(
            population=population,
            geography=geography,
            geography_filters=geography_filters,
            filter_expression=expression,
            count=count,
            capped=is_capped,
            backend=backend,
            observed_at=observed_at,
        )
        for population, expression, count, is_capped in zip(populations, expressions, counts, capped)
    ]
    provenance = {
        "backend": backend,
        "filter_expressions": expressions,
        "observed_at": observed_at,
        "batch_seconds": batch_seconds,
    }
    if backend == "powerset":
        provenance.update({
            "namespace": namespace_name,
            "set_id": retrieval_identity["set_id"],
            "resolved_operator_filter": ["allowed_operator_ids", "ContainsAny", operator_ids],
        })
    else:
        provenance.update({
            key: retrieval_identity[key]
            for key in ("db_path", "db_size", "db_mtime_ns")
        })
    return {
        "schema_version": "network-floors.v1",
        "binding": binding,
        "generated_at": observed_at,
        "provenance": provenance,
        "floors": floors,
    }


def sparsity_lines(artifact: Mapping[str, Any]) -> list[str]:
    return [
        f"exact-title floor: {row['display_count']} for {row['population']} in "
        f"{row['geography']} — semantic availability unknown; expect a thin pond."
        for row in artifact.get("floors") or []
        if not row.get("capped") and int(row.get("count") or 0) <= NEAR_ZERO_MAX
    ]
