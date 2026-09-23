"""The history-depth stage's durable artifacts: results.csv, the summary, manifest.

The stage (`depth.py`) decides what to attempt and what came back; this module
owns what that state LOOKS like on disk and how it is read back:

- `results.csv` — one row per chat keyed by the hashed `chat_ref`
  (`HISTORY_DEPTH_HEADERS`), written through a temp file + `chmod 600` +
  atomic replace, rows sorted so a rerun that changes nothing rewrites the same
  bytes;
- `HISTORY_DEPTH_TERMINAL_OUTCOMES` — the outcomes that mean "do not attempt
  this chat again", the vocabulary both the writer and the resume logic share;
- `manifest.json` — `history_depth_summary` builds it (policy knobs actually
  used, counts, the source watermark the next run compares against, the privacy
  block) and `write_history_depth_manifest` writes it through the shared stage
  manifest writer;
- `read_history_depth_manifest` — the previous manifest, returned as the typed
  `payloads.PriorDepthManifest`.

Changelog:
  2026-09-23 (typed rows): `results.csv` rows are the mutable `HistoryDepthRow`,
    parsed by `from_record`/`as_record` at the file, so `depth.py` and
    `history_depth_summary` read and set fields instead of re-parsing cells with
    `result_int(row, ...)` / `.get(...)`. Header, column order, and cell strings
    unchanged.
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`
    alongside `depth.py`, which kept the run loop. Artifact bytes unchanged.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.payloads import (  # noqa: E402
    HistoryDepthTarget,
    PriorDepthManifest,
)
from packs.ingestion.primitives.discover.messages.wacli.util import (  # noqa: E402
    DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS,
    result_int,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_HISTORY_DEPTH_NO_GROWTH_LIMIT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_NO_GROWTH_LIMIT", "1"))
HISTORY_DEPTH_POLICY_VERSION = 4
HISTORY_DEPTH_HEADERS = [
    "chat_ref",
    "kind",
    "initial_count",
    "current_count",
    "current_latest_ts",
    "target_rows_added",
    "unrelated_rows_added",
    "attempts",
    "requests_sent",
    "responses_seen",
    "transient_failures",
    "no_growth_attempts",
    "outcome",
    "error_category",
    "updated_at",
]
HISTORY_DEPTH_TERMINAL_OUTCOMES = {
    "completed_threshold",
    "recovered",
    "server_zero",
    "gone",
    "out_of_scope",
}


@dataclass
class HistoryDepthRow:
    """One `results.csv` row, parsed once at the file.

    Mutable on purpose: `depth.py` mutates its rows in place as attempts come back
    (counters, outcome, watermark). `from_record` is the ONLY tolerant reader of a
    row — it owns the fact that a cell is a `str` from CSV — so nothing downstream
    re-derives `"0"` out of a missing cell. `as_record` is the dict form the CSV
    writer takes."""

    chat_ref: str
    kind: str = ""
    initial_count: int = 0
    current_count: int = 0
    current_latest_ts: int = 0
    target_rows_added: int = 0
    unrelated_rows_added: int = 0
    attempts: int = 0
    requests_sent: int = 0
    responses_seen: int = 0
    transient_failures: int = 0
    no_growth_attempts: int = 0
    outcome: str = "pending"
    error_category: str = "none"
    updated_at: str = ""

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> HistoryDepthRow:
        return cls(
            chat_ref=str(record.get("chat_ref") or ""),
            kind=str(record.get("kind") or ""),
            initial_count=result_int(record, "initial_count"),
            current_count=result_int(record, "current_count"),
            current_latest_ts=result_int(record, "current_latest_ts"),
            target_rows_added=result_int(record, "target_rows_added"),
            unrelated_rows_added=result_int(record, "unrelated_rows_added"),
            attempts=result_int(record, "attempts"),
            requests_sent=result_int(record, "requests_sent"),
            responses_seen=result_int(record, "responses_seen"),
            transient_failures=result_int(record, "transient_failures"),
            no_growth_attempts=result_int(record, "no_growth_attempts"),
            outcome=str(record.get("outcome") or ""),
            error_category=str(record.get("error_category") or ""),
            updated_at=str(record.get("updated_at") or ""),
        )

    def as_record(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


def read_history_depth_results(path: Path) -> dict[str, HistoryDepthRow]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            row.chat_ref: row
            for row in (HistoryDepthRow.from_record(record) for record in CsvIO.dict_reader(handle))
            if row.chat_ref
        }


def read_history_depth_manifest(path: Path) -> PriorDepthManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return PriorDepthManifest.from_payload(payload)


def write_history_depth_results(path: Path, rows: dict[str, HistoryDepthRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_DEPTH_HEADERS)
        writer.writeheader()
        for chat_ref in sorted(rows):
            writer.writerow(rows[chat_ref].as_record())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def history_depth_summary(
    *,
    targets: list[HistoryDepthTarget],
    rows: dict[str, HistoryDepthRow],
    results_path: Path,
    progress_path: Path,
    active_since_ts: int,
    max_count: int,
    count: int,
    requests: int,
    request_delay: str,
    no_growth_limit: int,
    batch_size: int,
    max_in_flight: int,
    response_wait: str,
    batch_delay: str,
    timeout_backoff: str,
    time_budget_seconds: int,
    bootstrap: bool,
    source_total_messages: int,
    source_dm_state_sha256: str,
    recovered_pre_sync_changes: bool,
) -> dict[str, Any]:
    target_rows = [rows[target.chat_ref] for target in targets if target.chat_ref in rows]
    completed = sum(
        1 for row in target_rows if row.outcome in HISTORY_DEPTH_TERMINAL_OUTCOMES
    )
    pending = len(targets) - completed
    return {
        "status": "completed" if pending == 0 else "partial",
        "policy": {
            "version": HISTORY_DEPTH_POLICY_VERSION,
            "active_since": datetime.fromtimestamp(active_since_ts, timezone.utc).isoformat(),
            "lookback_years": DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS,
            "selection": "bootstrap_recent_shallow" if bootstrap else "changed_recent_shallow",
            "recovered_pre_sync_changes": recovered_pre_sync_changes,
            "max_message_count": max_count,
            "count_per_request": count,
            "requests_per_attempt": requests,
            "request_delay": request_delay,
            "batch_size": batch_size,
            "max_in_flight": max_in_flight,
            "response_wait": response_wait,
            "batch_delay": batch_delay,
            "timeout_backoff": timeout_backoff,
            "time_budget_seconds": time_budget_seconds,
            "no_growth_limit": no_growth_limit,
            "native_batch_command": True,
            "one_connection_per_run": True,
            "one_command_per_run": True,
            "identity_strategy": "saved_preference_then_opposite_fallback",
            "identity_preference_store": "private_wacli_db",
            "retry_scope": "next_import",
        },
        "counts": {
            "eligible": len(targets),
            "completed": completed,
            "pending": pending,
            "with_real_request": sum(1 for row in target_rows if row.requests_sent > 0),
            "recovered_chats": sum(
                1
                for row in target_rows
                if row.outcome in {"completed_threshold", "recovered"}
            ),
            "target_rows_added": sum(row.target_rows_added for row in target_rows),
            "unrelated_rows_added": sum(row.unrelated_rows_added for row in target_rows),
            "server_zero": sum(1 for row in target_rows if row.outcome == "server_zero"),
            "transient_failures": sum(row.transient_failures for row in target_rows),
            "terminal_errors": sum(1 for row in target_rows if row.outcome == "terminal_error"),
            "source_total_messages": source_total_messages,
        },
        "source": {
            "dm_state_sha256": source_dm_state_sha256,
        },
        "outputs": {
            "results_csv": str(results_path),
            "progress_jsonl": str(progress_path),
        },
        "privacy": {
            "powerpacks_queries_read_message_bodies": False,
            "raw_identifiers_persisted": False,
            "returned_history_persisted_locally_by_wacli": True,
            "llm_called": False,
            "network": "whatsapp_only",
        },
    }


def write_history_depth_manifest(out_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return write_stage_manifest(out_dir / "manifest.json", payload)
