"""Small local-file helpers for indexing scaffolding.

These helpers are intentionally stdlib-only and perform no network operations.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from packs.shared.csv_io import CsvIO  # noqa: E402


def ensure_parent(path: str | Path) -> Path:
    """Create the parent directory for *path* and return it as a Path."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a UTF-8/UTF-8-BOM CSV into dictionaries."""

    with Path(path).open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(CsvIO.dict_reader(handle))


def csv_header(path: str | Path) -> list[str]:
    """Return the CSV header row, or an empty list for an empty file."""

    with Path(path).open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return next(CsvIO.reader(handle), [])


def write_csv(path: str | Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> Path:
    """Write dictionaries to a CSV using exactly *fieldnames* order."""

    out = ensure_parent(path)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    return out


def write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write JSON objects as newline-delimited UTF-8 JSON."""

    text = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    return atomic_write_text(path, text)


def append_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> Path:
    """Append JSON objects as newline-delimited UTF-8 JSON."""

    out = ensure_parent(path)
    with out.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read newline-delimited JSON objects, skipping blank lines."""

    return list(iter_jsonl(path))


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream newline-delimited JSON objects, skipping blank lines.

    Use instead of read_jsonl when rows are processed one at a time, so large
    files (e.g. embedding artifacts) are never fully resident.
    """

    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Atomically write UTF-8 text next to the destination file."""

    out = ensure_parent(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, payload: Any) -> Path:
    return atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
