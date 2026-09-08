"""Map parsed Messages source metadata into canonical people rows."""

from __future__ import annotations

import re
from pathlib import Path

from packs.ingestion.schemas.candidates_schema import candidate_key_for
from packs.ingestion.schemas.message_contacts import MessageContact
from packs.ingestion.schemas.people_schema import latest_interaction, normalize_people_row


def messages_source_channels(row: MessageContact) -> list[str]:
    """Channels the contact was seen on ('imessage'/'whatsapp'), from the
    source column plus any positive per-channel count; ['messages'] fallback."""
    channels: list[str] = []
    raw = row.source
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for count, channel in (
        (row.imessage_message_count, "imessage"),
        (row.whatsapp_message_count, "whatsapp"),
    ):
        if count > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def contact_interaction_counts(row: MessageContact) -> dict[str, int]:
    """Positive per-channel DM counts, keyed by channel."""
    counts: dict[str, int] = {}
    for count, channel in (
        (row.imessage_message_count, "imessage"),
        (row.whatsapp_message_count, "whatsapp"),
    ):
        if count > 0:
            counts[channel] = count
    return counts


def contact_last_interaction(row: MessageContact) -> str:
    """Most recent activity across the per-channel and legacy last-message columns."""
    return latest_interaction(
        row.imessage_last_message,
        row.whatsapp_last_message,
        row.last_message,
    )


def contact_to_person(row: MessageContact, contacts_csv: Path) -> dict[str, str] | None:
    """Keep the source identity and metadata; only an absent key prevents import."""
    email = row.phone if "@" in row.phone else ""
    phone = "" if email else row.phone
    key = candidate_key_for(email, phone)
    if not key:
        return None

    name_parts = row.name.split(None, 1)
    counts = contact_interaction_counts(row)
    return normalize_people_row({
        "id": f"candidate:{key}",
        "full_name": row.name,
        "first_name": name_parts[0] if name_parts else "",
        "last_name": name_parts[1] if len(name_parts) > 1 else "",
        "primary_email": email,
        "all_emails": [email] if email else "",
        "primary_phone": phone,
        "all_phones": [phone] if phone else "",
        "source_channels": ",".join(messages_source_channels(row)),
        "source_artifacts": str(contacts_csv),
        "interaction_counts": counts or "",
        "last_interaction": contact_last_interaction(row),
    })
