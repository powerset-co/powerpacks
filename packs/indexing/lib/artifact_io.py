"""Streaming readers for JSONL and Parquet indexing artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator


def _qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def iter_artifact_rows(path: Path, columns: Iterable[str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield artifact rows without requiring pandas or PyArrow."""
    if path.suffix.lower() != ".parquet":
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)
        return

    import duckdb  # type: ignore

    selected = list(columns or [])
    projection = ", ".join(_qident(column) for column in selected) if selected else "*"
    con = duckdb.connect(":memory:")
    try:
        con.execute("SET enable_progress_bar=false")
        cursor = con.execute(f"SELECT {projection} FROM read_parquet(?)", [str(path)])
        names = [str(item[0]) for item in cursor.description]
        while True:
            batch = cursor.fetchmany(2048)
            if not batch:
                break
            for values in batch:
                yield dict(zip(names, values))
    finally:
        con.close()


def artifact_id_set(path: Path, key: str, *, require_vector: bool = False) -> set[str]:
    if not path.exists():
        return set()
    vector_fields = ("vector", "embedding", "dense_embedding")
    if path.suffix.lower() != ".parquet":
        out: set[str] = set()
        for row in iter_artifact_rows(path):
            value = str(row.get(key) or "").strip()
            vector = next((row.get(field) for field in vector_fields if field in row), None)
            if value and (not require_vector or isinstance(vector, list) and bool(vector)):
                out.add(value)
        return out

    import duckdb  # type: ignore

    con = duckdb.connect(":memory:")
    try:
        con.execute("SET enable_progress_bar=false")
        columns = {
            str(row[0])
            for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
        }
        if key not in columns:
            return set()
        predicates = [f"NULLIF(trim(CAST({_qident(key)} AS VARCHAR)), '') IS NOT NULL"]
        if require_vector:
            vector_field = next((field for field in vector_fields if field in columns), None)
            if not vector_field:
                return set()
            predicates.append(f"coalesce(len({_qident(vector_field)}), 0) > 0")
        rows = con.execute(
            f"SELECT DISTINCT CAST({_qident(key)} AS VARCHAR) FROM read_parquet(?) WHERE {' AND '.join(predicates)}",
            [str(path)],
        ).fetchall()
        return {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}
    finally:
        con.close()
