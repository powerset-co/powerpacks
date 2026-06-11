"""Read-only agentic SQL access to the local search DuckDB.

This primitive is the sandbox for the agentic SQL search vertical: it opens
the local search index database strictly read-only, accepts a single
SELECT/WITH statement, and emits JSON rows. It exists so an agent (Claude,
Codex, etc.) can run relational/aggregate queries the structured filter DSL
cannot express — per-person aggregates, person-to-person overlap joins,
source-summary joins — without any write or multi-statement surface.

Subcommands:
  schema  - dump tables, columns, and row counts so the agent never guesses
  query   - run one SELECT/WITH statement with a row cap

No state is written anywhere; output is JSON on stdout only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = ".powerpacks/search-index/local-search.duckdb"
DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 5000
# Long numeric arrays (embedding vectors) are useless to an agent and blow up
# stdout; anything longer than this is summarized unless --raw is passed.
LIST_SUMMARY_THRESHOLD = 32
ALLOWED_LEADING_KEYWORDS = {"select", "with"}
LINE_COMMENT_RE = re.compile(r"--[^\n]*")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LEADING_KEYWORD_RE = re.compile(r"^[A-Za-z]+")


class QueryGuardError(ValueError):
    """The submitted SQL is outside the read-only SELECT contract."""


def resolve_db_path(explicit: str | None) -> Path:
    candidate = explicit or os.getenv("POWERPACKS_LOCAL_SEARCH_DB") or DEFAULT_DB_PATH
    return Path(candidate)


def validate_select_only(sql: str) -> str:
    """Return the bare statement, or raise QueryGuardError.

    The connection is already opened read-only; this guard additionally
    rejects multi-statement input and anything that is not a single
    SELECT/WITH so file-writing statements (COPY TO, EXPORT, ATTACH) never
    reach the engine.
    """
    body = BLOCK_COMMENT_RE.sub(" ", sql)
    body = LINE_COMMENT_RE.sub(" ", body)
    body = body.strip().rstrip(";").strip()
    if not body:
        raise QueryGuardError("empty SQL statement")
    if ";" in body:
        raise QueryGuardError("multiple SQL statements are not allowed; submit one SELECT/WITH statement")
    match = LEADING_KEYWORD_RE.match(body)
    keyword = match.group(0).lower() if match else ""
    if keyword not in ALLOWED_LEADING_KEYWORDS:
        raise QueryGuardError(f"only SELECT/WITH statements are allowed (got leading keyword {keyword!r})")
    return body


def _json_safe(value: Any, *, raw: bool) -> Any:
    if isinstance(value, (list, tuple)):
        if not raw and len(value) > LIST_SUMMARY_THRESHOLD and all(isinstance(item, (int, float)) for item in value):
            return f"<{len(value)} numeric values omitted; pass --raw to include>"
        return [_json_safe(item, raw=raw) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item, raw=raw) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def connect_read_only(db_path: Path) -> Any:
    try:
        import duckdb
    except ModuleNotFoundError as exc:
        raise RuntimeError("duckdb is required; run bin/setup-python") from exc
    if not db_path.exists():
        raise FileNotFoundError(f"local search DuckDB not found at {db_path}; run $build-local-search-index first")
    return duckdb.connect(str(db_path), read_only=True)


def run_schema(conn: Any) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    table_names = [row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name").fetchall()]
    for table_name in table_names:
        columns = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
            [table_name],
        ).fetchall()
        row_count = conn.execute(f'SELECT count(*) FROM "{table_name.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
        tables.append(
            {
                "table": table_name,
                "row_count": int(row_count),
                "columns": [{"name": name, "type": dtype} for name, dtype in columns],
            }
        )
    return {"status": "ok", "tables": tables}


def run_query(conn: Any, sql: str, *, max_rows: int, raw: bool) -> dict[str, Any]:
    statement = validate_select_only(sql)
    cursor = conn.execute(statement)
    columns = [desc[0] for desc in cursor.description]
    fetched = cursor.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [
        {column: _json_safe(value, raw=raw) for column, value in zip(columns, row)}
        for row in fetched[:max_rows]
    ]
    return {
        "status": "ok",
        "columns": columns,
        "row_count": len(rows),
        "truncated": truncated,
        "max_rows": max_rows,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only SQL queries against the local search DuckDB")
    parser.add_argument("--db", help=f"DuckDB path (default: $POWERPACKS_LOCAL_SEARCH_DB or {DEFAULT_DB_PATH})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("schema", help="List tables, columns, and row counts")

    query_parser = subparsers.add_parser("query", help="Run one SELECT/WITH statement")
    sql_group = query_parser.add_mutually_exclusive_group(required=True)
    sql_group.add_argument("--sql", help="SQL statement to run")
    sql_group.add_argument("--sql-file", help="Path to a file containing the SQL statement")
    query_parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help=f"Row cap (default {DEFAULT_MAX_ROWS}, hard max {HARD_MAX_ROWS})")
    query_parser.add_argument("--raw", action="store_true", help="Include long numeric arrays (e.g. embedding vectors) verbatim")

    args = parser.parse_args(argv)

    try:
        conn = connect_read_only(resolve_db_path(args.db))
    except (FileNotFoundError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1

    try:
        if args.command == "schema":
            payload = run_schema(conn)
        else:
            sql = args.sql if args.sql is not None else Path(args.sql_file).read_text(encoding="utf-8")
            max_rows = max(1, min(int(args.max_rows), HARD_MAX_ROWS))
            payload = run_query(conn, sql, max_rows=max_rows, raw=bool(args.raw))
    except QueryGuardError as exc:
        print(json.dumps({"status": "error", "error_kind": "guard", "error": str(exc)}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "error", "error_kind": "sql", "error": str(exc)}))
        return 3
    finally:
        conn.close()

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
