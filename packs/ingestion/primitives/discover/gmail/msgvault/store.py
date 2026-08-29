"""Read-only connection and stable public API for the local msgvault archive.

`MsgvaultStore` owns connection lifetime and schema validation. Discovery
aggregation, Deep Context selection, and logbook SQL live in sibling modules;
the methods here preserve the one store API used by import-gmail and consumers.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.gmail.msgvault import (  # noqa: E402
    aggregation,
    context_db,
    logbook_db,
)
from packs.ingestion.primitives.discover.gmail.msgvault.util import (  # noqa: E402
    DEFAULT_MSGVAULT_DB,
    msgvault_db_uri,
)

DatabaseError = sqlite3.Error


class MsgvaultStore:
    """Read-only access to one msgvault SQLite archive."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_MSGVAULT_DB,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._con = connection
        self._owns = connection is None
        if connection is not None:
            connection.row_factory = sqlite3.Row

    def __enter__(self) -> "MsgvaultStore":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def connect(self) -> sqlite3.Connection:
        """Open the database read-only, or return the injected connection."""
        if self._con is None:
            db_path = self.db_path.expanduser()
            if not db_path.exists():
                raise SystemExit(
                    f"msgvault database not found: {db_path}. "
                    "Run msgvault sync-full first or pass --db."
                )
            try:
                self._con = sqlite3.connect(msgvault_db_uri(db_path), uri=True)
            except sqlite3.Error as exc:
                raise SystemExit(
                    f"failed to open msgvault database read-only: {exc}"
                ) from exc
            self._owns = True
        self._con.row_factory = sqlite3.Row
        return self._con

    def close(self) -> None:
        """Close only a connection opened by this store."""
        if self._owns and self._con is not None:
            self._con.close()
            self._con = None

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            raise RuntimeError(
                "MsgvaultStore is not connected; use it as a context manager "
                "or pass connection="
            )
        return self._con

    def _table_columns(self, table: str) -> set[str]:
        """Return a table's columns for schema-variant probes."""
        return aggregation._table_columns(self.con, table)

    def require_schema(self) -> None:
        """Exit unless the required msgvault metadata tables exist."""
        required = {"sources", "participants", "messages", "message_recipients"}
        rows = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        present = {str(row[0]) for row in rows}
        missing = sorted(required - present)
        if missing:
            raise SystemExit(
                f"msgvault schema missing required tables: {', '.join(missing)}"
            )

    def has_label_tables(self) -> bool:
        return aggregation.has_label_tables(self.con)

    def iter_metadata(
        self,
        account_email: str = "",
        exclude_labels: Iterable[str] | None = None,
        *,
        stream_order: bool = False,
    ) -> Iterator[sqlite3.Row]:
        yield from aggregation.iter_metadata(
            self.con,
            account_email,
            exclude_labels,
            stream_order=stream_order,
        )

    def aggregate_contacts(
        self,
        account_email: str = "",
        exclude_labels: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        return aggregation.aggregate_contacts(self.con, account_email, exclude_labels)

    def list_accounts(self) -> list[dict[str, Any]]:
        return aggregation.list_accounts(self.con)

    def thread_participant_rosters(
        self,
        emails: Iterable[str],
        max_threads: int,
    ) -> list[dict[str, Any]]:
        return context_db.thread_participant_rosters(self.con, emails, max_threads)

    def fetch_recent_rows(self, email: str, fetch_limit: int) -> list[sqlite3.Row]:
        return context_db.fetch_recent_rows(self.con, email, fetch_limit)

    def create_candidate_pid_table(self, emails: Iterable[str]) -> int:
        return context_db.create_candidate_pid_table(self.con, emails)

    def stream_contact_groups(
        self,
        fetch_limit: int,
    ) -> Iterator[tuple[str, list[sqlite3.Row]]]:
        yield from context_db.stream_contact_groups(self.con, fetch_limit)

    def account_emails(self) -> set[str]:
        return context_db.account_emails(self.con)

    def owner_identity(self) -> dict[str, Any]:
        return context_db.owner_identity(self.con)

    def count_messages_for(self, email: str, accounts: set[str]) -> int:
        return context_db.count_messages_for(self.con, email, accounts)

    def prepare_logbook_conversations(self, emails: Iterable[str]) -> int:
        return logbook_db.prepare_conversations(self.con, emails)

    def stream_logbook_thread_rows(self, since_id: int = 0) -> sqlite3.Cursor:
        return logbook_db.stream_thread_rows(self.con, since_id)

    def logbook_body_parts(self, message_id: int, raw_head_cap: int) -> dict[str, Any]:
        return logbook_db.body_parts(self.con, message_id, raw_head_cap)

    def count_logbook_messages(self) -> int:
        return logbook_db.count_messages(self.con)

    def participant_phone_names(self) -> list[dict[str, str]]:
        return logbook_db.participant_phone_names(self.con)
