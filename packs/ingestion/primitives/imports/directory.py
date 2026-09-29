#!/usr/bin/env python3
"""The `directory.csv` row shape and pure people-row merge helpers.

The fan-in reads directory rows; source imports reuse the metadata unions.

Changelog:
  2026-09-28: the Deep Context review writer (`replace_directory_source_rows`
    and its row normalizer/ranker) is gone; realize exports reviewed identities
    straight from SQLite.
  2026-09-23 (typed rows): `normalized_directory_row` is the ONE boundary parse of
    a source/review row into the declared `DirectoryRow` and returns that instance;
    `merge_directory_rows` compares typed rows (rank from
    `directory_source_priority`) instead of dicts. The in-memory `_priority` /
    `_url_column` hint columns are gone: the sole producer's `_priority="100"` is
    what `directory_source_priority("deep_context_review", ...)` already returns,
    and no writer ever set `_url_column`. directory.csv bytes are unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import sys  # noqa: F401

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import parse_jsonish  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import row_model_for  # noqa: E402

DIRECTORY_COLUMNS = [
    "source",
    "source_key",
    "source_account",
    "source_id",
    "source_channels",
    "status",
    "email",
    "phone",
    "name",
    "linkedin_url",
    "public_identifier",
    "confidence",
    "matched_name",
    "matched_headline",
    "evidence",
    "reasoning",
    "source_artifact",
    "updated_at",
]
# The declared row shape of `directory.csv`, generated FROM DIRECTORY_COLUMNS so
# field order stays the on-disk header order and the column list keeps one home.
DirectoryRow = row_model_for("DirectoryRow", DIRECTORY_COLUMNS)


def parse_confidence(value: Any, default: float = 0.0) -> float:
    raw = str(value or "").strip().lower()
    if raw in {"high", "confirmed", "exact"}:
        return 0.95
    if raw in {"medium", "med"}:
        return 0.8
    if raw == "low":
        return 0.5
    try:
        parsed = float(raw)
        return parsed / 100.0 if parsed > 1 else parsed
    except ValueError:
        return default


def merge_jsonish_lists(current: str, incoming: str) -> str:
    values: list[str] = []
    for value in (current, incoming):
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            values.extend(str(item) for item in parsed if str(item).strip())
        else:
            values.extend(part.strip() for part in str(value).split(",") if part.strip())
    return json.dumps(sorted(set(values)), ensure_ascii=False) if values else ""


def union_alias_list(current: str, incoming: str, primary_current: str = "", primary_incoming: str = "") -> str:
    """Set-union an all_emails/all_phones column, preserving first-seen order.

    Distinct work emails that resolve to the same LinkedIn person accumulate
    here rather than overwriting each other. The matching
    primary_email/primary_phone values are folded in so a single-email row that
    only populated primary_* still contributes its address to the union.
    """
    seen: list[str] = []
    for value in (primary_current, primary_incoming):
        value = (value or "").strip()
        if value and value not in seen:
            seen.append(value)
    for blob in (current, incoming):
        parsed = parse_jsonish(blob, None)
        values = parsed if isinstance(parsed, list) else [part for part in re.split(r"[,;]", str(blob or "")) if part.strip()]
        for value in values:
            value = str(value).strip()
            if value and value not in seen:
                seen.append(value)
    return json.dumps(seen, ensure_ascii=False) if seen else ""
