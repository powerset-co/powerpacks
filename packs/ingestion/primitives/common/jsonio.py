#!/usr/bin/env python3
"""JSON / hashing I/O helpers shared across every ingestion primitive.

The single home for the small serialization + hashing utilities that used to be
copy-pasted into ~a dozen leaf primitives. Behavior is byte-identical to the
copies it replaces:

- `now_iso` — UTC, second precision, trailing `Z`.
- `emit` — pretty JSON to stdout (indent=2, sort_keys) for a primitive's final
  result payload.
- `read_json` — safe read; returns `default` on missing file or decode error.
- `write_json` — mkdir parents, indent=2, sort_keys, single trailing newline.
- `read_jsonl` — newline-delimited JSON objects into a list; `[]` for a missing
  file, blank lines skipped, a malformed line raises.
- `write_jsonl` — mkdir parents, one JSON object per line, utf-8; returns the
  number of rows written.
- `parse_last_json` — return the LAST top-level JSON object emitted on a child's
  stdout (`{}` when none); the tolerant progress-then-result contract, which
  scans past undecodable stretches instead of stopping at the first one.
- `unique_strings` — order-preserving de-dup of a scalar/list into stripped
  non-empty strings.
- `sha256_file` — streaming SHA-256 of a file (fingerprint helper).
- `short_hash` — first `n` hex chars of a value's SHA-256 (stable ids). Callers
  pass their historical length so existing digests are unchanged.

Changelog:
  2026-07-24 (dedup): `parse_last_json` absorbed the divergent copy that lived
    in discover/messages/whatsapp_wacli.py. That copy's scan-forward recovery
    (on a decode error, jump to the next `{` instead of stopping) is now the
    canonical behavior — strictly more tolerant, and required for Go binaries
    that interleave log output with their JSON result. The fork's `dict | None`
    return was NOT promoted: `{}` stays the contract, and every former fork
    consumer already coerced a non-dict to `{}`.
  2026-07-24 (jsonl home): added the `read_jsonl` / `write_jsonl` pair. The repo
    carried 28 separate newline-delimited-JSON reader/writer definitions and no
    shared one; the message extractors now route here, and the remaining copies
    can collapse onto it. `write_jsonl` keeps `sort_keys=True` /
    `ensure_ascii=True` as defaults so the artifacts it replaced stay
    byte-identical, with both exposed as keyword overrides for the callers that
    write literal UTF-8.
  2026-07-23 (audit consolidation): created; absorbs the duplicated now_iso /
    emit / read_json / write_json / parse_last_json / unique_strings /
    sha256_file copies and the `sha`/`short_hash` truncated-digest helpers from
    discover/common, imports/common, twitter, enrich, merge_network_sources,
    the message primitives, and gmail/import_steps. One name per concept, no
    re-exports.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def now_iso() -> str:
    """Current UTC time as an RFC3339 `...Z` string at second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: Any) -> None:
    """Print a primitive's result payload as pretty, key-sorted JSON to stdout."""
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from `path`, returning `default` on a missing file or decode error."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Write `payload` as key-sorted JSON (indent 2, trailing newline), mkdir-ing parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read newline-delimited JSON objects into a list; `[]` for a missing file.

    Blank lines are skipped, so a file's trailing newline costs nothing. A
    malformed line raises `json.JSONDecodeError` — unlike `read_json`'s tolerant
    default-on-error contract, a half-written JSONL artifact should be loud
    rather than silently short.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def write_jsonl(
    path: Path,
    rows: Iterable[Any],
    *,
    sort_keys: bool = True,
    ensure_ascii: bool = True,
) -> int:
    """Write `rows` as newline-delimited JSON, mkdir-ing parents; returns the count.

    One compact JSON value per line, LF-terminated, utf-8. `rows` is consumed as
    an iterable, so generators stream instead of materializing. The defaults
    match the artifacts this replaced: `sort_keys` keeps reruns diff-stable and
    `ensure_ascii` escapes non-ASCII — pass `ensure_ascii=False` for the
    artifacts that keep literal UTF-8.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=sort_keys, ensure_ascii=ensure_ascii) + "\n")
            written += 1
    return written


def parse_last_json(stdout: str) -> dict[str, Any]:
    """Return the last top-level JSON object on a child's stdout, `{}` if none.

    Children print human progress lines and then a final JSON result; scanning
    for the last decodable dict tolerates that interleaving without a delimiter.
    An undecodable stretch does NOT end the scan — it advances to the next `{`
    and keeps going — so a truncated or interleaved fragment ahead of the real
    payload (the msgvault and wacli Go binaries both interleave log/binary
    output with their `--json` result) still yields the final object.
    """
    text = (stdout or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    idx = 0
    last: dict[str, Any] = {}
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            value, idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            nxt = text.find("{", idx + 1)  # always > idx, so the scan terminates
            if nxt == -1:
                break
            idx = nxt
            continue
        if isinstance(value, dict):
            last = value
    return last


def unique_strings(values: Any) -> list[str]:
    """Order-preserving de-dup of a scalar or list into stripped, non-empty strings."""
    if values is None:
        return []
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, list):
        raw = values
    else:
        raw = [values]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 hex digest of a file's bytes (artifact fingerprints)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short_hash(value: str, n: int = 12) -> str:
    """First `n` hex chars of `value`'s SHA-256 — a stable, collision-cheap id.

    Callers pass their historical length (12 for twitter/enrich/merge ids, 10
    or 16 for gmail ids) so previously-written digests stay identical.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]
