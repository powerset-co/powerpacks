"""Discovery metadata queries and per-contact aggregation for msgvault."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from packs.ingestion.primitives.discover.gmail.msgvault.util import (
    best_display_name,
    canonical_message_id,
    classify_email,
    is_automated_email,
    normalize_email,
    normalize_label_names,
)


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def has_label_tables(con: sqlite3.Connection) -> bool:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name IN ('labels', 'message_labels')"
    ).fetchall()
    return {str(row[0]) for row in rows} == {"labels", "message_labels"}


def iter_metadata(
    con: sqlite3.Connection,
    account_email: str = "",
    exclude_labels: Iterable[str] | None = None,
    *,
    stream_order: bool = False,
) -> Iterator[sqlite3.Row]:
    """Yield one row per message recipient from msgvault metadata tables."""
    labels = normalize_label_names(exclude_labels)
    label_filter = ""
    params: list[Any] = [account_email, account_email]
    labels_present = has_label_tables(con)
    if labels and labels_present:
        placeholders = ",".join("?" for _ in labels)
        label_filter = f"""
          AND NOT EXISTS (
              SELECT 1 FROM message_labels ml
              JOIN labels l ON l.id = ml.label_id
              WHERE ml.message_id = m.id
                AND UPPER(l.name) IN ({placeholders})
          )
        """
        params.extend(labels)

    message_columns = _table_columns(con, "messages")
    sender_join = ""
    sender_select = "NULL AS sender_email, NULL AS sender_display_name,"
    if "sender_id" in message_columns:
        sender_join = "LEFT JOIN participants sender_p ON sender_p.id = m.sender_id"
        sender_select = (
            "sender_p.email_address AS sender_email, "
            "sender_p.display_name AS sender_display_name,"
        )
    rfc822_select = (
        "m.rfc822_message_id AS rfc822_message_id,"
        if "rfc822_message_id" in message_columns
        else "NULL AS rfc822_message_id,"
    )
    source_msg_select = (
        "m.source_message_id AS source_message_id,"
        if "source_message_id" in message_columns
        else "NULL AS source_message_id,"
    )
    rfc822_col = (
        "m.rfc822_message_id" if "rfc822_message_id" in message_columns else "NULL"
    )
    source_col = (
        "m.source_message_id" if "source_message_id" in message_columns else "NULL"
    )
    order_clause = "LOWER(p.email_address), m.id"
    if stream_order:
        order_clause = (
            f"COALESCE(NULLIF(TRIM({rfc822_col}), ''), "
            f"NULLIF(TRIM({source_col}), ''), 'row:' || m.id), "
            "LOWER(p.email_address), m.id"
        )
    label_select = "'' AS label_names"
    labels_flag = "0 AS has_label_tables"
    if labels_present:
        labels_flag = "1 AS has_label_tables"
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
            {labels_flag},
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
        labels_flag=labels_flag,
        rfc822_select=rfc822_select,
        source_msg_select=source_msg_select,
        sender_join=sender_join,
        label_filter=label_filter,
        order_clause=order_clause,
    )
    yield from con.execute(query, params)






def list_accounts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute("""
        SELECT s.id AS source_id, s.identifier AS account_email,
               s.display_name AS display_name, COUNT(DISTINCT m.id) AS message_count
        FROM sources s LEFT JOIN messages m ON m.source_id = s.id
        WHERE (s.source_type IS NULL OR LOWER(s.source_type) = 'gmail')
          AND s.identifier IS NOT NULL AND TRIM(s.identifier) != ''
        GROUP BY s.id, s.identifier, s.display_name
        ORDER BY LOWER(s.identifier)
    """).fetchall()
    return [{
        "source_id": str(row["source_id"]),
        "account_email": str(row["account_email"] or "").strip().lower(),
        "display_name": str(row["display_name"] or ""),
        "message_count": int(row["message_count"] or 0),
    } for row in rows if str(row["account_email"] or "").strip()]

@dataclass(frozen=True)
class _Participant:
    email: str
    recipient_type: str
    recipient_display_name: str
    participant_display_name: str


@dataclass(frozen=True)
class _MessageMetadata:
    conversation_id: str | None
    message_at: str
    source_id: str | None
    source_account: str
    sender_email: str
    sender_display_name: str
    label_names: tuple[str, ...]
    has_label_tables: bool
    participants: list[_Participant] = field(default_factory=list)


@dataclass
class _InteractionCounts:
    sent: int = 0
    received: int = 0
    threads: set[str] = field(default_factory=set)


@dataclass
class _ContactAccumulator:
    names: dict[str, int] = field(default_factory=dict)
    one_to_one: _InteractionCounts = field(default_factory=_InteractionCounts)
    group: _InteractionCounts = field(default_factory=_InteractionCounts)
    accounts: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    first_interaction: str = ""
    last_interaction: str = ""


def _fold_msgvault_message(
    message: _MessageMetadata, records: dict[str, _ContactAccumulator], account_filter: str,
) -> None:
    """Count each canonical message once per contact and direction."""
    source_account = message.source_account
    if account_filter and source_account != account_filter:
        return
    participants = message.participants
    from_emails = {p.email for p in participants if p.recipient_type == "from"}
    sender_email = message.sender_email
    if sender_email:
        from_emails.add(sender_email)
    has_recipient = any(p.recipient_type in {"to", "cc", "bcc"} for p in participants)
    if message.has_label_tables:
        is_sent = "SENT" in message.label_names
    else:
        is_sent = (bool(source_account) and source_account in from_emails) or (not from_emails and has_recipient)
    external_emails = {p.email for p in participants if p.email and p.email != source_account}
    if sender_email and sender_email != source_account:
        external_emails.add(sender_email)
    is_group = len(external_emails) > 1
    if not any(p.recipient_type == "from" for p in participants) and sender_email:
        participants = participants + [_Participant(
            email=sender_email,
            recipient_type="from",
            recipient_display_name=message.sender_display_name,
            participant_display_name=message.sender_display_name,
        )]
    counted_for_message: set[tuple[str, str]] = set()
    for participant in participants:
        email = participant.email
        if not email or email == source_account or (account_filter and email == account_filter):
            continue
        if is_sent and participant.recipient_type in {"to", "cc", "bcc"}:
            direction = "sent"
        elif not is_sent and participant.recipient_type == "from":
            direction = "received"
        else:
            continue
        dedupe_key = (email, direction)
        if dedupe_key in counted_for_message:
            continue
        counted_for_message.add(dedupe_key)
        record = records.setdefault(email, _ContactAccumulator())
        for name in (participant.recipient_display_name, participant.participant_display_name):
            if name:
                record.names[name] = record.names.get(name, 0) + 1
        counts = record.group if is_group else record.one_to_one
        if direction == "sent":
            counts.sent += 1
        else:
            counts.received += 1
        if message.conversation_id is not None:
            counts.threads.add(message.conversation_id)
        if message.source_id is not None:
            record.source_ids.add(message.source_id)
        if source_account:
            record.accounts.add(source_account)
        if message.message_at:
            if not record.first_interaction or message.message_at < record.first_interaction:
                record.first_interaction = message.message_at
            if not record.last_interaction or message.message_at > record.last_interaction:
                record.last_interaction = message.message_at


def aggregate_contacts(con: sqlite3.Connection, account_email: str = "", exclude_labels: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Aggregate msgvault contact metadata into per-person interaction records.

    Streams rows ordered so every row of one canonical message is contiguous,
    folding one message at a time instead of materializing all messages. Peak
    memory becomes O(unique contacts) + one buffered message instead of
    O(total messages). Output is byte-identical to the materialized path.
    """
    account_filter = account_email.strip().lower()
    records: dict[str, _ContactAccumulator] = {}
    current_key: str | None = None
    message: _MessageMetadata | None = None
    for row in iter_metadata(con, account_filter, exclude_labels, stream_order=True):
        msg_id = canonical_message_id(row)
        if msg_id != current_key:
            if message is not None:
                _fold_msgvault_message(message, records, account_filter)
            current_key = msg_id
            message = _MessageMetadata(
                conversation_id=str(row["conversation_id"]) if row["conversation_id"] is not None else None,
                message_at=str(row["message_at"] or "").strip(),
                source_id=str(row["source_id"]) if row["source_id"] is not None else None,
                source_account=str(row["account_email"] or "").strip().lower(),
                sender_email=str(row["sender_email"] or "").strip().lower(),
                sender_display_name=str(row["sender_display_name"] or "").strip(),
                label_names=tuple(normalize_label_names(str(row["label_names"] or "").split(","))),
                has_label_tables=bool(row["has_label_tables"]),
            )
        try:
            email = normalize_email(str(row["email"] or ""))
        except ValueError:
            continue
        message.participants.append(_Participant(
            email=email,
            recipient_type=str(row["recipient_type"] or "").strip().lower(),
            recipient_display_name=str(row["recipient_display_name"] or "").strip(),
            participant_display_name=str(row["participant_display_name"] or "").strip(),
        ))
    if message is not None:
        _fold_msgvault_message(message, records, account_filter)

    out: list[dict[str, Any]] = []
    for email, record in records.items():
        display_name = best_display_name(email, record.names)
        automated, automated_reason = is_automated_email(email)
        out.append({
            "email": email,
            "display_name": display_name,
            "total_sent": record.one_to_one.sent + record.group.sent,
            "total_received": record.one_to_one.received + record.group.received,
            "total_messages": record.one_to_one.sent + record.one_to_one.received + record.group.sent + record.group.received,
            "one_to_one_sent": record.one_to_one.sent,
            "one_to_one_received": record.one_to_one.received,
            "one_to_one_messages": record.one_to_one.sent + record.one_to_one.received,
            "group_sent": record.group.sent,
            "group_received": record.group.received,
            "group_messages": record.group.sent + record.group.received,
            "one_to_one_thread_count": len(record.one_to_one.threads),
            "group_thread_count": len(record.group.threads),
            "thread_count": len(record.one_to_one.threads | record.group.threads),
            "first_interaction": record.first_interaction,
            "last_interaction": record.last_interaction,
            "account_emails": sorted(record.accounts),
            "source_ids": sorted(record.source_ids),
            "primary_email_type": classify_email(email),
            "automated_filtered": automated,
            "automated_reason": automated_reason,
        })
    out.sort(key=lambda row: (-row["total_messages"], row["email"]))
    return out
