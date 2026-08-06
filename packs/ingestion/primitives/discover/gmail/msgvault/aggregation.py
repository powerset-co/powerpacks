"""Discovery metadata queries and per-contact aggregation for msgvault."""

from __future__ import annotations

import sqlite3
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


def _fold_message(
    message: dict[str, Any],
    records: dict[str, dict[str, Any]],
    account_filter: str,
) -> None:
    source_account = str(message.get("source_account") or "").strip().lower()
    if account_filter and source_account != account_filter:
        return
    participants = message.get("participants") or []
    from_emails = {
        str(p.get("email") or "").strip().lower()
        for p in participants
        if p.get("recipient_type") == "from"
    }
    sender_email = str(message.get("sender_email") or "").strip().lower()
    if sender_email:
        from_emails.add(sender_email)
    labels = set(message.get("label_names") or [])
    has_explicit_from = bool(from_emails)
    has_recipient = any(
        p.get("recipient_type") in {"to", "cc", "bcc"} for p in participants
    )
    is_sent = (
        "SENT" in labels
        if message.get("has_label_tables")
        else (
            (bool(source_account) and source_account in from_emails)
            or (not has_explicit_from and has_recipient)
        )
    )
    external_emails = {
        str(p.get("email") or "").strip().lower()
        for p in participants
        if p.get("email")
        and str(p.get("email")).strip().lower() != source_account
    }
    if sender_email and sender_email != source_account:
        external_emails.add(sender_email)
    message_kind = "group" if len(external_emails) > 1 else "one_to_one"
    if not any(p.get("recipient_type") == "from" for p in participants) and sender_email:
        participants = list(participants) + [{
            "email": sender_email,
            "recipient_type": "from",
            "recipient_display_name": str(message.get("sender_display_name") or ""),
            "participant_display_name": str(message.get("sender_display_name") or ""),
        }]

    counted: set[tuple[str, str, str]] = set()
    for participant in participants:
        email = str(participant.get("email") or "").strip().lower()
        if not email or email == source_account or (account_filter and email == account_filter):
            continue
        recipient_type = str(participant.get("recipient_type") or "")
        direction = ""
        if is_sent and recipient_type in {"to", "cc", "bcc"}:
            direction = "sent"
        elif not is_sent and recipient_type == "from":
            direction = "received"
        if not direction:
            continue
        dedupe_key = (str(message["message_id"]), email, direction)
        if dedupe_key in counted:
            continue
        counted.add(dedupe_key)
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
        record[f"{message_kind}_messages"] += 1
        record[f"{message_kind}_{direction}_messages"] += 1
        record[f"{direction}_messages"] += 1
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


def aggregate_contacts(
    con: sqlite3.Connection,
    account_email: str = "",
    exclude_labels: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    account_filter = account_email.strip().lower()
    records: dict[str, dict[str, Any]] = {}
    current_key: str | None = None
    message: dict[str, Any] | None = None
    for row in iter_metadata(con, account_filter, exclude_labels, stream_order=True):
        msg_id = canonical_message_id(row)
        if msg_id != current_key:
            if message is not None:
                _fold_message(message, records, account_filter)
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
        _fold_message(message, records, account_filter)

    out: list[dict[str, Any]] = []
    for email, record in records.items():
        automated, automated_reason = is_automated_email(email)
        out.append({
            "email": email,
            "display_name": best_display_name(email, record["names"]),
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
