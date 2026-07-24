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
- `parse_last_json` — return the LAST top-level JSON object emitted on a child's
  stdout (`{}` when none); the tolerant progress-then-result contract.
- `unique_strings` — order-preserving de-dup of a scalar/list into stripped
  non-empty strings.
- `sha256_file` — streaming SHA-256 of a file (fingerprint helper).
- `short_hash` — first `n` hex chars of a value's SHA-256 (stable ids). Callers
  pass their historical length so existing digests are unchanged.

Changelog:
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


def parse_last_json(stdout: str) -> dict[str, Any]:
    """Return the last top-level JSON object on a child's stdout, `{}` if none.

    Children print human progress lines and then a final JSON result; scanning
    for the last decodable dict tolerates that interleaving without a delimiter.
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
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            last = value
        idx = end
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
