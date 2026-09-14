"""Message-contact CSV columns and parsed import values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Canonical, ordered column list for the message-contact CSV that the
# iMessage/WhatsApp discovery and import stages read and write. This IS the
# on-disk header order — do not reorder without updating the companion schema
# docs and every consumer.
CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "imessage_message_count",
    "whatsapp_message_count",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
]

# The minimum input columns a source CSV must carry to be accepted by the
# message stages (everything else in CSV_HEADERS is derived/optional on input).
REQUIRED_INPUT_HEADERS = {"phone", "name"}

# Separator that joins multiple group names inside the single `group_names` cell.
GROUP_SEPARATOR = " | "

# Message source channels the contract tracks dedicated per-channel
# `<channel>_message_count` / `<channel>_last_message` columns for.
MESSAGE_CHANNELS = ("imessage", "whatsapp")

# Repo-relative paths to the companion schema docs, surfaced in schema-mismatch
# errors so a user can convert a legacy CSV into this contract.
SCHEMA_DOC = "packs/ingestion/schemas/contacts-csv.md"
SCHEMA_JSON = "packs/ingestion/schemas/contacts-csv.schema.json"


def _parse_int(value: str | None) -> int:
    """Parse decimal count cells; blank or nonnumeric cells count as zero."""
    text = (value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


@dataclass(frozen=True)
class MessageContact:
    """Contact metadata parsed once from a message-contact CSV row."""

    phone: str
    name: str
    source: str
    imessage_message_count: int
    whatsapp_message_count: int
    last_message: str
    imessage_last_message: str
    whatsapp_last_message: str

    @classmethod
    def from_csv_row(cls, row: Mapping[str, str]) -> MessageContact:
        return cls(
            phone=(row.get("phone") or "").strip(),
            name=(row.get("name") or "").strip(),
            source=(row.get("source") or "").strip().lower(),
            imessage_message_count=_parse_int(row.get("imessage_message_count")),
            whatsapp_message_count=_parse_int(row.get("whatsapp_message_count")),
            last_message=row.get("last_message") or "",
            imessage_last_message=row.get("imessage_last_message") or "",
            whatsapp_last_message=row.get("whatsapp_last_message") or "",
        )
