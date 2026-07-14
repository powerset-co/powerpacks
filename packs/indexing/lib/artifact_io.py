"""Streaming readers for JSONL and Parquet indexing artifacts."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


PARQUET_WRITE_BATCH_SIZE = 2048


def _qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qliteral(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _combine_types(current: str, incoming: str) -> str:
    if current == incoming:
        return current
    if current.endswith("[]") and incoming.endswith("[]"):
        return _combine_types(current[:-2], incoming[:-2]) + "[]"
    if {current, incoming} <= {"BIGINT", "DOUBLE"}:
        return "DOUBLE"
    return "VARCHAR"


def _single_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, (bytes, bytearray)):
        return "BLOB"
    if isinstance(value, dict):
        return "JSON"
    if isinstance(value, (list, tuple)):
        return _value_type([item for item in value if item is not None]) + "[]"
    return "VARCHAR"


def _value_type(values: Sequence[Any], *, force_float_array: bool = False) -> str:
    if force_float_array:
        return "FLOAT[]"
    present = [value for value in values if value is not None]
    if not present:
        return "VARCHAR"
    informative = [
        value for value in present
        if not isinstance(value, (list, tuple)) or bool(value)
    ]
    candidates = informative or present
    inferred = _single_value_type(candidates[0])
    for value in candidates[1:]:
        inferred = _combine_types(inferred, _single_value_type(value))
    return inferred


def _coerce_value(value: Any, sql_type: str) -> Any:
    if value is None:
        return None
    if sql_type == "JSON":
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if sql_type == "VARCHAR":
        return value if isinstance(value, str) else str(value)
    if sql_type.endswith("[]"):
        item_type = sql_type[:-2]
        return [_coerce_value(item, item_type) for item in value]
    return value


def _merge_types(current: str, incoming: str) -> str:
    return _combine_types(current, incoming)


def _has_type_evidence(values: Sequence[Any]) -> bool:
    return any(
        value is not None and (not isinstance(value, (list, tuple)) or bool(value))
        for value in values
    )


def _arrow_type(sql_type: str):
    import pyarrow as pa  # type: ignore

    if sql_type.endswith("[]"):
        return pa.list_(_arrow_type(sql_type[:-2]))
    return {
        "BOOLEAN": pa.bool_(),
        "INTEGER": pa.int32(),
        "BIGINT": pa.int64(),
        "DOUBLE": pa.float64(),
        "FLOAT": pa.float32(),
        "BLOB": pa.binary(),
        "JSON": pa.string(),
        "VARCHAR": pa.string(),
    }[sql_type]


def write_parquet_rows(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    float_array_fields: Iterable[str] = (),
    schema: Mapping[str, str] | None = None,
) -> int:
    """Atomically write rows as bounded Arrow batches and native Parquet."""
    import duckdb  # type: ignore
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore

    float_arrays = set(float_array_fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=str(path.parent)))
    tmp = temp_dir / "merged.parquet"
    fields = list(schema or {})
    types = dict(schema or {})
    inferred_with_evidence = {field: True for field in (schema or {})}
    row_count = 0
    part_paths: list[Path] = []

    def update_schema(batch: list[dict[str, Any]]) -> None:
        batch_fields = list(fields)
        for row in batch:
            for field in row:
                if field not in batch_fields:
                    batch_fields.append(field)
        observed = {
            field: [row[field] for row in batch if field in row and row[field] is not None]
            for field in batch_fields
        }
        inferred = {
            field: _value_type(observed[field], force_float_array=field in float_arrays)
            for field in batch_fields
        }
        has_evidence = {field: _has_type_evidence(observed[field]) for field in batch_fields}
        if not fields:
            fields.extend(batch_fields)
        for field in batch_fields:
            if field not in fields:
                fields.append(field)
                types[field] = inferred[field]
                inferred_with_evidence[field] = has_evidence[field]
                continue
            if field not in types:
                types[field] = inferred[field]
                inferred_with_evidence[field] = has_evidence[field]
                continue
            if not has_evidence[field]:
                continue
            if not inferred_with_evidence.get(field, False) and field not in (schema or {}):
                types[field] = inferred[field]
            elif field not in (schema or {}):
                types[field] = _merge_types(types[field], inferred[field])
            inferred_with_evidence[field] = True
        if not fields:
            raise ValueError("Parquet output requires at least one column")

    try:
        iterator = iter(records)
        while True:
            batch: list[dict[str, Any]] = []
            for _ in range(PARQUET_WRITE_BATCH_SIZE):
                try:
                    batch.append(dict(next(iterator)))
                except StopIteration:
                    break
            if not batch:
                break
            update_schema(batch)
            part = temp_dir / f"part-{len(part_paths) + 1:06d}.parquet"
            table = pa.Table.from_arrays(
                [
                    pa.array(
                        [_coerce_value(row.get(field), types[field]) for row in batch],
                        type=_arrow_type(types[field]),
                    )
                    for field in fields
                ],
                names=fields,
            )
            pq.write_table(table, part, compression="zstd", row_group_size=16384)
            part_paths.append(part)
            row_count += len(batch)

        if not part_paths:
            update_schema([])
            part = temp_dir / "part-000001.parquet"
            pq.write_table(
                pa.Table.from_arrays(
                    [pa.array([], type=_arrow_type(types[field])) for field in fields],
                    names=fields,
                ),
                part,
                compression="zstd",
            )
            part_paths.append(part)

        con = duckdb.connect(":memory:")
        try:
            con.execute("SET enable_progress_bar=false")
            con.execute(f"SET threads={int(os.getenv('POWERPACKS_CACHE_THREADS', '8'))}")
            sources = "[" + ", ".join(_qliteral(part) for part in part_paths) + "]"
            projection = ", ".join(
                f"CAST({_qident(field)} AS {types[field]}) AS {_qident(field)}"
                for field in fields
            )
            con.execute(
                f"COPY (SELECT {projection} FROM read_parquet({sources}, union_by_name=true)) "
                f"TO {_qliteral(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 16384)"
            )
        finally:
            con.close()
        os.replace(tmp, path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return row_count


def merge_parquet_chunks(
    chunks: Sequence[Path],
    output: Path,
    *,
    id_field: str,
    empty_schema: Mapping[str, str],
) -> int:
    """Deduplicate ordered Parquet chunks (first id wins) into one artifact."""
    if not chunks:
        return write_parquet_rows(output, [], schema=empty_schema)

    import duckdb  # type: ignore

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp.parquet")
    sources = "[" + ", ".join(_qliteral(path) for path in chunks) + "]"
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            f"""
            COPY (
                WITH source AS (
                    SELECT *, row_number() OVER () AS __source_order
                    FROM read_parquet({sources}, union_by_name=true)
                ), ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY {_qident(id_field)} ORDER BY __source_order
                    ) AS __keep
                    FROM source
                )
                SELECT * EXCLUDE (__source_order, __keep)
                FROM ranked
                WHERE __keep = 1
                ORDER BY {_qident(id_field)}
            ) TO {_qliteral(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        count = int(con.execute(
            f"SELECT count(DISTINCT {_qident(id_field)}) FROM read_parquet({sources}, union_by_name=true)"
        ).fetchone()[0])
        os.replace(tmp, output)
        return count
    finally:
        con.close()
        tmp.unlink(missing_ok=True)


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
