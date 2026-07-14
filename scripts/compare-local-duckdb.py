#!/usr/bin/env python3
"""Compare two local-search DuckDB artifacts for behavioral equivalence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PRIMITIVES = ROOT / "packs/search/primitives/local"
sys.path.insert(0, str(LOCAL_PRIMITIVES))

from local_duckdb_store import LocalDuckDBSearchStore  # noqa: E402


NAMESPACE_TABLES = dict(LocalDuckDBSearchStore.NAMESPACE_TABLES)
PROJECTION_FIELDS = {
    "people": ["position_id", "person_id", "base_id", "position_title", "company_id"],
    "summaries": ["person_id", "base_id", "summary", "tech_skills"],
    "company_signals": ["company_id", "company_urn", "summary"],
    "education": ["person_id", "base_id", "canonical_education_id", "school_name"],
    "schools": ["canonical_education_id", "school_name", "person_count"],
    "companies": ["company_urn", "company_name", "website_domain", "stage"],
}


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def canonical_value(value: Any, *, logical_json: bool = False) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        if logical_json and isinstance(value, str):
            text = value.strip()
            if text and text[0] in "[{" and text[-1] in "]}":
                try:
                    return canonical_value(json.loads(text), logical_json=True)
                except json.JSONDecodeError:
                    pass
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return 0.0 if value == 0.0 else value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, (date, datetime, time)):
        return {"$datetime": value.isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": bytes(value).hex()}
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda pair: str(pair[0]))
        if logical_json:
            items = [(key, item) for key, item in items if item is not None]
        return {str(key): canonical_value(item, logical_json=logical_json) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item, logical_json=logical_json) for item in value]
    if hasattr(value, "tolist"):
        return canonical_value(value.tolist(), logical_json=logical_json)
    return {"$value": str(value), "$type": type(value).__name__}


def canonical_json(value: Any, *, logical_json: bool = False) -> bytes:
    return json.dumps(
        canonical_value(value, logical_json=logical_json),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def table_inventory(con: Any) -> list[str]:
    rows = con.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main'
          AND table_type = 'BASE TABLE'
          AND table_name LIKE 'local\\_%' ESCAPE '\\'
        ORDER BY table_name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def table_schema(con: Any, table: str) -> list[dict[str, Any]]:
    return [
        {
            "name": str(row[1]),
            "type": str(row[2]),
            "not_null": bool(row[3]),
            "default": canonical_value(row[4]),
            "primary_key": bool(row[5]),
        }
        for row in con.execute(f"PRAGMA table_info({qident(table)})").fetchall()
    ]


def table_content_checksum(con: Any, table: str, *, fetch_size: int = 128) -> tuple[int, str]:
    cursor = con.execute(f"SELECT * FROM {qident(table)}")
    columns = [str(desc[0]) for desc in cursor.description or []]
    row_hashes: list[bytes] = []
    row_count = 0
    while True:
        batch = cursor.fetchmany(fetch_size)
        if not batch:
            break
        for row in batch:
            payload = {column: value for column, value in zip(columns, row)}
            row_hashes.append(hashlib.sha256(canonical_json(payload, logical_json=True)).digest())
            row_count += 1
    digest = hashlib.sha256()
    for row_hash in sorted(row_hashes):
        digest.update(row_hash)
    return row_count, digest.hexdigest()


def table_columns(con: Any, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({qident(table)})").fetchall()}


def row_order_sql(columns: set[str]) -> str:
    for field in ("id", "position_id", "person_id", "base_id", "company_urn", "canonical_education_id"):
        if field in columns:
            return f"CAST({qident(field)} AS VARCHAR)"
    return "CAST(rowid AS VARCHAR)"


def first_operator_id(con: Any, table: str) -> str | None:
    columns = table_columns(con, table)
    if "allowed_operator_ids" not in columns:
        return None
    row = con.execute(
        f"""
        SELECT allowed_operator_ids
        FROM {qident(table)}
        WHERE allowed_operator_ids IS NOT NULL AND len(allowed_operator_ids) > 0
        ORDER BY {row_order_sql(columns)}
        LIMIT 1
        """
    ).fetchone()
    return str(row[0][0]) if row and row[0] else None


def first_vector(con: Any, table: str) -> list[float] | None:
    columns = table_columns(con, table)
    if "vector" not in columns:
        return None
    row = con.execute(
        f"SELECT vector FROM {qident(table)} WHERE vector IS NOT NULL AND len(vector) > 0 ORDER BY {row_order_sql(columns)} LIMIT 1"
    ).fetchone()
    return [float(value) for value in row[0]] if row and row[0] else None


def build_query_corpus(con: Any, inventory: set[str]) -> list[dict[str, Any]]:
    corpus: list[dict[str, Any]] = []
    for namespace, table in sorted(NAMESPACE_TABLES.items()):
        if table not in inventory:
            continue
        corpus.append({"name": f"{namespace}:first", "kind": "filter", "namespace": namespace, "filters": None})
        operator_id = first_operator_id(con, table)
        if operator_id:
            corpus.append(
                {
                    "name": f"{namespace}:operator",
                    "kind": "filter",
                    "namespace": namespace,
                    "filters": ("allowed_operator_ids", "ContainsAny", [operator_id]),
                }
            )
        vector = first_vector(con, table)
        if vector:
            corpus.append({"name": f"{namespace}:knn", "kind": "knn", "namespace": namespace, "vector": vector})
    return corpus


def query_row_payload(row: Any) -> dict[str, Any]:
    return {"id": str(row.id), **dict(row.model_extra)}


def normalize_equal_id_ties(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ignore physical row order only within adjacent equal-id result ties."""
    normalized: list[dict[str, Any]] = []
    start = 0
    while start < len(rows):
        end = start + 1
        row_id = str(rows[start].get("id") or "")
        while end < len(rows) and str(rows[end].get("id") or "") == row_id:
            end += 1
        normalized.extend(sorted(rows[start:end], key=canonical_json))
        start = end
    return normalized


def execute_query_corpus(db_path: Path, corpus: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    store = LocalDuckDBSearchStore(str(db_path), read_only=True)
    output: dict[str, dict[str, Any]] = {}
    try:
        for probe in corpus:
            namespace = str(probe["namespace"])
            include = PROJECTION_FIELDS.get(namespace, [])
            try:
                if probe["kind"] == "knn":
                    response = store.namespace(namespace).query(
                        rank_by=("vector", "kNN", probe["vector"]),
                        filters=None,
                        top_k=10,
                        include_attributes=include,
                    )
                    rows = [query_row_payload(row) for row in response.rows]
                else:
                    rows = store.filter_only_rows_for_namespace(
                        namespace,
                        probe.get("filters"),
                        include,
                        max_results=10,
                    )
                rows = normalize_equal_id_ties(rows)
                output[str(probe["name"])] = {
                    "count": len(rows),
                    "checksum": hashlib.sha256(canonical_json(rows)).hexdigest(),
                    "ids": [str(row.get("id") or "") for row in rows],
                }
            except Exception as exc:
                output[str(probe["name"])] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        store.conn.close()
    return output


def compare_databases(left_path: Path, right_path: Path) -> dict[str, Any]:
    import duckdb

    for path in (left_path, right_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    left = duckdb.connect(str(left_path), read_only=True)
    right = duckdb.connect(str(right_path), read_only=True)
    try:
        left_inventory = table_inventory(left)
        right_inventory = table_inventory(right)
        tables: dict[str, dict[str, Any]] = {}
        for table in sorted(set(left_inventory) | set(right_inventory)):
            left_present = table in left_inventory
            right_present = table in right_inventory
            left_schema = table_schema(left, table) if left_present else None
            right_schema = table_schema(right, table) if right_present else None
            left_count, left_checksum = table_content_checksum(left, table) if left_present else (None, None)
            right_count, right_checksum = table_content_checksum(right, table) if right_present else (None, None)
            tables[table] = {
                "match": left_present
                and right_present
                and {column["name"] for column in left_schema or []} == {column["name"] for column in right_schema or []}
                and left_count == right_count
                and left_checksum == right_checksum,
                "schema_match": left_schema == right_schema,
                "columns_match": left_present
                and right_present
                and {column["name"] for column in left_schema or []} == {column["name"] for column in right_schema or []},
                "row_count": {"left": left_count, "right": right_count},
                "content_checksum": {"left": left_checksum, "right": right_checksum},
                "content_normalization": "logical-json-v1",
            }

        corpus = build_query_corpus(left, set(left_inventory))
        left_queries = execute_query_corpus(left_path, corpus)
        right_queries = execute_query_corpus(right_path, corpus)
        query_results = {
            name: {"match": left_queries.get(name) == right_queries.get(name), "left": left_queries.get(name), "right": right_queries.get(name)}
            for name in sorted(set(left_queries) | set(right_queries))
        }
        inventory_match = left_inventory == right_inventory
        tables_match = all(item["match"] for item in tables.values())
        physical_schema_match = all(item["schema_match"] for item in tables.values())
        queries_match = all(item["match"] for item in query_results.values())
        ok = inventory_match and tables_match and queries_match
        return {
            "status": "ok" if ok else "mismatch",
            "match": ok,
            "left": str(left_path),
            "right": str(right_path),
            "inventory": {"match": inventory_match, "left": left_inventory, "right": right_inventory},
            "physical_schema_match": physical_schema_match,
            "tables": tables,
            "search": {"match": queries_match, "probes": query_results},
        }
    finally:
        left.close()
        right.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path, help="Also write the JSON report to this path")
    args = parser.parse_args()
    try:
        report = compare_databases(args.left.expanduser().resolve(), args.right.expanduser().resolve())
    except Exception as exc:
        report = {"status": "error", "match": False, "error": f"{type(exc).__name__}: {exc}"}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report.get("match") else 1


if __name__ == "__main__":
    raise SystemExit(main())
