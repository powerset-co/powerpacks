"""msgvault sync for Gmail discovery: last-sync inference and account sync.

Changelog:
  2026-07-23 (audit): moved from `gmail/sync.py` into the `gmail/msgvault/`
    package (it is msgvault lifecycle — sync-full + resume inference — so it
    belongs beside store/util). Bootstrap depth +1; imports unchanged.
  2026-07-23 (audit):
    - Split out of the former single-file gmail.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
import json
import shlex
import shutil
import sqlite3
import subprocess
import urllib.parse
import sys


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB  # noqa: E402
from packs.ingestion.primitives.common.proc import emit_progress, run_cmd  # noqa: E402
from packs.ingestion.primitives.discover.common import ordered_unique  # noqa: E402
from packs.ingestion.primitives.discover.discovery_config import (  # noqa: E402
    source_config,
)
MSGVAULT_REAUTH_ERROR_MARKERS = (
    "expired or revoked",
    "cannot re-authorize",
    "invalid_grant",
    "missing token",
    "token is missing",
)


def parse_msgvault_sync_date(value: Any) -> str:
    """Normalize a msgvault date to YYYY-MM-DD ("" when unparseable).

    The value is NOT strictly typed at this boundary: msgvault stores/emits
    dates as ISO strings, epoch seconds, or epoch milliseconds depending on
    build and table (sources.last_sync_at vs messages.internal_date), so this
    accepts all three and never raises."""
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) >= 10 and text[:10].count("-") == 2:
        return text[:10]
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def sqlite_table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def infer_msgvault_sync_after(db: str, email: str) -> dict[str, str]:
    """Best local marker for an incremental sync: {sync_after, source} or {}.

    PER ACCOUNT, in preference order: (1) the msgvault sources.last_sync_at
    column when the build records it; (2) otherwise the max stored message
    date for that account across internal_date/sent_at/received_at. Empty
    dict means no usable marker — the caller runs a FULL sync (correct on a
    first run or an unreadable store). Read-only, 1s timeout, never raises."""
    path = Path(db or DEFAULT_MSGVAULT_DB).expanduser()
    if not email or not path.exists():
        return {}
    uri = f"file:{urllib.parse.quote(str(path), safe='/')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1)
    except sqlite3.Error:
        return {}
    try:
        source_cols = sqlite_table_columns(con, "sources")
        if not {"id", "source_type", "identifier"}.issubset(source_cols):
            return {}
        select_cols = ["id"]
        if "last_sync_at" in source_cols:
            select_cols.append("last_sync_at")
        source = con.execute(
            f"SELECT {', '.join(select_cols)} FROM sources WHERE lower(source_type) = 'gmail' AND lower(identifier) = lower(?) ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if not source:
            return {}
        source_id = source[0]
        if "last_sync_at" in source_cols:
            source_date = parse_msgvault_sync_date(source[1])
            if source_date:
                return {"sync_after": source_date, "source": "msgvault.sources.last_sync_at"}

        message_cols = sqlite_table_columns(con, "messages")
        if "source_id" not in message_cols:
            return {}
        candidates: list[tuple[str, str]] = []
        for column in ("internal_date", "sent_at", "received_at"):
            if column not in message_cols:
                continue
            row = con.execute(f"SELECT max({column}) FROM messages WHERE source_id = ?", (source_id,)).fetchone()
            date = parse_msgvault_sync_date(row[0] if row else "")
            if date:
                candidates.append((date, f"msgvault.messages.{column}"))
        if not candidates:
            return {}
        date, source_name = max(candidates, key=lambda item: item[0])
        return {"sync_after": date, "source": source_name}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


@lru_cache(maxsize=1)
def msgvault_sync_supports_no_attachments() -> bool:
    """True only if this msgvault build exposes --no-attachments on sync-full.
    Older builds (e.g. v0.14.1) only expose it on the import-* commands, so we
    must not pass it to sync-full or the command errors out."""
    if not shutil.which("msgvault"):
        return False
    try:
        help_text = subprocess.run(
            ["msgvault", "sync-full", "--help"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "--no-attachments" in help_text


def msgvault_reauthorization_required(payload: dict[str, Any], stderr: str) -> bool:
    error_text = "\n".join((stderr, json.dumps(payload, default=str))).lower()
    return any(marker in error_text for marker in MSGVAULT_REAUTH_ERROR_MARKERS)


def msgvault_reauthorize_command(email: str) -> str:
    return (
        "uv run --project . python "
        "packs/ingestion/primitives/setup/msgvault_setup.py "
        f"add-account --email {shlex.quote(email)} --force-auth"
    )


def sync_msgvault_account(
    email: str,
    db: str,
    query: str,
    *,
    sync_after_override: str = "",
    sync_before: str = "",
    fresh: bool = False,
    limit: int = 0,
    no_attachments: bool = False,
) -> dict[str, Any]:
    # An explicit window (from the onboarding date picker) overrides the
    # resume-inferred --after. With a window we also pass --noresume so the
    # full range is rescanned deterministically; already-stored messages are
    # skipped, so this only downloads what is genuinely new.
    if sync_after_override:
        sync_after = sync_after_override
        sync_after_source = "explicit_window"
    else:
        inferred = infer_msgvault_sync_after(db, email)
        sync_after = inferred.get("sync_after", "")
        sync_after_source = inferred.get("source", "")
    no_attachments_applied = bool(no_attachments) and msgvault_sync_supports_no_attachments()
    if not shutil.which("msgvault"):
        return {
            "status": "skipped",
            "reason": "msgvault_command_not_found",
            "account_email": email,
            "sync_after": sync_after,
            "sync_after_source": sync_after_source,
            "query": query,
        }
    cmd = ["msgvault"]
    db_home = Path(db).expanduser().parent
    default_home = Path(DEFAULT_MSGVAULT_DB).expanduser().parent
    if db_home != default_home:
        cmd.extend(["--home", str(db_home)])
    cmd.extend(["sync-full", email])
    if sync_after:
        cmd.extend(["--after", sync_after])
    if sync_before:
        cmd.extend(["--before", sync_before])
    if query:
        cmd.extend(["--query", query])
    if fresh or sync_after_override:
        cmd.append("--noresume")
    if limit and int(limit) > 0:
        cmd.extend(["--limit", str(int(limit))])
    if no_attachments_applied:
        cmd.append("--no-attachments")
    window_label = f" after {sync_after}" if sync_after else ""
    emit_progress(f"Starting Gmail sync for {email}{window_label}.")
    code, payload, stderr = run_cmd(cmd)
    error: Any = (stderr or payload) if code != 0 else ""
    result = {
        "status": "completed" if code == 0 else "failed",
        "account_email": email,
        "code": code,
        "messages_added": payload.get("messages_added") if isinstance(payload, dict) else "",
        "error": error,
        "sync_after": sync_after,
        "sync_after_source": sync_after_source,
        "sync_before": sync_before,
        "query": query,
        "fresh": bool(fresh or sync_after_override),
        "limit": int(limit) if limit else 0,
        "no_attachments_requested": bool(no_attachments),
        "no_attachments_applied": no_attachments_applied,
    }
    if code != 0 and msgvault_reauthorization_required(payload, stderr):
        reauthorize_command = msgvault_reauthorize_command(email)
        result.update({
            "error_code": "gmail_reauthorization_required",
            "error": (
                f"Gmail authorization expired or was revoked for {email}. "
                f"Re-authorize it explicitly before retrying: {reauthorize_command}"
            ),
            "error_detail": error,
            "reauthorize_command": reauthorize_command,
        })
    if code == 0:
        emit_progress(f"Gmail sync completed for {email}.")
    elif result.get("error_code") == "gmail_reauthorization_required":
        emit_progress(str(result["error"]))
    else:
        emit_progress(f"Gmail sync failed for {email} (exit {code}).")
    return result


def normalize_label_names(labels: Any) -> list[str]:
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, list):
        return []
    return ordered_unique([str(label).strip() for label in labels if str(label or "").strip()])


def gmail_sync_query(input_cfg: dict[str, Any]) -> str:
    explicit = str(input_cfg.get("gmail_sync_query") or "").strip()
    if explicit:
        return explicit
    return str(source_config("gmail")["inputs"].get("sync_query") or "").strip()


def gmail_sync_after(input_cfg: dict[str, Any]) -> str:
    return parse_msgvault_sync_date(input_cfg.get("gmail_sync_after"))


def gmail_excluded_labels(input_cfg: dict[str, Any]) -> list[str]:
    if input_cfg.get("include_category_mail"):
        return []
    labels = input_cfg.get("gmail_exclude_labels")
    if labels:
        return normalize_label_names(labels)
    return ["CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES"]




