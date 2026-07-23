"""The one canonical access layer over the local msgvault SQLite archive.

`MsgvaultStore` is the single object every consumer uses to talk to the msgvault
DB: it owns the read-only connection (open/close, context-manager support),
schema/label-table probing, the discovery metadata aggregation, and the
deep-context/logbook body reads. Everything is local, so there is no
metadata-vs-bodies split in the access layer — the discovery aggregation
(`iter_metadata`/`aggregate_contacts`) reads only the metadata tables
(`sources`, `participants`, `messages`, `message_recipients`, plus the optional
`labels`/`message_labels` pair), while the deep-context/logbook helpers
(`fetch_recent_rows`, `create_candidate_pid_table`/`stream_contact_groups`,
`count_messages_for`) additionally read subjects, snippets, and message bodies
(`messages.subject`/`messages.snippet`, `message_bodies.body_text`). Both go
through this one store.

Pure email-identity/text helpers (`normalize_email`, `classify_email`,
`parse_email_header`, `is_automated_email`, `best_display_name`, `split_name`,
…) and the pure label/row helpers (`normalize_label_names`,
`default_excluded_labels`, `has_round_trip_interaction`, `canonical_message_id`)
stay as module-level functions — they take no connection.

Consumers: `gmail/discover_engine.py` (the discovery CLI child),
`deep_context/build_email_context.py` / `deep_context/sources.py` and
`deep_context/collect_person_context.py` (per-person context + candidate
re-derivation), and `logbook/logbook_sources.py` (candidate-pid temp table).

Changelog:
  2026-07-23 (audit batch 22): absorbed is_likely_person_name /
    is_generic_or_non_person (+ their keyword sets) from the retired legacy
    resolver gmail/resolve_queue.py; deep-context owns LinkedIn resolution now.
  2026-07-23 (audit batch 19): folded the second msgvault SQLite layer
    (`deep_context/build_email_context.py`) into this module. Introduced
    `MsgvaultStore` as the single access point; the former connection-bound
    module functions (`connect_msgvault`, `require_msgvault_schema`,
    `msgvault_has_label_tables`, `iter_msgvault_metadata`,
    `aggregate_msgvault_contacts`, `list_msgvault_accounts`) became methods
    (`connect`/`close`, `require_schema`, `has_label_tables`, `iter_metadata`,
    `aggregate_contacts`, `list_accounts`), and the recent-emails-with-bodies
    SQL + `fetch_recent_rows`, `create_candidate_pid_table`,
    `stream_contact_groups`, `account_emails`, `owner_identity`,
    `count_messages_for` moved here from build_email_context. The thin
    `recent_emails_for` selection wrapper stays in build_email_context (it is
    pure over `fetch_recent_rows` output + `select_emails_from_rows`, and
    keeping it there avoids a discover→deep_context import cycle).
  2026-07-23 (audit batch 17): split out of the retired
    `gmail/network_import.py` monolith. DEFAULT_MSGVAULT_DB stays here (NOT
    deduped into discover common.py: this one honors $MSGVAULT_HOME; common's
    does not).
"""

from __future__ import annotations

import itertools
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_MSGVAULT_DB = Path(os.environ.get("MSGVAULT_HOME", str(Path.home() / ".msgvault"))) / "msgvault.db"
DEFAULT_EXCLUDED_MSGVAULT_LABELS = ("CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES")

PERSONAL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
    "msn.com",
    "live.com",
    "me.com",
    "mac.com",
    "protonmail.com",
    "mail.com",
    "ymail.com",
    "googlemail.com",
    "comcast.net",
    "att.net",
    "verizon.net",
    "sbcglobal.net",
    "cox.net",
    "earthlink.net",
    "126.com",
    "163.com",
    "qq.com",
}
NON_WORK_DOMAINS = PERSONAL_DOMAINS | {"noreply.github.com", "users.noreply.github.com"}
AUTOMATED_EMAIL_KEYWORDS = {
    "unsub",
    "unsubscribe",
    "bounce",
    "spam",
    "spamproc",
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "leave-",
    "void-",
    "reply.",
    "notification",
    "notifications",
    "alert",
    "alerts",
    "reservation",
    "reservations",
    "booking",
    "bookings",
}
SUPPORT_TICKET_DOMAINS = {"zendesk", "freshdesk", "intercom", "helpscout", "helpdesk"}
TRAVEL_SERVICE_DOMAINS = {
    "airbnb",
    "vrbo",
    "booking.com",
    "hotels.com",
    "expedia",
    "uber.com",
    "lyft.com",
    "united",
    "delta",
    "aa.com",
    "americanairlines",
    "southwest",
    "jetblue",
    "alaska",
    "marriott",
    "hilton",
    "hyatt",
    "hertz",
    "avis",
    "enterprise",
}

# Resolve a contact's participant id(s) up front so the message lookups can use
# the sender_id / message_recipients.participant_id indexes directly. The old
# `sender = ? OR EXISTS(recipients…)` shape made SQLite scan every email per
# contact (idx_messages_type) and run a correlated subquery per row — minutes
# per all-contact run on a large archive.
PARTICIPANT_IDS_SQL = "SELECT id FROM participants WHERE LOWER(email_address) = ?"

_RECENT_EMAILS_SELECT = """
SELECT
    COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
    m.conversation_id,
    LOWER(sp.email_address) AS sender_email,
    m.subject,
    m.snippet,
    mb.body_text
"""
# Emails the contact SENT — index-direct on messages.sender_id.
RECENT_EMAILS_FROM_SENDER_SQL = _RECENT_EMAILS_SELECT + """
FROM messages m
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  AND m.sender_id = ?1
ORDER BY at DESC
LIMIT ?2
"""
# Emails the contact RECEIVED — index-direct on message_recipients.participant_id.
RECENT_EMAILS_TO_RECIPIENT_SQL = _RECENT_EMAILS_SELECT + """
FROM message_recipients mr
JOIN messages m ON m.id = mr.message_id
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  AND mr.participant_id = ?1
ORDER BY at DESC
LIMIT ?2
"""

# All-contacts fast path: one windowed query, streamed contact-by-contact. Window
# on lightweight columns only (subject/snippet); join body_text AFTER the rn<=K
# filter so the big blobs are read for kept rows only. UNION (not UNION ALL)
# dedupes a message that matches a contact on both the sender and recipient side.
# The result is ORDER BY contact so `stream_contact_groups` can groupby the cursor
# and hold just one contact's rows at a time.
WINDOWED_CONTEXT_SQL = """
WITH assoc AS (
    SELECT cp.cemail AS cemail, m.id AS mid,
           COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
           m.conversation_id AS conversation_id, m.sender_id AS sender_id,
           m.subject AS subject, m.snippet AS snippet
    FROM cand_pid cp
    JOIN messages m ON m.sender_id = cp.pid
    WHERE m.message_type = 'email'
      AND (m.deleted_at IS NULL OR m.deleted_at = '')
      AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
    UNION
    SELECT cp.cemail, m.id,
           COALESCE(m.sent_at, m.received_at, m.internal_date),
           m.conversation_id, m.sender_id, m.subject, m.snippet
    FROM cand_pid cp
    JOIN message_recipients mr ON mr.participant_id = cp.pid
    JOIN messages m ON m.id = mr.message_id
    WHERE m.message_type = 'email'
      AND (m.deleted_at IS NULL OR m.deleted_at = '')
      AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
),
ranked AS (
    SELECT assoc.*,
           ROW_NUMBER() OVER (PARTITION BY cemail ORDER BY at DESC, mid DESC) AS rn
    FROM assoc
)
SELECT r.cemail AS cemail,
       r.at AS at,
       r.conversation_id AS conversation_id,
       LOWER(sp.email_address) AS sender_email,
       r.subject AS subject,
       r.snippet AS snippet,
       mb.body_text AS body_text
FROM ranked r
LEFT JOIN participants sp ON sp.id = r.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = r.mid
WHERE r.rn <= ?
ORDER BY r.cemail, r.at DESC, r.mid DESC
"""


def normalize_email(email: str) -> str:
    """Lowercase and validate an email address; raise ValueError when invalid."""
    value = (email or "").strip().lower()
    if not value or "@" not in value:
        raise ValueError("--email must contain a valid email address")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("--email must contain a valid email address")
    return value


def normalize_name(name: str, email: str = "") -> str:
    """Collapse whitespace in a display name, falling back to a name derived
    from the email local part when the name is blank."""
    value = " ".join((name or "").strip().split())
    if value:
        return value
    local = email.split("@", 1)[0] if "@" in email else ""
    local = re.sub(r"[._+-]+", " ", local)
    return " ".join(part.capitalize() for part in local.split() if part)


def classify_email(email: str) -> str:
    """Classify an address as personal, work, other, or unknown by its domain."""
    if not email or "@" not in email:
        return "unknown"
    domain = email.rsplit("@", 1)[1].lower()
    if domain in PERSONAL_DOMAINS:
        return "personal"
    if domain in NON_WORK_DOMAINS:
        return "other"
    return "work"


def domain_guess(email: str) -> dict[str, str]:
    """Guess a company name from the email domain (local heuristic only)."""
    domain = email.rsplit("@", 1)[1].lower() if "@" in email else ""
    root = domain.split(".")[0] if domain else ""
    company_guess = " ".join(part.capitalize() for part in re.split(r"[-_]", root) if part)
    return {"domain": domain, "company_guess": company_guess, "method": "local_domain_heuristic"}


def parse_email_header(header_value: str) -> list[tuple[str, str]]:
    """Parse a Gmail From/To/Cc header into (display_name, email) pairs.

    Ported from the legacy Gmail ingestion path, but kept stdlib-only and local.
    """
    if not header_value:
        return []

    parts: list[str] = []
    current = ""
    in_quotes = False
    in_angle = False
    for char in header_value:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "<":
            in_angle = True
        elif char == ">":
            in_angle = False
        elif char == "," and not in_quotes and not in_angle:
            if current.strip():
                parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())

    results: list[tuple[str, str]] = []
    angle_pattern = r"^(.*?)\s*<([^>]+)>$"
    email_pattern = r"^[a-zA-Z0-9._%+-]{2,}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    for part in parts:
        value = part.strip()
        if not value:
            continue
        angle_match = re.match(angle_pattern, value)
        if angle_match:
            name = normalize_name(angle_match.group(1).strip().strip('"'))
            email = angle_match.group(2).strip().lower()
            if re.match(email_pattern, email):
                results.append((name, email))
        else:
            email = value.lower()
            if re.match(email_pattern, email):
                results.append(("", email))
    return results


def is_automated_email(email: str) -> tuple[bool, str]:
    """Detect automated/service addresses; return (is_automated, reason)."""
    if not email or "@" not in email:
        return True, "invalid email"
    local_part, domain = email.lower().rsplit("@", 1)
    for keyword in AUTOMATED_EMAIL_KEYWORDS:
        if keyword in local_part or keyword in domain:
            return True, f"contains '{keyword}'"
    for system in SUPPORT_TICKET_DOMAINS:
        if system in domain:
            return True, f"support ticket system ({system})"
    for service in TRAVEL_SERVICE_DOMAINS:
        if service in domain:
            return True, f"travel/hospitality service ({service})"
    if re.search(r"[a-f0-9]{16,}", local_part):
        return True, "hash-like pattern in email"
    if len(local_part) >= 20:
        vowel_count = sum(1 for c in local_part if c in "aeiou")
        vowel_ratio = vowel_count / len(local_part) if local_part else 0
        if vowel_ratio < 0.15 and re.match(r"^[a-z0-9_-]+$", local_part):
            return True, "random alphanumeric pattern"
    if len(local_part) > 40 and re.match(r"^[a-z0-9_-]+$", local_part):
        return True, "very long alphanumeric local part"
    return False, ""


def split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (first, last), using the outermost tokens."""
    parts = [part for part in re.split(r"\s+", (full_name or "").strip()) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def default_name_for_email(email: str) -> str:
    """Derive a capitalized display name from an email's local part."""
    local = email.split("@", 1)[0] if "@" in email else email
    local = re.sub(r"[._+-]+", " ", local)
    return " ".join(part.capitalize() for part in local.split() if part)


def best_display_name(email: str, names: dict[str, int]) -> str:
    """Pick the most frequent non-empty display name observed for an email,
    falling back to a name derived from the address."""
    cleaned: dict[str, int] = {}
    email_l = email.lower()
    for name, count in names.items():
        value = normalize_name(name, email)
        if not value or value.lower() == email_l:
            continue
        cleaned[value] = cleaned.get(value, 0) + count
    if cleaned:
        return sorted(cleaned.items(), key=lambda item: (-item[1], item[0].casefold()))[0][0]
    return default_name_for_email(email)


def normalize_label_names(values: Iterable[str] | None) -> list[str]:
    """Uppercase, trim, and dedupe label names, preserving first-seen order."""
    out: list[str] = []
    for value in values or []:
        label = str(value or "").strip()
        if label and label.upper() not in out:
            out.append(label.upper())
    return out


def default_excluded_labels(include_category_mail: bool, extra_labels: Iterable[str] | None = None) -> list[str]:
    """Return the label-exclusion list: Gmail category labels (unless included)
    plus any extra labels, normalized."""
    labels: list[str] = []
    if not include_category_mail:
        labels.extend(DEFAULT_EXCLUDED_MSGVAULT_LABELS)
    labels.extend(extra_labels or [])
    return normalize_label_names(labels)


def msgvault_db_uri(path: Path) -> str:
    """Return the read-only SQLite URI for a msgvault database path."""
    return f"file:{path.expanduser().resolve()}?mode=ro"


def canonical_message_id(row: Any) -> str:
    """Stable identity for one real email message.

    msgvault can store the same RFC822 message under multiple conversation_ids
    (and across accounts), each with its own msgvault row id. Counting by row
    id double-counts those copies. Prefer rfc822_message_id, then
    source_message_id, then fall back to the msgvault row id.
    """
    for key in ("rfc822_message_id", "source_message_id"):
        try:
            value = str(row[key] or "").strip()
        except (KeyError, IndexError):
            value = ""
        if value:
            return f"{key}:{value}"
    return f"row:{row['message_id']}"


def _fold_msgvault_message(message: dict[str, Any], records: dict[str, dict[str, Any]], account_filter: str) -> None:
    """Fold one canonical message's contributions into per-contact accumulators.

    Mirrors the materialized path's per-message body, but the nine per-message
    count categories are integer counters: each canonical message is folded once
    and counted_for_message dedupes within it, so a counter equals the old set
    length. Thread/account/source sets stay sets (a thread_id recurs across
    messages).
    """
    source_account = str(message.get("source_account") or "").strip().lower()
    if account_filter and source_account != account_filter:
        return
    participants = message.get("participants") or []
    from_emails = {str(p.get("email") or "").strip().lower() for p in participants if p.get("recipient_type") == "from"}
    sender_email = str(message.get("sender_email") or "").strip().lower()
    if sender_email:
        from_emails.add(sender_email)
    labels = set(message.get("label_names") or [])
    has_explicit_from = bool(from_emails)
    has_recipient = any(p.get("recipient_type") in {"to", "cc", "bcc"} for p in participants)
    if message.get("has_label_tables"):
        is_sent = "SENT" in labels
    else:
        is_sent = (bool(source_account) and source_account in from_emails) or (not has_explicit_from and has_recipient)
    external_emails = {
        str(p.get("email") or "").strip().lower()
        for p in participants
        if p.get("email") and str(p.get("email")).strip().lower() != source_account
    }
    if sender_email and sender_email != source_account:
        external_emails.add(sender_email)
    is_group = len(external_emails) > 1
    message_kind = "group" if is_group else "one_to_one"
    if not any(p.get("recipient_type") == "from" for p in participants) and sender_email:
        participants = list(participants) + [{
            "email": sender_email,
            "recipient_type": "from",
            "recipient_display_name": str(message.get("sender_display_name") or ""),
            "participant_display_name": str(message.get("sender_display_name") or ""),
        }]
    counted_for_message: set[tuple[str, str, str]] = set()
    for participant in participants:
        email = str(participant.get("email") or "").strip().lower()
        if not email or email == source_account or (account_filter and email == account_filter):
            continue
        recipient_type = str(participant.get("recipient_type") or "")
        count_direction = ""
        if is_sent and recipient_type in {"to", "cc", "bcc"}:
            count_direction = "sent"
        elif not is_sent and recipient_type == "from":
            count_direction = "received"
        if not count_direction:
            continue
        msg_id = str(message["message_id"])
        dedupe_key = (msg_id, email, count_direction)
        if dedupe_key in counted_for_message:
            continue
        counted_for_message.add(dedupe_key)
        record = records.setdefault(email, {
            "email": email,
            "names": {},
            "sent_messages": 0,
            "received_messages": 0,
            "all_messages": 0,
            "one_to_one_messages": 0,
            "one_to_one_sent_messages": 0,
            "one_to_one_received_messages": 0,
            "group_messages": 0,
            "group_sent_messages": 0,
            "group_received_messages": 0,
            "threads": set(),
            "one_to_one_threads": set(),
            "group_threads": set(),
            "accounts": set(),
            "source_ids": set(),
            "first_interaction": "",
            "last_interaction": "",
        })
        for name_key in ("recipient_display_name", "participant_display_name"):
            name = str(participant.get(name_key) or "").strip()
            if name:
                record["names"][name] = int(record["names"].get(name, 0)) + 1
        record["all_messages"] += 1
        if count_direction == "sent":
            record["sent_messages"] += 1
            record[f"{message_kind}_sent_messages"] += 1
        elif count_direction == "received":
            record["received_messages"] += 1
            record[f"{message_kind}_received_messages"] += 1
        record[f"{message_kind}_messages"] += 1
        if message["conversation_id"] is not None:
            thread_id = str(message["conversation_id"])
            record["threads"].add(thread_id)
            record[f"{message_kind}_threads"].add(thread_id)
        if message["source_id"] is not None:
            record["source_ids"].add(str(message["source_id"]))
        if source_account:
            record["accounts"].add(source_account)
        message_at = str(message["message_at"] or "").strip()
        if message_at:
            if not record["first_interaction"] or message_at < record["first_interaction"]:
                record["first_interaction"] = message_at
            if not record["last_interaction"] or message_at > record["last_interaction"]:
                record["last_interaction"] = message_at


def has_round_trip_interaction(row: dict[str, Any]) -> bool:
    """True when a contact has BOTH sent and received messages (round trip)."""
    return int(row.get("total_sent") or 0) > 0 and int(row.get("total_received") or 0) > 0


class MsgvaultStore:
    """The single access point for the local msgvault SQLite archive.

    Open it either against a database path (``MsgvaultStore(db_path)`` — opens
    the file read-only on ``connect``/context-enter and closes it on exit) or
    around an already-open connection (``MsgvaultStore(connection=con)`` — used
    by callers/tests that own the connection; the store then never closes it).
    All methods operate on ``self.con``. Metadata reads and body reads both go
    through this one object; the archive is local so no split is preserved.
    """

    def __init__(self, db_path: Path | str = DEFAULT_MSGVAULT_DB, *, connection: sqlite3.Connection | None = None) -> None:
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
        """Open the database read-only (SystemExit when missing/unreadable) and
        return the connection. A no-op when already connected or connection-injected."""
        if self._con is None:
            db_path = self.db_path.expanduser()
            if not db_path.exists():
                raise SystemExit(f"msgvault database not found: {db_path}. Run msgvault sync-full first or pass --db.")
            try:
                self._con = sqlite3.connect(msgvault_db_uri(db_path), uri=True)
            except sqlite3.Error as exc:
                raise SystemExit(f"failed to open msgvault database read-only: {exc}") from exc
            self._owns = True
        self._con.row_factory = sqlite3.Row
        return self._con

    def close(self) -> None:
        """Close the connection only if this store opened it (never an injected one)."""
        if self._owns and self._con is not None:
            self._con.close()
            self._con = None

    @property
    def con(self) -> sqlite3.Connection:
        """The live connection; raises if the store was never connected."""
        if self._con is None:
            raise RuntimeError("MsgvaultStore is not connected; use it as a context manager or pass connection=")
        return self._con

    def _table_columns(self, table: str) -> set[str]:
        """Return the column names of a table, or an empty set on SQLite errors."""
        try:
            rows = self.con.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            return set()
        return {str(row[1]) for row in rows}

    def require_schema(self) -> None:
        """SystemExit unless the required msgvault metadata tables exist."""
        required = {"sources", "participants", "messages", "message_recipients"}
        rows = self.con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
        present = {str(row[0]) for row in rows}
        missing = sorted(required - present)
        if missing:
            raise SystemExit(f"msgvault schema missing required tables: {', '.join(missing)}")

    def has_label_tables(self) -> bool:
        """Return True when both `labels` and `message_labels` tables exist."""
        rows = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name IN ('labels', 'message_labels')"
        ).fetchall()
        return {str(row[0]) for row in rows} == {"labels", "message_labels"}

    def iter_metadata(self, account_email: str = "", exclude_labels: Iterable[str] | None = None, *, stream_order: bool = False) -> Iterator[sqlite3.Row]:
        """Yield one row per (message, recipient) from the metadata tables,
        optionally filtered to one account and excluding labeled messages.

        With stream_order=True, rows of one canonical message are contiguous so the
        streaming aggregation can fold message-by-message."""
        con = self.con
        labels = normalize_label_names(exclude_labels)
        label_filter = ""
        params: list[Any] = [account_email, account_email]
        has_label_tables = self.has_label_tables()
        if labels and has_label_tables:
            placeholders = ",".join("?" for _ in labels)
            label_filter = f"""
              AND NOT EXISTS (
                  SELECT 1
                  FROM message_labels ml
                  JOIN labels l ON l.id = ml.label_id
                  WHERE ml.message_id = m.id
                    AND UPPER(l.name) IN ({placeholders})
              )
            """
            params.extend(labels)
        message_columns = self._table_columns("messages")
        sender_join = ""
        sender_select = "NULL AS sender_email, NULL AS sender_display_name,"
        if "sender_id" in message_columns:
            sender_join = "LEFT JOIN participants sender_p ON sender_p.id = m.sender_id"
            sender_select = "sender_p.email_address AS sender_email, sender_p.display_name AS sender_display_name,"
        rfc822_select = "m.rfc822_message_id AS rfc822_message_id," if "rfc822_message_id" in message_columns else "NULL AS rfc822_message_id,"
        source_msg_select = "m.source_message_id AS source_message_id," if "source_message_id" in message_columns else "NULL AS source_message_id,"
        # Streaming aggregation needs all rows of one canonical message contiguous.
        # Group by the same key canonical_message_id() uses (rfc822 -> source -> row),
        # then keep the within-message order identical to the default sort so the
        # buffered message (header from first row + participant order) matches the
        # materialized path exactly.
        rfc822_col = "m.rfc822_message_id" if "rfc822_message_id" in message_columns else "NULL"
        source_col = "m.source_message_id" if "source_message_id" in message_columns else "NULL"
        if stream_order:
            order_clause = (
                f"COALESCE(NULLIF(TRIM({rfc822_col}), ''), NULLIF(TRIM({source_col}), ''), 'row:' || m.id), "
                "LOWER(p.email_address), m.id"
            )
        else:
            order_clause = "LOWER(p.email_address), m.id"
        label_select = "'' AS label_names"
        has_label_tables_select = "0 AS has_label_tables"
        if has_label_tables:
            has_label_tables_select = "1 AS has_label_tables"
            label_select = """
                COALESCE((
                    SELECT group_concat(UPPER(l2.name), ',')
                    FROM message_labels ml2
                    JOIN labels l2 ON l2.id = ml2.label_id
                    WHERE ml2.message_id = m.id
                ), '') AS label_names
            """
        query = """
            SELECT
                s.id AS source_id,
                s.identifier AS account_email,
                s.display_name AS account_display_name,
                {sender_select}
                {label_select},
                {has_label_tables_select},
                p.email_address AS email,
                p.display_name AS participant_display_name,
                mr.display_name AS recipient_display_name,
                LOWER(mr.recipient_type) AS recipient_type,
                m.id AS message_id,
                {rfc822_select}
                {source_msg_select}
                m.conversation_id AS conversation_id,
                COALESCE(m.sent_at, m.received_at, m.internal_date) AS message_at
            FROM message_recipients mr
            JOIN participants p ON p.id = mr.participant_id
            JOIN messages m ON m.id = mr.message_id
            JOIN sources s ON s.id = m.source_id
            {sender_join}
            WHERE p.email_address IS NOT NULL
              AND TRIM(p.email_address) != ''
              AND (m.message_type IS NULL OR m.message_type = '' OR m.message_type = 'email')
              AND (m.deleted_at IS NULL OR m.deleted_at = '')
              AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
              AND (? = '' OR LOWER(s.identifier) = LOWER(?))
              {label_filter}
            ORDER BY {order_clause}
        """.format(
            sender_select=sender_select,
            label_select=label_select,
            has_label_tables_select=has_label_tables_select,
            rfc822_select=rfc822_select,
            source_msg_select=source_msg_select,
            sender_join=sender_join,
            label_filter=label_filter,
            order_clause=order_clause,
        )
        yield from con.execute(query, params)

    def aggregate_contacts(self, account_email: str = "", exclude_labels: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Aggregate msgvault contact metadata into per-person interaction records.

        Streams rows ordered so every row of one canonical message is contiguous,
        folding one message at a time instead of materializing all messages. Peak
        memory becomes O(unique contacts) + one buffered message instead of
        O(total messages). Output is byte-identical to the materialized path.
        """
        account_filter = account_email.strip().lower()
        records: dict[str, dict[str, Any]] = {}
        current_key: str | None = None
        message: dict[str, Any] | None = None
        for row in self.iter_metadata(account_filter, exclude_labels, stream_order=True):
            msg_id = canonical_message_id(row)
            if msg_id != current_key:
                if message is not None:
                    _fold_msgvault_message(message, records, account_filter)
                current_key = msg_id
                message = {
                    "message_id": msg_id,
                    "conversation_id": row["conversation_id"],
                    "message_at": str(row["message_at"] or "").strip(),
                    "source_id": row["source_id"],
                    "source_account": str(row["account_email"] or "").strip().lower(),
                    "sender_email": str(row["sender_email"] or "").strip().lower(),
                    "sender_display_name": str(row["sender_display_name"] or "").strip(),
                    "label_names": normalize_label_names(str(row["label_names"] or "").split(",")),
                    "has_label_tables": bool(row["has_label_tables"]),
                    "participants": [],
                }
            try:
                email = normalize_email(str(row["email"] or ""))
            except ValueError:
                continue
            message["participants"].append({
                "email": email,
                "recipient_type": str(row["recipient_type"] or "").strip().lower(),
                "recipient_display_name": str(row["recipient_display_name"] or "").strip(),
                "participant_display_name": str(row["participant_display_name"] or "").strip(),
            })
        if message is not None:
            _fold_msgvault_message(message, records, account_filter)

        out: list[dict[str, Any]] = []
        for email, record in records.items():
            display_name = best_display_name(email, record["names"])
            automated, automated_reason = is_automated_email(email)
            out.append({
                "email": email,
                "display_name": display_name,
                "total_sent": record["sent_messages"],
                "total_received": record["received_messages"],
                "total_messages": record["all_messages"],
                "one_to_one_sent": record["one_to_one_sent_messages"],
                "one_to_one_received": record["one_to_one_received_messages"],
                "one_to_one_messages": record["one_to_one_messages"],
                "group_sent": record["group_sent_messages"],
                "group_received": record["group_received_messages"],
                "group_messages": record["group_messages"],
                "one_to_one_thread_count": len(record["one_to_one_threads"]),
                "group_thread_count": len(record["group_threads"]),
                "thread_count": len(record["threads"]),
                "first_interaction": record["first_interaction"],
                "last_interaction": record["last_interaction"],
                "account_emails": sorted(record["accounts"]),
                "source_ids": sorted(record["source_ids"]),
                "primary_email_type": classify_email(email),
                "automated_filtered": automated,
                "automated_reason": automated_reason,
            })
        out.sort(key=lambda row: (-int(row["total_messages"]), str(row["email"])))
        return out

    def list_accounts(self) -> list[dict[str, Any]]:
        """List Gmail source accounts in the archive with their message counts."""
        rows = self.con.execute("""
            SELECT
                s.id AS source_id,
                s.identifier AS account_email,
                s.display_name AS display_name,
                COUNT(DISTINCT m.id) AS message_count
            FROM sources s
            LEFT JOIN messages m ON m.source_id = s.id
            WHERE (s.source_type IS NULL OR LOWER(s.source_type) = 'gmail')
              AND s.identifier IS NOT NULL
              AND TRIM(s.identifier) != ''
            GROUP BY s.id, s.identifier, s.display_name
            ORDER BY LOWER(s.identifier)
        """).fetchall()
        accounts: list[dict[str, Any]] = []
        for row in rows:
            email = str(row["account_email"] or "").strip().lower()
            if not email:
                continue
            accounts.append({
                "source_id": str(row["source_id"]),
                "account_email": email,
                "display_name": str(row["display_name"] or ""),
                "message_count": int(row["message_count"] or 0),
            })
        return accounts

    def fetch_recent_rows(self, email: str, fetch_limit: int) -> list[sqlite3.Row]:
        """Recent sender+recipient messages (with bodies) for a contact, via the
        participant-id indexes.

        Per-contact path, backing ``build_email_context.recent_emails_for`` and
        the unit tests. The all-contacts build path uses
        ``stream_contact_groups`` (one windowed query) instead."""
        con = self.con
        ids = [r["id"] for r in con.execute(PARTICIPANT_IDS_SQL, (email.lower(),)).fetchall()]
        if not ids:
            return []
        rows: list[sqlite3.Row] = []
        for pid in ids:
            rows.extend(con.execute(RECENT_EMAILS_FROM_SENDER_SQL, (pid, fetch_limit)).fetchall())
            rows.extend(con.execute(RECENT_EMAILS_TO_RECIPIENT_SQL, (pid, fetch_limit)).fetchall())
        rows.sort(key=lambda row: str(row["at"] or ""), reverse=True)
        return rows[:fetch_limit]

    def create_candidate_pid_table(self, emails: Iterable[str]) -> int:
        """(Re)build temp tables mapping each candidate email -> participant id(s).

        One O(participants) scan up front, instead of a per-contact id lookup.
        Returns the number of (email, pid) rows mapped. Temp tables are created on
        the temp database, so this works on the read-only main connection."""
        con = self.con
        con.execute("DROP TABLE IF EXISTS cand_pid")
        con.execute("DROP TABLE IF EXISTS cand_email")
        con.execute("CREATE TEMP TABLE cand_email(email TEXT PRIMARY KEY)")
        con.executemany(
            "INSERT OR IGNORE INTO cand_email(email) VALUES (?)",
            [(e.strip().lower(),) for e in emails if e and e.strip()],
        )
        con.execute(
            """
            CREATE TEMP TABLE cand_pid AS
            SELECT ce.email AS cemail, p.id AS pid
            FROM cand_email ce
            JOIN participants p ON LOWER(p.email_address) = ce.email
            """
        )
        con.execute("CREATE INDEX cand_pid_pid ON cand_pid(pid)")
        return con.execute("SELECT COUNT(*) AS n FROM cand_pid").fetchone()["n"]

    def stream_contact_groups(self, fetch_limit: int) -> Iterator[tuple[str, list[sqlite3.Row]]]:
        """Yield ``(contact_email, recent_rows)`` from the windowed query, one contact
        at a time. Requires ``create_candidate_pid_table`` to have run first. Only one
        contact's rows are materialized at a time, so memory stays bounded."""
        cur = self.con.execute(WINDOWED_CONTEXT_SQL, (fetch_limit,))
        for cemail, group in itertools.groupby(cur, key=lambda r: r["cemail"]):
            yield cemail, list(group)

    def account_emails(self) -> set[str]:
        """Lowercased synced account addresses, used to infer message direction.

        msgvault's Gmail sync leaves ``is_from_me`` = 0 for every row, so direction
        is derived from whether the sender is one of the synced accounts instead.
        """
        rows = self.con.execute("SELECT LOWER(identifier) AS ident FROM sources").fetchall()
        return {str(r["ident"]).strip() for r in rows if str(r["ident"] or "").strip()}

    def owner_identity(self) -> dict[str, Any]:
        """Who the mailbox owner is, derived entirely from msgvault (no flags needed).

        Emails = every synced ``sources.identifier`` (already what ``account_emails``
        returns). Name = the most common non-empty ``participants.display_name`` seen
        for any of those addresses (Gmail leaves ``sources.display_name`` blank, but the
        owner's name shows up on the participant rows). Used to tell the LLM marker step
        who 'me' is so it never mints a marker from the owner's own identity."""
        emails = sorted(self.account_emails())
        name = ""
        if emails:
            placeholders = ",".join("?" for _ in emails)
            row = self.con.execute(
                f"SELECT TRIM(display_name) AS dn, COUNT(*) AS n FROM participants "
                f"WHERE LOWER(email_address) IN ({placeholders}) "
                f"AND TRIM(COALESCE(display_name, '')) <> '' "
                f"GROUP BY TRIM(display_name) ORDER BY n DESC, dn LIMIT 1",
                emails,
            ).fetchone()
            if row:
                name = str(row["dn"]).strip()
        return {"name": name, "emails": emails}

    def count_messages_for(self, email: str, accounts: set[str]) -> int:
        """True total of the messages the body selector draws from for this contact:
        messages the contact SENT, plus messages an owner account sent where the contact
        is a recipient. Mirrors the selector's sender filter (and its not-deleted / email
        guards) so ``capped`` is honest — it excludes third-party-sent mail the selector
        always drops, unlike a naive "every message in the contact's threads" count.
        """
        con = self.con
        contact_ids = [r["id"] for r in con.execute(PARTICIPANT_IDS_SQL, (email.lower(),)).fetchall()]
        if not contact_ids:
            return 0
        owner_ids: list[Any] = []
        if accounts:
            oph = ",".join("?" for _ in accounts)
            owner_ids = [r["id"] for r in con.execute(
                f"SELECT id FROM participants WHERE LOWER(email_address) IN ({oph})",
                tuple(sorted(a.lower() for a in accounts))).fetchall()]
        cph = ",".join("?" for _ in contact_ids)
        not_deleted = ("AND (m.deleted_at IS NULL OR m.deleted_at = '') "
                       "AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')")
        arms = [f"SELECT m.id FROM messages m WHERE m.message_type='email' {not_deleted} "
                f"AND m.sender_id IN ({cph})"]
        params: list[Any] = list(contact_ids)
        if owner_ids:
            oin = ",".join("?" for _ in owner_ids)
            arms.append(f"SELECT m.id FROM message_recipients mr JOIN messages m ON m.id = mr.message_id "
                        f"WHERE m.message_type='email' {not_deleted} "
                        f"AND mr.participant_id IN ({cph}) AND m.sender_id IN ({oin})")
            params += list(contact_ids) + list(owner_ids)
        # UNION (not UNION ALL) dedupes a message matched by both arms.
        sql = f"SELECT COUNT(*) AS n FROM ({' UNION '.join(arms)})"
        return int(con.execute(sql, params).fetchone()["n"])


# --- Person-vs-role classification (moved from the retired resolve_queue.py) ---

GENERIC_PREFIXES = {
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply",
    "info", "contact", "support", "help", "hello",
    "sales", "marketing", "hr", "careers", "jobs",
    "admin", "administrator", "webmaster", "postmaster",
    "office", "team", "staff", "general",
    "billing", "invoices", "payments", "accounts", "accounting",
    "newsletter", "news", "updates", "notifications",
    "feedback", "enquiries", "inquiries",
    "service", "customerservice", "care", "dispatch",
    "concierge", "reservation", "reservations", "booking", "bookings",
    "rsvp", "registration", "club", "member", "members", "membership", "memberservices",
    "optical", "photos", "equity", "futures",
    "launch", "eat", "pbx", "alumni", "masters", "csi",
    "studentinfo", "fintechsupport", "casasupport",
}

GENERIC_KEYWORDS = {
    "support", "service", "noreply", "reply", "taskforce",
    "insurance", "verification", "recognition",
}

BUSINESS_NAME_KEYWORDS = {
    "llc", "inc", "corp", "ltd", "team", "services", "service",
    "spa", "optometry", "electronics", "insurance", "association",
    "department", "office", "institute", "run", "discount", "massages",
    "management", "concierge", "dispatch", "accounting", "task force",
    "hawaii", "waikiki", "aruba", "support", "delivery",
    "wines", "coffee", "mason", "security", "motors",
}

def is_likely_person_name(name: str) -> bool:
    """Return True if the name looks like a real person (first + last)."""
    if not name:
        return False
    clean = re.sub(r'\s*\([^)]*\)', '', name).strip()  # strip parentheticals like (LinkedIn Supplier)
    clean = re.sub(r'^["\']|["\']$', '', clean).strip()
    words = clean.split()
    if len(words) < 2:
        return False
    if any(kw in clean.lower() for kw in BUSINESS_NAME_KEYWORDS):
        return False
    if '&' in clean:
        return False
    # All-caps or all-lower single tokens that aren't name-like
    if clean == clean.upper() and len(words) <= 2:
        return False
    return True


def is_generic_or_non_person(email: str) -> bool:
    """Return True if the email looks like a role/service address, not a person."""
    if not email or "@" not in email:
        return True
    local = email.split("@")[0].lower().strip()
    # Strip plus-addressing
    local = local.split("+")[0]
    # Exact prefix match
    if local in GENERIC_PREFIXES:
        return True
    # First segment match (e.g. customer.service@, no-reply@, info-mhi@)
    base = re.split(r'[.\-_]', local)[0]
    if base in GENERIC_PREFIXES:
        return True
    # Contains generic keyword anywhere
    for kw in GENERIC_KEYWORDS:
        if kw in local:
            return True
    # Phone-number-like local parts
    if re.match(r'^\d{7,}$', local):
        return True
    # Single character local parts
    if len(local) <= 1:
        return True
    # Local part is just digits (e.g. 2relaxinparadise is fine but pure digits aren't)
    if re.match(r'^\d+$', local):
        return True
    return False
