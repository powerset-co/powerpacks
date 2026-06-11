"""Local DuckDB-backed search store for Powerpacks primitives.

This module deliberately uses top-level imports that work when
``packs/search/primitives/lib`` is added directly to ``sys.path``.  DuckDB is
imported lazily by ``LocalDuckDBSearchStore`` so normal TurboPuffer mode does not
require the local backend dependency to be importable at module import time.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

K_RRF = 60
TOKEN_RE = re.compile(r"[a-z0-9]+")
TITLE_SPLIT_RE = re.compile(r"[,|@\-–—/\(]")
_STEMMER: Any | None = None
ARRAY_FILTER_FIELDS = {
    "accelerators",
    "allowed_operator_ids",
    "customer_type",
    "d2q_tokens",
    "doc2query",
    "entity_types",
    "investor_urns",
    "metro_areas",
    "phrase_tokens",
    "role_ids",
    "school_name_tokens",
    "sector_types",
    "summary_tokens",
    "tech_skills",
    "technology_types",
    "word_tokens",
    "yc_batches",
}
COMPARISON_OPS = {"Eq", "NotEq", "In", "NotIn", "Gt", "Gte", "Lt", "Lte", "ContainsAny", "ContainsAllTokens", "IGlob"}
PERSON_PROFILE_TABLES = ("local_person_profiles", "local_people_profiles")
PERSON_PROFILE_FILTER_FIELDS = {
    "allowed_operator_ids",
    "city",
    "state",
    "country",
    "location_raw",
    "full_name",
    "first_name",
    "last_name",
    "headline",
    "linkedin_url",
    "public_identifier",
    "source_channels",
    "source_artifacts",
    "twitter_handle",
    "x_twitter_handle",
    "x_twitter_followers",
    "linkedin_followers",
    "linkedin_connections",
    "ig_followers",
    "inferred_birth_year",
}
ROLE_ROW_PREFERRED_PROFILE_FIELDS = {"inferred_birth_year"}
TITLE_CLUSTER_SENIORITY_PREFIXES = {
    "senior", "sr", "staff", "principal", "lead", "junior", "jr",
    "associate", "intern", "founding", "chief", "head",
    "vp", "vice", "president", "director", "avp",
    "founder", "co", "owner",
}
TITLE_CLUSTER_NOISE_WORDS = {"at", "the", "and", "of", "for", "in", "on", "to", "a", "an", "with", "ii", "iii", "iv", "i"}
_MISSING_FILTER_COLUMN_WARNED: set[tuple[str, str]] = set()


def _warn_missing_filter_column(field: str, operator: str) -> None:
    """Warn loudly (once per field/operator) when a filter silently degrades.

    A filter on a column that does not exist in the local table compiles to a
    constant clause (no-match for positive operators, match-all for NotEq /
    NotIn).  That silent degradation caused real parity failures (for example
    a missing metro_areas column turning an Or(city, metro_areas) location
    filter into exact city matching), so surface it on stderr.
    """
    key = (field, operator)
    if key in _MISSING_FILTER_COLUMN_WARNED:
        return
    _MISSING_FILTER_COLUMN_WARNED.add(key)
    effect = "matches all rows" if operator in {"NotEq", "NotIn"} else "matches no rows"
    print(
        f"[local-duckdb] WARNING: filter field {field!r} has no backing column in the local table; "
        f"{operator} clause {effect}. Rebuild the local DuckDB index so the column exists.",
        file=sys.stderr,
        flush=True,
    )


class LocalDuckDBError(RuntimeError):
    """Clear local-backend runtime error."""


@dataclass
class LocalQueryResponse:
    rows: list[Any]


class LocalQueryRow:
    """TurboPuffer-compatible lightweight row object."""

    def __init__(self, row_id: Any, attrs: dict[str, Any] | None = None):
        self.id = str(row_id)
        self.model_extra = dict(attrs or {})

    def __getattr__(self, name: str) -> Any:
        if name in self.model_extra:
            return self.model_extra[name]
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"LocalQueryRow(id={self.id!r}, model_extra={self.model_extra!r})"


class LocalDuckDBNamespace:
    def __init__(self, store: "LocalDuckDBSearchStore", logical_name: str):
        self.store = store
        self.logical_name = logical_name

    def query(self, *, rank_by: Any = None, filters: Any = None, top_k: int = 10, include_attributes: list[str] | None = None, **_: Any) -> LocalQueryResponse:
        return self.store.query_namespace(self.logical_name, rank_by, filters, top_k, include_attributes or [])

    def multi_query(self, *, queries: list[dict[str, Any]], **_: Any) -> Any:
        results = [
            self.query(
                rank_by=query.get("rank_by"),
                filters=query.get("filters"),
                top_k=int(query.get("top_k") or 10),
                include_attributes=list(query.get("include_attributes") or []),
            )
            for query in queries
        ]
        return type("LocalMultiQueryResponse", (), {"results": results})()


class LocalDuckDBSearchStore:
    NAMESPACE_TABLES = {
        "people": "local_people_positions",
        "summaries": "local_summaries",
        "company_signals": "local_company_signals",
        "education": "local_people_education",
        "schools": "local_education",
        "companies": "local_companies",
    }

    FIELD_ALIASES = {
        "company_urn": ["company_urn", "id"],
        "name_aliases_text": ["name_aliases_text", "aliases", "name_aliases", "company_name"],
        "doc2query_text": ["doc2query_text", "doc2query", "d2q_text"],
        "entity_sector_text": ["entity_sector_text", "word_text"],
        "website_domain": ["website_domain", "domain"],
        "linkedin_url": ["linkedin_url", "url", "company_url"],
        "logo_url": ["logo_url", "logo"],
    }

    def __init__(self, db_path: str, *, read_only: bool = True):
        try:
            import duckdb  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("duckdb is required for local DuckDB search") from exc

        self.db_path = str(db_path)
        self.read_only = read_only
        self.conn = duckdb.connect(self.db_path, read_only=read_only)

    def fork(self) -> "LocalDuckDBSearchStore":
        """Return a store sharing this store's database instance via a cursor.

        Concurrent ``duckdb.connect()`` calls to the same file race DuckDB's
        per-file instance cache while sibling connections are being closed,
        which can transiently surface an empty catalog (missing tables /
        empty PRAGMA table_info). A ``.cursor()`` duplicates the connection
        on the SAME instance and is the sanctioned per-thread pattern, so
        threaded fan-out must fork one root store instead of reconnecting.
        """
        clone = object.__new__(LocalDuckDBSearchStore)
        clone.db_path = self.db_path
        clone.read_only = self.read_only
        clone.conn = self.conn.cursor()
        return clone

    def namespace(self, logical_name: str) -> LocalDuckDBNamespace:
        self._table_for_namespace(logical_name)
        return LocalDuckDBNamespace(self, logical_name)

    def _table_for_namespace(self, logical_name: str) -> str:
        table = self.NAMESPACE_TABLES.get(str(logical_name))
        if not table:
            supported = ", ".join(sorted(self.NAMESPACE_TABLES))
            raise LocalDuckDBError(f"unknown local DuckDB namespace {logical_name!r}; supported namespaces: {supported}")
        if not self._table_exists(table):
            raise LocalDuckDBError(f"local DuckDB namespace {logical_name!r} requires missing table {table!r}")
        return table

    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "select count(*) from information_schema.tables where table_schema in ('main', 'temp') and table_name = ?",
            [table],
        ).fetchone()
        return bool(row and row[0])

    def namespace_exists(self, logical_name: str) -> bool:
        table = self.NAMESPACE_TABLES.get(str(logical_name))
        return bool(table and self._table_exists(table))

    def namespace_row_count(self, logical_name: str) -> int:
        table = self.NAMESPACE_TABLES.get(str(logical_name))
        if not table or not self._table_exists(table):
            return 0
        row = self.conn.execute(f"select count(*) from {self._quote_ident(table)}").fetchone()
        return int(row[0] or 0) if row else 0

    def filtered_people_count(self, filters: Any) -> dict[str, int]:
        """Distinct-person pool size for the people namespace under hard filters.

        Used by the prepare preview's breadth gate: hybrid ranking only orders
        the filter-eligible pool, so this count is the upper bound on how many
        people downstream hydration/LLM stages could touch.
        """
        table = self._table_for_namespace("people")
        columns = self._table_columns(table)
        person_col = "person_id" if "person_id" in columns else "base_id"
        where_sql, params = self._compile_people_where_sql(filters, columns)
        matched = self.conn.execute(
            f"select count(distinct _pp_role.{self._quote_ident(person_col)}), count(*) from {self._quote_ident(table)} as _pp_role where {where_sql}",
            params,
        ).fetchone()
        total = self.conn.execute(
            f"select count(distinct {self._quote_ident(person_col)}) from {self._quote_ident(table)}"
        ).fetchone()
        return {
            "matched_people": int(matched[0] or 0),
            "matched_positions": int(matched[1] or 0),
            "total_people": int(total[0] or 0),
        }

    def _person_profile_table(self) -> str | None:
        for table in PERSON_PROFILE_TABLES:
            if self._table_exists(table):
                return table
        return None

    def _person_profile_can_constrain_positions(self) -> bool:
        profile_table = self._person_profile_table()
        if not profile_table or not self._table_exists("local_people_positions"):
            return False
        try:
            row = self.conn.execute(
                f"""
                select count(*)
                from {self._quote_ident(profile_table)} p
                join local_people_positions r
                  on cast(p.person_id as varchar) = cast(r.person_id as varchar)
                  or cast(p.person_id as varchar) = cast(r.base_id as varchar)
                limit 1
                """
            ).fetchone()
            return bool(row and row[0])
        except Exception:
            return False

    def _rows_for_namespace(self, logical_name: str) -> list[dict[str, Any]]:
        table = self._table_for_namespace(logical_name)
        try:
            rows = self.conn.execute(f"select * from {table}").fetchall()
            columns = [desc[0] for desc in self.conn.description or []]
        except Exception as exc:
            raise LocalDuckDBError(f"failed reading local DuckDB table {table!r} for namespace {logical_name!r}: {exc}") from exc
        return [self._normalize_row(dict(zip(columns, row))) for row in rows]

    def _table_columns(self, table: str) -> dict[str, str]:
        return {str(row[1]): str(row[2]).upper() for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def has_nonempty_vectors(self, logical_name: str, field: str = "vector") -> bool:
        table = self._table_for_namespace(logical_name)
        columns = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if field not in {str(row[1]) for row in columns}:
            return False
        try:
            row = self.conn.execute(f"select count(*) from {table} where {field} is not null and len({field}) > 0").fetchone()
        except Exception:
            return False
        return bool(row and row[0])

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {key: self._normalize_value(value) for key, value in row.items()}

    def _normalize_value(self, value: Any) -> Any:
        if hasattr(value, "tolist"):
            try:
                value = value.tolist()
            except Exception:
                pass
        if isinstance(value, (list, tuple)):
            # Fast path for flat numeric arrays (embedding vectors are 1536
            # floats per row); per-element recursion here dominated filtered
            # fetches at ~76M isinstance calls per resolve.
            if all(type(item) is float or type(item) is int for item in value):
                return list(value)
            return [self._normalize_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._normalize_value(item) for key, item in value.items()}
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            if text and text[0] in "[{" and text[-1] in "]}":
                try:
                    return self._normalize_value(json.loads(text))
                except Exception:
                    return value
        return value

    def _row_id(self, row: dict[str, Any]) -> str:
        for field in ("id", "company_urn", "position_id", "person_id", "canonical_education_id", "base_id"):
            if row.get(field) is not None:
                return str(row[field])
        raise LocalDuckDBError("local DuckDB row is missing id/company_urn/position_id/person_id/canonical_education_id/base_id")

    def _field_value(self, row: dict[str, Any], field: str) -> Any:
        if field in row:
            return row[field]
        for alias in self.FIELD_ALIASES.get(field, []):
            if alias in row:
                return row[alias]
        return None

    def _quote_ident(self, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise LocalDuckDBError(f"unsafe DuckDB identifier: {value!r}")
        return f'"{value}"'

    def _sql_field(self, field: str, columns: dict[str, str]) -> str | None:
        candidates = [field, f"{field}_tokens", *self.FIELD_ALIASES.get(field, [])]
        for candidate in candidates:
            if candidate in columns:
                return candidate
        return None

    def _is_array_sql_field(self, field: str, columns: dict[str, str]) -> bool:
        column_type = columns.get(field, "")
        return field in ARRAY_FILTER_FIELDS or "[]" in column_type or column_type.startswith("LIST")

    def _value_list(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (str, bytes, bytearray, dict)):
            return [value]
        if isinstance(value, Iterable):
            return list(value)
        return [value]

    def _string_list(self, value: Any) -> list[str]:
        return [str(item) for item in self._value_list(value)]

    def _tokenize_filter_value(self, value: Any) -> list[str]:
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, dict)):
            tokens: list[str] = []
            for item in value:
                tokens.extend(TOKEN_RE.findall(str(item).lower()))
            return tokens
        return TOKEN_RE.findall(str(value or "").lower())

    def _sql_in_list(self, values: list[Any]) -> str:
        return "(" + ", ".join("?" for _ in values) + ")"

    def _sql_contains_any(self, sql_field: str, values: list[Any], *, array_field: bool) -> tuple[str, list[Any]]:
        if not values:
            return "false", []
        column = self._quote_ident(sql_field)
        params = self._string_list(values)
        if array_field:
            return (
                f"exists (select 1 from unnest({column}) as _pp_u(value) "
                f"where cast(_pp_u.value as varchar) in {self._sql_in_list(params)})",
                params,
            )
        return f"cast({column} as varchar) in {self._sql_in_list(params)}", params

    def _sql_contains_all_tokens(self, sql_field: str, expected: Any, options: dict[str, Any] | None, *, array_field: bool) -> tuple[str, list[Any]]:
        tokens = self._tokenize_filter_value(expected)
        if not tokens:
            return "true", []
        column = self._quote_ident(sql_field)
        last_as_prefix = bool((options or {}).get("last_as_prefix"))
        exact_tokens = tokens[:-1] if last_as_prefix else tokens
        clauses: list[str] = []
        params: list[Any] = []
        if array_field:
            for token in exact_tokens:
                clauses.append(f"list_contains({column}, ?)")
                params.append(token)
            if last_as_prefix:
                clauses.append(f"exists (select 1 from unnest({column}) as _pp_u(value) where cast(_pp_u.value as varchar) like ?)")
                params.append(f"{tokens[-1]}%")
            return " and ".join(clauses) if clauses else "true", params

        text_expr = f"lower(cast({column} as varchar))"
        for token in exact_tokens:
            clauses.append(f"regexp_matches({text_expr}, ?)")
            params.append(rf"(^|[^a-z0-9]){re.escape(token)}([^a-z0-9]|$)")
        if last_as_prefix:
            clauses.append(f"regexp_matches({text_expr}, ?)")
            params.append(rf"(^|[^a-z0-9]){re.escape(tokens[-1])}")
        return " and ".join(clauses) if clauses else "true", params

    def _sql_like_pattern(self, pattern: Any) -> str:
        text = str(pattern or "").lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return text.replace("*", "%").replace("?", "_")

    def _filter_fields(self, filters: Any) -> set[str]:
        if filters is None or not isinstance(filters, (list, tuple)) or not filters:
            return set()
        op_or_field = filters[0]
        if op_or_field in {"And", "Or"} and len(filters) == 2 and isinstance(filters[1], (list, tuple)):
            out: set[str] = set()
            for clause in filters[1]:
                out.update(self._filter_fields(clause))
            return out
        if len(filters) in {3, 4} and isinstance(filters[0], str):
            return {filters[0]}
        return set()

    def _is_person_profile_filter(self, filters: Any, role_columns: dict[str, str] | None = None) -> bool:
        fields = self._filter_fields(filters)
        if role_columns and fields & ROLE_ROW_PREFERRED_PROFILE_FIELDS:
            if all(field in role_columns for field in fields & ROLE_ROW_PREFERRED_PROFILE_FIELDS):
                return False
        return bool(fields) and fields.issubset(PERSON_PROFILE_FILTER_FIELDS)

    def _split_person_role_filters(self, filters: Any, role_columns: dict[str, str] | None = None) -> tuple[Any | None, Any | None]:
        if filters is None:
            return None, None
        if not isinstance(filters, (list, tuple)) or not filters:
            return None, filters
        op_or_field = filters[0]
        if op_or_field == "And" and len(filters) == 2 and isinstance(filters[1], (list, tuple)):
            person_clauses: list[Any] = []
            role_clauses: list[Any] = []
            for clause in filters[1]:
                person_filter, role_filter = self._split_person_role_filters(clause, role_columns)
                if person_filter is not None:
                    person_clauses.append(person_filter)
                if role_filter is not None:
                    role_clauses.append(role_filter)
            return (
                ["And", person_clauses] if person_clauses else None,
                ["And", role_clauses] if role_clauses else None,
            )
        if op_or_field == "Or" and len(filters) == 2 and isinstance(filters[1], (list, tuple)):
            if all(self._is_person_profile_filter(clause, role_columns) for clause in filters[1]):
                return filters, None
            return None, filters
        if self._is_person_profile_filter(filters, role_columns):
            return filters, None
        return None, filters

    def _compile_filter_sql(self, filters: Any, columns: dict[str, str]) -> tuple[str, list[Any]]:
        if filters is None:
            return "true", []
        if not isinstance(filters, (list, tuple)) or not filters:
            raise ValueError(f"invalid filter tuple: {filters!r}")

        op_or_field = filters[0]
        if op_or_field in {"And", "Or"}:
            if len(filters) != 2 or not isinstance(filters[1], (list, tuple)):
                raise ValueError(f"{op_or_field} filter must be a 2-tuple containing a list of clauses")
            joiner = " and " if op_or_field == "And" else " or "
            clauses: list[str] = []
            params: list[Any] = []
            for clause in filters[1]:
                clause_sql, clause_params = self._compile_filter_sql(clause, columns)
                clauses.append(f"({clause_sql})")
                params.extend(clause_params)
            if not clauses:
                return ("true" if op_or_field == "And" else "false"), []
            return joiner.join(clauses), params

        if len(filters) not in {3, 4}:
            raise ValueError(f"invalid comparison filter tuple: {filters!r}")
        field, operator, expected = filters[0], filters[1], filters[2]
        if not isinstance(field, str) or not isinstance(operator, str) or operator not in COMPARISON_OPS:
            raise ValueError(f"unsupported comparison filter tuple: {filters!r}")
        if len(filters) == 4 and operator != "ContainsAllTokens":
            raise ValueError(f"operator {operator!r} does not accept filter options")

        sql_field = self._sql_field(field, columns)
        if sql_field is None:
            _warn_missing_filter_column(field, operator)
            return ("true", []) if operator in {"NotEq", "NotIn"} else ("false", [])
        column = self._quote_ident(sql_field)
        array_field = self._is_array_sql_field(sql_field, columns)

        if operator == "Eq":
            return f"{column} = ?", [expected]
        if operator == "NotEq":
            return f"({column} is null or {column} <> ?)", [expected]
        if operator == "In":
            values = self._value_list(expected)
            return self._sql_contains_any(sql_field, values, array_field=array_field)
        if operator == "NotIn":
            values = self._value_list(expected)
            if not values:
                return "true", []
            clause, params = self._sql_contains_any(sql_field, values, array_field=array_field)
            return f"not ({clause})", params
        if operator in {"Gt", "Gte", "Lt", "Lte"}:
            op = {"Gt": ">", "Gte": ">=", "Lt": "<", "Lte": "<="}[operator]
            return f"{column} {op} ?", [expected]
        if operator == "ContainsAny":
            return self._sql_contains_any(sql_field, self._value_list(expected), array_field=array_field)
        if operator == "ContainsAllTokens":
            options = filters[3] if len(filters) == 4 else None
            if options is not None and not isinstance(options, dict):
                raise ValueError("ContainsAllTokens options must be a dict")
            return self._sql_contains_all_tokens(sql_field, expected, options, array_field=array_field)
        if operator == "IGlob":
            pattern = self._sql_like_pattern(expected)
            return f"lower(cast({column} as varchar)) like ? escape '\\'", [pattern]

        raise ValueError(f"unsupported filter operator: {operator!r}")

    def _row_id_order_sql(self, columns: dict[str, str]) -> str:
        for field in ("id", "company_urn", "position_id", "person_id", "canonical_education_id", "base_id"):
            if field in columns:
                return self._quote_ident(field)
        return "1"

    def _compile_people_where_sql(self, filters: Any, columns: dict[str, str], outer_alias: str = "_pp_role") -> tuple[str, list[Any]]:
        effective_filters = filters
        person_filters = None
        profile_table = self._person_profile_table()
        if profile_table and self._person_profile_can_constrain_positions():
            person_filters, effective_filters = self._split_person_role_filters(filters, columns)
        where_sql, params = self._compile_filter_sql(effective_filters, columns)
        if person_filters is None or not profile_table:
            return where_sql, params

        role_id_fields = [field for field in ["person_id", "base_id"] if field in columns]
        profile_columns = self._table_columns(profile_table)
        profile_id_fields = [field for field in ["person_id", "base_id", "id"] if field in profile_columns]
        if not role_id_fields or not profile_id_fields:
            return "false", []
        profile_where_sql, profile_params = self._compile_filter_sql(person_filters, profile_columns)
        link_clauses = [
            f"cast(p.{self._quote_ident(profile_field)} as varchar) = cast({outer_alias}.{self._quote_ident(role_field)} as varchar)"
            for profile_field in profile_id_fields
            for role_field in role_id_fields
        ]
        semijoin = (
            f"exists (select 1 from {self._quote_ident(profile_table)} p "
            f"where ({profile_where_sql}) and ({' or '.join(link_clauses)}))"
        )
        return f"({where_sql}) and ({semijoin})", [*params, *profile_params]

    def _filtered_rows_sql(self, logical_name: str, filters: Any, *, limit: int = 0, order_by_id: bool = False) -> list[dict[str, Any]]:
        table = self._table_for_namespace(logical_name)
        columns = self._table_columns(table)
        if logical_name == "people":
            where_sql, params = self._compile_people_where_sql(filters, columns)
            sql = f"select _pp_role.* from {self._quote_ident(table)} as _pp_role where {where_sql}"
        else:
            where_sql, params = self._compile_filter_sql(filters, columns)
            sql = f"select * from {self._quote_ident(table)} where {where_sql}"
        if order_by_id:
            sql += f" order by {self._row_id_order_sql(columns)}"
        if limit and limit > 0:
            sql += " limit ?"
            params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
            result_columns = [desc[0] for desc in self.conn.description or []]
        except Exception as exc:
            raise LocalDuckDBError(f"failed querying local DuckDB table {table!r} for namespace {logical_name!r}: {exc}") from exc
        return [self._normalize_row(dict(zip(result_columns, row))) for row in rows]

    def _project_row_object(self, row: dict[str, Any], include_attributes: list[str]) -> LocalQueryRow:
        row_id = self._row_id(row)
        attrs = {key: self._field_value(row, key) for key in include_attributes if self._field_value(row, key) is not None}
        if "score" in row:
            attrs["score"] = row.get("score")
        return LocalQueryRow(row_id, attrs)

    def _project_dict(self, row: dict[str, Any], include_attributes: list[str]) -> dict[str, Any]:
        out = {"id": self._row_id(row)}
        for key in include_attributes:
            value = self._field_value(row, key)
            if value is not None:
                out[key] = value
        if "position_id" not in out and row.get("position_id") is not None:
            out["position_id"] = row.get("position_id")
        return out

    def _filtered_rows(self, logical_name: str, filters: Any) -> list[dict[str, Any]]:
        return self._filtered_rows_sql(logical_name, filters)

    def filter_only_rows_for_namespace(
        self,
        logical_name: str,
        filters: Any,
        include_attributes: list[str],
        page_size: int = 10000,
        max_results: int = 0,
    ) -> list[dict[str, Any]]:
        rows = self._filtered_rows_sql(logical_name, filters, limit=max_results, order_by_id=True)
        return [self._project_dict(row, include_attributes) for row in rows]

    def query_namespace(
        self,
        logical_name: str,
        rank_by: Any,
        filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> LocalQueryResponse:
        if isinstance(rank_by, (list, tuple)) and len(rank_by) >= 3 and str(rank_by[1]) == "kNN":
            rows = self._vector_rank_sql(logical_name, filters, str(rank_by[0]), rank_by[2], top_k)
            return LocalQueryResponse([self._project_row_object(row, include_attributes) for row in rows])

        rows = self._filtered_rows(logical_name, filters)
        ranked = self._rank_rows(logical_name, rows, rank_by)
        if top_k and top_k > 0:
            ranked = ranked[:top_k]
        return LocalQueryResponse([self._project_row_object(row, include_attributes) for row in ranked])

    def _with_score(self, row: dict[str, Any], score: float) -> dict[str, Any]:
        out = dict(row)
        out["score"] = score
        return out

    def _rank_rows(self, logical_name: str, rows: list[dict[str, Any]], rank_by: Any) -> list[dict[str, Any]]:
        if not rank_by:
            if logical_name == "schools":
                return sorted(rows, key=lambda row: (-float(row.get("person_count") or 0), self._row_id(row)))
            return sorted(rows, key=lambda row: self._row_id(row))

        if isinstance(rank_by, (list, tuple)) and len(rank_by) == 2:
            field, direction = str(rank_by[0]), str(rank_by[1]).lower()
            reverse = direction == "desc"
            return sorted(rows, key=lambda row: (self._field_value(row, field) is None, self._field_value(row, field), self._row_id(row)), reverse=reverse)

        if isinstance(rank_by, (list, tuple)) and len(rank_by) >= 3:
            field, operator, value = str(rank_by[0]), str(rank_by[1]), rank_by[2]
            if operator == "BM25":
                scored = self._bm25_rank(rows, field, value)
                return [self._with_score(row, score) for row, score in scored]
            if operator == "kNN":
                scored = self._vector_rank(rows, field, value)
                return [self._with_score(row, score) for row, score in scored]

        raise ValueError(f"unsupported local DuckDB rank_by expression: {rank_by!r}")

    def _stem_words(self, words: list[str]) -> list[str]:
        global _STEMMER
        try:
            if _STEMMER is None:
                import snowballstemmer  # type: ignore

                _STEMMER = snowballstemmer.stemmer("english")
            return [_STEMMER.stemWord(word) for word in words]
        except Exception:
            return words

    def _phrase_query_tokens(self, query: str) -> list[str]:
        words = TOKEN_RE.findall(str(query).lower())
        stems = self._stem_words(words)
        return [" ".join(stems)] if stems else []

    def _word_query_tokens(self, query: str) -> list[str]:
        words = TOKEN_RE.findall(str(query).lower())
        result = list(words)
        for index in range(len(words) - 1):
            result.append(f"{words[index]} {words[index + 1]}")
        return result

    def _tokens_for_value(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return TOKEN_RE.findall(value.lower())
        if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
            tokens: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip().lower()
                    tokens.extend([text] if " " in text else TOKEN_RE.findall(text))
                else:
                    tokens.extend(TOKEN_RE.findall(str(item).lower()))
            return tokens
        return TOKEN_RE.findall(str(value).lower())

    def _bm25_rank(self, rows: list[dict[str, Any]], field: str, query: Any) -> list[tuple[dict[str, Any], float]]:
        query_tokens = self._tokens_for_value(query)
        if not query_tokens or not rows:
            return []
        docs = [self._tokens_for_value(self._field_value(row, field)) for row in rows]
        avgdl = sum(len(doc) for doc in docs) / max(1, len(docs))
        df: Counter[str] = Counter()
        for doc in docs:
            for token in set(doc):
                df[token] += 1
        query_counts = Counter(query_tokens)
        k1 = 1.2
        b = 0.75
        scored: list[tuple[dict[str, Any], float]] = []
        for row, doc in zip(rows, docs):
            if not doc:
                continue
            tf = Counter(doc)
            score = 0.0
            dl = len(doc)
            for token, qf in query_counts.items():
                freq = tf.get(token, 0)
                if not freq:
                    continue
                idf = math.log(1.0 + (len(docs) - df[token] + 0.5) / (df[token] + 0.5))
                denom = freq + k1 * (1.0 - b + b * dl / max(avgdl, 1e-9))
                score += qf * idf * (freq * (k1 + 1.0) / denom)
            if score > 0.0:
                scored.append((row, score))
        return sorted(scored, key=lambda item: (-item[1], self._row_id(item[0])))

    def _vector_values(self, value: Any) -> list[float]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return self._vector_values(parsed)
            except Exception:
                return []
        if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
            out: list[float] = []
            for item in value:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    return []
            return out
        return []

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)

    def _vector_rank(self, rows: list[dict[str, Any]], field: str, query_vector: Any) -> list[tuple[dict[str, Any], float]]:
        query = self._vector_values(query_vector)
        if not query:
            return []
        scored = []
        for row in rows:
            score = self._cosine(self._vector_values(self._field_value(row, field)), query)
            if score > 0.0:
                scored.append((row, score))
        return sorted(scored, key=lambda item: (-item[1], self._row_id(item[0])))

    def _vector_rank_sql(
        self,
        logical_name: str,
        filters: Any,
        field: str,
        query_vector: Any,
        top_k: int,
    ) -> list[dict[str, Any]]:
        query = self._vector_values(query_vector)
        if not query:
            return []

        table = self._table_for_namespace(logical_name)
        columns = self._table_columns(table)
        sql_field = self._sql_field(field, columns)
        if sql_field is None:
            return []

        where_sql, params = (
            self._compile_people_where_sql(filters, columns)
            if logical_name == "people"
            else self._compile_filter_sql(filters, columns)
        )
        vector_column = self._quote_ident(sql_field)
        order_column = self._row_id_order_sql(columns)
        sql = f"""
            with filtered as (
                select _pp_role.*
                from {self._quote_ident(table)} as _pp_role
                where {where_sql}
            ), scored as (
                select
                    *,
                    list_cosine_similarity({vector_column}, ?::DOUBLE[]) as score
                from filtered
                where {vector_column} is not null
                  and len({vector_column}) = ?
            )
            select *
            from scored
            where score > 0.0
            order by score desc, {order_column}
        """
        query_params = [*params, query, len(query)]
        if top_k and top_k > 0:
            sql += " limit ?"
            query_params.append(top_k)

        try:
            rows = self.conn.execute(sql, query_params).fetchall()
            result_columns = [desc[0] for desc in self.conn.description or []]
        except Exception as exc:
            raise LocalDuckDBError(f"failed vector-ranking local DuckDB table {table!r} for namespace {logical_name!r}: {exc}") from exc
        return [self._normalize_row(dict(zip(result_columns, row))) for row in rows]

    def _rrf(self, result_lists: list[list[dict[str, Any]]], weights: list[float]) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for rows, weight in zip(result_lists, weights):
            for rank, row in enumerate(rows, start=1):
                row_id = self._row_id(row)
                scores[row_id] = scores.get(row_id, 0.0) + weight / (K_RRF + rank)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

    def _base_person_id(self, value: str) -> str:
        parts = str(value).split("-")
        if len(parts) == 6 and parts[5].isdigit():
            return "-".join(parts[:5])
        return str(value)

    def _role_output_row(self, row: dict[str, Any], include_attributes: list[str], score: float, retrieval_mode: str) -> dict[str, Any]:
        row_id = self._row_id(row)
        out = self._project_dict(row, include_attributes)
        out["id"] = row_id
        out["score"] = score
        out["person_id"] = row.get("person_id") or row.get("base_id") or self._base_person_id(row_id)
        out["position_id"] = row.get("position_id") or row_id
        out["retrieval_mode"] = retrieval_mode
        for key in ["has_core_regex", "has_adjacent_regex", "bucket"]:
            if key in row:
                out[key] = row[key]
        return out

    def _cluster_stem_title(self, title: str) -> str:
        core = TITLE_SPLIT_RE.split(str(title or ""))[0].strip()
        tokens = TOKEN_RE.findall(core.lower())
        meaningful = [token for token in tokens if token not in TITLE_CLUSTER_NOISE_WORDS]
        while meaningful and meaningful[0] in TITLE_CLUSTER_SENIORITY_PREFIXES:
            meaningful.pop(0)
        return " ".join(self._stem_words(meaningful)) if meaningful else ""

    def title_clusters_for_filters(self, filters: Any, *, max_titles: int = 10000) -> list[dict[str, Any]]:
        rows = self._filtered_rows_sql("people", filters, limit=max_titles)
        clusters: dict[str, Counter[str]] = {}
        for row in rows:
            title = str(row.get("position_title") or "").strip()
            if not title:
                continue
            stemmed = self._cluster_stem_title(title)
            if not stemmed:
                continue
            clusters.setdefault(stemmed, Counter())[title] += 1
        out: list[dict[str, Any]] = []
        for stemmed, raw_counts in sorted(clusters.items(), key=lambda item: (-sum(item[1].values()), item[0])):
            raw_titles = [title for title, _count in raw_counts.most_common()]
            out.append({
                "stemmed": stemmed,
                "count": sum(raw_counts.values()),
                "raw_titles": raw_titles[:5],
                "display_title": raw_titles[0],
            })
        return out

    def _compile_patterns(self, patterns: Any) -> list[re.Pattern[str]]:
        compiled: list[re.Pattern[str]] = []
        for item in patterns or []:
            if not isinstance(item, dict):
                continue
            regex = str(item.get("regex") or "").strip()
            if not regex:
                continue
            try:
                compiled.append(re.compile(regex, re.IGNORECASE))
            except re.error:
                continue
        return compiled

    def _uses_regex_patterns(self, payload: dict[str, Any]) -> bool:
        role_function = str(payload.get("role_function") or "").strip().lower()
        role_ids = {str(value).strip().lower() for value in payload.get("role_ids") or [] if value}
        # network-search-api disables regex pattern execution for the founder
        # shortcut; mirror that before adding local regex rows so founder title
        # filters do not get misleading regex provenance.
        if role_function == "founder" or "founder" in role_ids:
            return False
        return bool(payload.get("role_core_patterns") or payload.get("role_adjacent_patterns"))

    def _regex_match_rank(
        self,
        rows: list[dict[str, Any]],
        *,
        core_patterns: Any,
        adjacent_patterns: Any,
        text_fields: list[str],
    ) -> list[dict[str, Any]]:
        core = self._compile_patterns(core_patterns)
        adjacent = self._compile_patterns(adjacent_patterns)
        if not core and not adjacent:
            return []
        ranked: list[tuple[dict[str, Any], float]] = []
        for row in rows:
            text = " ".join(str(self._field_value(row, field) or "") for field in text_fields)
            has_core = any(pattern.search(text) for pattern in core)
            has_adjacent = False if has_core else any(pattern.search(text) for pattern in adjacent)
            if not (has_core or has_adjacent):
                continue
            out = dict(row)
            out["has_core_regex"] = has_core
            out["has_adjacent_regex"] = has_adjacent
            out["bucket"] = "good" if has_core else "maybe"
            ranked.append((out, 1.0 if has_core else 0.5))
        return [row for row, _score in sorted(ranked, key=lambda item: (-item[1], self._row_id(item[0])))]

    def _best_text_field(self, table: str, preferred: list[str]) -> str | None:
        columns = self._table_columns(table)
        for field in preferred:
            if field in columns:
                return field
        return None

    async def hybrid_role_rows(
        self,
        payload: dict[str, Any],
        filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> list[dict[str, Any]]:
        semantic_query = str(payload.get("semantic_query") or "").strip()
        query_embedding = payload.get("query_embedding")
        if semantic_query and query_embedding is None:
            raise LocalDuckDBError("local semantic role search requires query_embedding")
        has_regex_patterns = self._uses_regex_patterns(payload)
        if not semantic_query and not payload.get("bm25_queries") and query_embedding is None and not has_regex_patterns:
            rows = sorted(self._filtered_rows("people", filters), key=lambda row: self._row_id(row))
            if top_k and top_k > 0:
                rows = rows[:top_k]
            return [self._role_output_row(row, include_attributes, 1.0, "filter_only") for row in rows]

        bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
        result_lists: list[list[dict[str, Any]]] = []
        weights: list[float] = []

        phrase_tokens: list[str] = []
        word_tokens: list[str] = []
        for query in bm25_queries:
            phrase_tokens.extend(self._phrase_query_tokens(query))
            word_tokens.extend(self._word_query_tokens(query))
        candidates: list[dict[str, Any]] | None = None
        for field, tokens, weight in [
            ("phrase_tokens", list(dict.fromkeys(phrase_tokens)), 1.5),
            ("word_tokens", list(dict.fromkeys(word_tokens)), 1.0),
        ]:
            if tokens:
                if candidates is None:
                    candidates = self._filtered_rows("people", filters)
                result_lists.append([row for row, _score in self._bm25_rank(candidates, field, tokens)[:top_k]])
                weights.append(weight)

        if query_embedding is not None:
            result_lists.append(self._vector_rank_sql("people", filters, "vector", query_embedding, top_k))
            weights.append(0.6)

        if has_regex_patterns:
            if candidates is None:
                candidates = self._filtered_rows("people", filters)
            regex_ranked = self._regex_match_rank(
                candidates,
                core_patterns=payload.get("role_core_patterns"),
                adjacent_patterns=payload.get("role_adjacent_patterns"),
                text_fields=["position_title"],
            )
            if regex_ranked:
                result_lists.append(regex_ranked[:top_k])
                weights.append(0.5)

        if not result_lists:
            rows = sorted(self._filtered_rows("people", filters), key=lambda row: self._row_id(row))
            if top_k and top_k > 0:
                rows = rows[:top_k]
            return [self._role_output_row(row, include_attributes, 1.0, "filter_only") for row in rows]

        by_id = {self._row_id(row): row for rows in result_lists for row in rows}
        fused = self._rrf(result_lists, weights) if result_lists else []
        return [
            self._role_output_row(by_id[row_id], include_attributes, score, "hybrid")
            for row_id, score in fused[:top_k]
            if row_id in by_id
        ]

    def summary_search_rows(
        self,
        payload: dict[str, Any],
        people_filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> list[dict[str, Any]]:
        if not self.namespace_exists("summaries"):
            return []
        rows = self._filtered_rows("summaries", None)
        if people_filters is not None:
            eligible = {
                str(row.get("base_id") or row.get("person_id"))
                for row in self._filtered_rows("people", people_filters)
                if row.get("base_id") or row.get("person_id")
            }
            if not eligible:
                return []
            rows = [row for row in rows if str(row.get("base_id") or row.get("person_id") or row.get("id")) in eligible]
        if payload.get("tech_skills"):
            wanted = {str(value).lower() for value in payload.get("tech_skills") or []}
            rows = [row for row in rows if wanted & {str(value).lower() for value in self._value_list(row.get("tech_skills"))}]
        if not rows:
            return []

        result_lists: list[list[dict[str, Any]]] = []
        weights: list[float] = []
        table = self._table_for_namespace("summaries")
        bm25_field = self._best_text_field(table, ["summary_tokens", "word_tokens", "doc2query", "summary"])
        bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
        if bm25_field and bm25_queries:
            tokens: list[str] = []
            for query in bm25_queries:
                tokens.extend(self._word_query_tokens(query))
            ranked = [row for row, _score in self._bm25_rank(rows, bm25_field, list(dict.fromkeys(tokens)))[:top_k]]
            if ranked:
                result_lists.append(ranked)
                weights.append(1.0)
        if payload.get("query_embedding") is not None and "vector" in self._table_columns(table):
            ranked = [row for row, _score in self._vector_rank(rows, "vector", payload.get("query_embedding"))[:top_k]]
            if ranked:
                result_lists.append(ranked)
                weights.append(0.6)
        if self._uses_regex_patterns(payload):
            regex_ranked = self._regex_match_rank(
                rows,
                core_patterns=payload.get("role_core_patterns"),
                adjacent_patterns=payload.get("role_adjacent_patterns"),
                text_fields=["summary", "headline", "doc2query_text"],
            )
            if regex_ranked:
                result_lists.append(regex_ranked[:top_k])
                weights.append(0.5)
        if not result_lists:
            return []
        by_id = {self._row_id(row): row for ranked_rows in result_lists for row in ranked_rows}
        fused = self._rrf(result_lists, weights)
        out: list[dict[str, Any]] = []
        for row_id, score in fused[:top_k]:
            row = by_id[row_id]
            person_id = str(row.get("base_id") or row.get("person_id") or row.get("id"))
            item = self._project_dict(row, include_attributes)
            # Summary vertical matches are person-level in prod.  Do not attach
            # a position_id even if a future local_summaries schema happens to
            # carry one, otherwise downstream provenance would imply fake title
            # evidence for a summary-only hit.
            item.pop("position_id", None)
            item.update({
                "id": row_id,
                "person_id": person_id,
                "base_id": person_id,
                "position_id": None,
                "score": score,
                "retrieval_mode": "summary",
                "summary": row.get("summary"),
            })
            for key in ["has_core_regex", "has_adjacent_regex", "bucket"]:
                if key in row:
                    item[key] = row[key]
            out.append(item)
        return out

    def company_signal_company_ids(self, payload: dict[str, Any], top_k: int) -> list[str]:
        if not self.namespace_exists("company_signals"):
            return []
        rows = self._filtered_rows("company_signals", None)
        if not rows:
            return []
        table = self._table_for_namespace("company_signals")
        bm25_field = self._best_text_field(table, ["signal_tokens", "signals_tokens", "summary_tokens", "word_tokens", "signals_text", "summary", "doc2query_text"])
        bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
        result_lists: list[list[dict[str, Any]]] = []
        weights: list[float] = []
        if bm25_field and bm25_queries:
            tokens: list[str] = []
            for query in bm25_queries:
                tokens.extend(self._word_query_tokens(query))
            ranked = [row for row, _score in self._bm25_rank(rows, bm25_field, list(dict.fromkeys(tokens)))[:top_k]]
            if ranked:
                result_lists.append(ranked)
                weights.append(1.0)
        if payload.get("query_embedding") is not None and "vector" in self._table_columns(table):
            ranked = self._vector_rank_sql("company_signals", None, "vector", payload.get("query_embedding"), top_k)
            if ranked:
                result_lists.append(ranked)
                weights.append(0.6)
        if self._uses_regex_patterns(payload):
            regex_ranked = self._regex_match_rank(
                rows,
                core_patterns=payload.get("role_core_patterns"),
                adjacent_patterns=payload.get("role_adjacent_patterns"),
                text_fields=["signals_text", "summary", "doc2query_text"],
            )
            if regex_ranked:
                result_lists.append(regex_ranked[:top_k])
                weights.append(0.5)
        if not result_lists:
            return []
        by_id = {self._row_id(row): row for ranked_rows in result_lists for row in ranked_rows}
        fused = self._rrf(result_lists, weights)
        out: list[str] = []
        for row_id, _score in fused[:top_k]:
            row = by_id[row_id]
            company_id = row.get("company_id") or row.get("company_urn") or row.get("id")
            if company_id and str(company_id) not in out:
                out.append(str(company_id))
        return out

    def bm25_adjacency_rows(
        self,
        queries: list[str],
        filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> list[dict[str, Any]]:
        candidates = self._filtered_rows("people", filters)
        result_lists: list[list[dict[str, Any]]] = []
        query_indexes: dict[str, list[int]] = {}
        for query_index, query in enumerate(queries):
            phrase_tokens = self._phrase_query_tokens(str(query))
            word_tokens = self._word_query_tokens(str(query))
            if not phrase_tokens and not word_tokens:
                continue
            ranked = [row for row, _score in self._bm25_rank(candidates, "phrase_tokens", phrase_tokens)[:top_k]] if phrase_tokens else []
            if not ranked and word_tokens:
                ranked = [row for row, _score in self._bm25_rank(candidates, "word_tokens", word_tokens)[:top_k]]
            result_lists.append(ranked)
            for row in ranked:
                query_indexes.setdefault(self._row_id(row), []).append(query_index)
        fused = self._rrf(result_lists, [1.0] * len(result_lists))
        by_id = {self._row_id(row): row for rows in result_lists for row in rows}
        out: list[dict[str, Any]] = []
        for rank, (row_id, score) in enumerate(fused[:top_k], start=1):
            row = self._role_output_row(by_id[row_id], include_attributes, score, "company_adjacency_bm25")
            row["company_adjacency_rank"] = rank
            row["adjacency_query_indexes"] = query_indexes.get(row_id, [])
            out.append(row)
        return out

    def company_signal_rows(
        self,
        payload: dict[str, Any],
        people_filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> list[dict[str, Any]]:
        company_ids = self.company_signal_company_ids(payload, top_k)
        if not company_ids:
            return []
        company_filter = ("company_id", "In", company_ids)
        filters = company_filter if people_filters is None else ("And", [people_filters, company_filter])
        rows = self._filtered_rows_sql("people", filters, limit=top_k, order_by_id=True)
        return [self._role_output_row(row, include_attributes, 1.0, "company_signal") for row in rows]
