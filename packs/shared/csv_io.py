"""Central CSV I/O for Powerpacks.

Every CSV read in this repo should go through :class:`CsvIO` so a single fix
applies to all call sites. The reason this exists: rows can carry very large
embedded JSON fields (``rapidapi_response``, ``harmonic_response``,
``work_experiences``) that blow past Python's default 131072-byte
``csv.field_size_limit`` and raise ``_csv.Error: field larger than field
limit``. Routing reads through here raises that limit once, idempotently, for
the whole process — so we never have to remember to do it per file again.

Drop-in usage (mirrors the stdlib signatures exactly)::

    from packs.shared.csv_io import CsvIO

    with open(path, newline="") as f:
        for row in CsvIO.dict_reader(f):
            ...

    rows = list(CsvIO.reader(handle))

Whole-file convenience wrappers live here too: :meth:`CsvIO.read_dict_rows` and
:meth:`CsvIO.write_dict_rows` are the canonical replacements for the many
per-module ``read_csv``/``write_csv`` copies (utf-8-sig read, utf-8
extrasaction-ignore write, default CRLF, no fingerprint). The fingerprinted
LF writer that skips unchanged rewrites stays in ``discover/common.py``.

Changelog:
  2026-07-23 (audit): added read_dict_rows / write_dict_rows so the ingestion
    stages stop each defining a byte-identical local read_csv/write_csv.
  2026-07-23 (audit): added write_dict_rows_strict (the loud extrasaction="raise"
    writer) and the generic upsert-by-key helpers (upsert_dict_rows +
    _upsert_key/_project_row/_merge_row), absorbed from
    discover/gmail/discover_engine.py's local write_csv/csv_key/normalize_csv_row/
    merge_csv_row/upsert_csv. Byte output unchanged for current callers.
  2026-07-23 (audit class-sharing): added the priority-key upsert family
    (read_dict_rows_normalized + _priority_key + upsert_dict_rows_priority),
    absorbed from linkedin/network_import.py's local read_csv_rows/row_key/
    upsert_csv. Distinct from upsert_dict_rows: keyed by the FIRST non-empty
    preferred field instead of the composite of all key fields, and preserves
    existing row order instead of re-sorting. Byte output unchanged.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any


class CsvIO:
    """Process-wide CSV facade. All methods are classmethods; never instantiated."""

    _limit_raised = False

    @classmethod
    def ensure_field_limit(cls) -> None:
        """Raise ``csv.field_size_limit`` to the platform maximum, once per process."""
        if cls._limit_raised:
            return
        limit = sys.maxsize
        while True:
            try:
                csv.field_size_limit(limit)
            except OverflowError:
                limit //= 2
                continue
            cls._limit_raised = True
            return

    @classmethod
    def dict_reader(cls, f: Any, *args: Any, **kwargs: Any) -> "csv.DictReader":
        """Drop-in for ``csv.DictReader`` with the field-size guard applied."""
        cls.ensure_field_limit()
        return csv.DictReader(f, *args, **kwargs)

    @classmethod
    def reader(cls, f: Any, *args: Any, **kwargs: Any):
        """Drop-in for ``csv.reader`` with the field-size guard applied."""
        cls.ensure_field_limit()
        return csv.reader(f, *args, **kwargs)

    @classmethod
    def dict_writer(cls, f: Any, *args: Any, **kwargs: Any) -> "csv.DictWriter":
        """Drop-in for ``csv.DictWriter`` (writes are unaffected by the limit;
        provided so all CSV traffic can share one facade)."""
        return csv.DictWriter(f, *args, **kwargs)

    @classmethod
    def writer(cls, f: Any, *args: Any, **kwargs: Any):
        """Drop-in for ``csv.writer``."""
        return csv.writer(f, *args, **kwargs)

    @classmethod
    def read_dict_rows(cls, path: Path) -> list[dict[str, Any]]:
        """Read a whole CSV file into a list of dict rows.

        Canonical replacement for the per-module ``read_csv(path)`` copies.
        Opens ``path`` with ``newline=""``, ``encoding="utf-8-sig"``
        (BOM-tolerant) and ``errors="replace"``, applies the process-wide
        field-size guard, and returns ``list(csv.DictReader(...))`` verbatim —
        a short row keeps ``DictReader``'s ``None`` key and any ``None`` values
        (no normalization). For the (fields, normalized-rows) tuple shape use
        ``discover/common.py:read_csv_rows`` instead."""
        cls.ensure_field_limit()
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            return list(csv.DictReader(handle))

    @classmethod
    def write_dict_rows(cls, path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        r"""Write dict rows to a CSV file — byte-for-byte like the per-module
        ``write_csv(path, fieldnames, rows)`` copies it replaces.

        Creates parent directories, opens ``path`` with ``newline=""`` and
        ``encoding="utf-8"``, then writes a header row plus one row per input
        through ``csv.DictWriter(extrasaction="ignore")`` using the stdlib
        DEFAULT ``\r\n`` line terminator. Every row is projected onto exactly
        ``fieldnames`` (``{field: row.get(field, "")}``) so extra keys are
        dropped and missing keys become empty strings. Always rewrites — there
        is NO fingerprint/no-op guard; for the fingerprinted ``\n`` writer that
        skips unchanged rewrites use ``discover/common.py:write_csv_rows``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    @classmethod
    def write_dict_rows_strict(cls, path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        r"""Write dict rows to a CSV, RAISING on any key outside ``fieldnames``.

        Like :meth:`write_dict_rows` (mkdir parents, ``newline=""``,
        ``encoding="utf-8"``, header row, stdlib DEFAULT ``\r\n`` terminator) but
        keeps ``csv.DictWriter``'s default ``extrasaction="raise"`` and passes
        ``rows`` straight to ``writerows`` with NO per-row projection: a row
        carrying a key not in ``fieldnames`` is a loud failure (``ValueError``),
        and a missing key fills from ``restval`` (``""``). This is the strict
        loud-failure guard the discover engine relied on — use
        :meth:`write_dict_rows` when extra keys should be silently dropped."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @classmethod
    def read_dict_rows_normalized(cls, path: Path) -> list[dict[str, str]]:
        """Read a CSV into normalized dict rows: a missing file returns ``[]``,
        the short-row ``None`` key is dropped, and ``None`` values become ``""``.
        The lenient counterpart to :meth:`read_dict_rows` for upsert-style
        callers that re-read their own prior output."""
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            return [{str(key): value or "" for key, value in row.items() if key is not None} for row in cls.dict_reader(handle)]

    @classmethod
    def _priority_key(cls, row: dict[str, Any], preferred: list[str]) -> str:
        """The FIRST non-empty preferred field as ``field:value`` (lowercased);
        falls back to the sorted whole-row JSON so keyless rows never collide."""
        for field in preferred:
            value = str(row.get(field) or "").strip().lower()
            if value:
                return f"{field}:{value}"
        return json.dumps({k: str(row.get(k, "") or "") for k in sorted(row)}, sort_keys=True)

    @classmethod
    def upsert_dict_rows_priority(cls, path: Path, fieldnames: list[str], rows: list[dict[str, Any]], preferred_keys: list[str]) -> list[dict[str, Any]]:
        """Merge ``rows`` into the CSV at ``path`` keyed by :meth:`_priority_key`
        over ``preferred_keys``, preserving existing row order and appending new
        rows; non-empty incoming values overwrite. Returns the merged rows.

        Contrast with :meth:`upsert_dict_rows`, which keys on the composite of
        ALL key fields and re-sorts output — this variant treats the preferred
        fields as identity fallbacks (public_identifier beats linkedin_url beats
        email) so restated rows with newly-filled fields still match."""
        ordered: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for existing in cls.read_dict_rows_normalized(path):
            key = cls._priority_key(existing, preferred_keys)
            by_key[key] = existing
            ordered.append(existing)
        for row in rows:
            key = cls._priority_key(row, preferred_keys)
            normalized = {field: row.get(field, "") for field in fieldnames}
            if key in by_key:
                target = by_key[key]
                for field, value in normalized.items():
                    if value != "":
                        target[field] = value
                    else:
                        target.setdefault(field, "")
            else:
                by_key[key] = normalized
                ordered.append(normalized)
        cls.write_dict_rows(path, fieldnames, ordered)
        return ordered

    @classmethod
    def _upsert_key(cls, row: dict[str, Any], fields: list[str]) -> tuple[str, ...] | None:
        """Build a normalized upsert key from ``fields``; ``None`` when all blank."""
        key = tuple(str(row.get(field) or "").strip().lower() for field in fields)
        return key if any(key) else None

    @classmethod
    def _project_row(cls, fieldnames: list[str], row: dict[str, Any]) -> dict[str, Any]:
        """Project a row onto exactly ``fieldnames`` (missing keys -> '')."""
        return {field: row.get(field, "") for field in fieldnames}

    @classmethod
    def _merge_row(cls, fieldnames: list[str], existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """Overlay non-empty ``incoming`` values onto ``existing``; ``added_at`` is
        first-write-wins (an existing non-empty ``added_at`` is never overwritten)."""
        merged = cls._project_row(fieldnames, existing)
        for field in fieldnames:
            value = incoming.get(field, "")
            if value in ("", None):
                continue
            if field == "added_at" and merged.get(field):
                continue
            merged[field] = value
        return merged

    @classmethod
    def upsert_dict_rows(cls, path: Path, fieldnames: list[str], rows: list[dict[str, Any]], key_fields: list[str]) -> dict[str, int]:
        """Merge ``rows`` into an existing CSV at ``path`` by ``key_fields``,
        preserving existing rows the incoming set does not restate; return upsert
        counters (``incoming``/``existing``/``written``/``preserved_existing``/
        ``upserted``).

        Rows keyed by :meth:`_upsert_key` are overlaid via :meth:`_merge_row`;
        rows with an all-blank key are appended verbatim (keyless). Output is
        written with :meth:`write_dict_rows_strict` (the strict extra-key guard).
        Generic dict-row upsert-by-key logic — absorbed from the discover engine's
        local ``upsert_csv``."""
        existing_rows = cls.read_dict_rows(path) if path.exists() else []
        keyed: dict[tuple[str, ...], dict[str, Any]] = {}
        keyless_existing: list[dict[str, Any]] = []
        for row in existing_rows:
            normalized = cls._project_row(fieldnames, row)
            key = cls._upsert_key(normalized, key_fields)
            if key is None:
                keyless_existing.append(normalized)
                continue
            keyed[key] = cls._merge_row(fieldnames, keyed[key], normalized) if key in keyed else normalized

        incoming_keys: set[tuple[str, ...]] = set()
        for row in rows:
            normalized = cls._project_row(fieldnames, row)
            key = cls._upsert_key(normalized, key_fields)
            if key is None:
                keyless_existing.append(normalized)
                continue
            incoming_keys.add(key)
            keyed[key] = cls._merge_row(fieldnames, keyed[key], normalized) if key in keyed else normalized

        output_rows = [keyed[key] for key in sorted(keyed)]
        output_rows.extend(keyless_existing)
        cls.write_dict_rows_strict(path, fieldnames, output_rows)
        return {
            "incoming": len(rows),
            "existing": len(existing_rows),
            "written": len(output_rows),
            "preserved_existing": len([key for key in keyed if key not in incoming_keys]),
            "upserted": len(incoming_keys),
        }


# Raise the limit at import time too, so merely importing this module protects
# any process that pulls it in (directly or transitively), even before the first
# reader is constructed.
CsvIO.ensure_field_limit()
