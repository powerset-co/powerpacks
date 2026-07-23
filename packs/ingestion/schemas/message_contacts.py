"""The message-contact CSV contract for ingestion primitives.

The ONE home for the constants that define the iMessage/WhatsApp message-contact
CSV shape. The discover message primitives (extract_imessage, whatsapp_wacli,
merge_contacts, normalize_contacts), the discovery orchestrator (discover), the
imports matcher (match_local_candidates), and the shared contact-field helpers
(common/contact_fields) each carried a hand-copied, byte-identical copy of these;
they now import from here so the contract has a single definition.

Companion schema docs (human- and machine-readable) live beside this module and
are referenced by the path constants below:

- `contacts-csv.md` — prose schema doc (`SCHEMA_DOC`).
- `contacts-csv.schema.json` — JSON Schema (`SCHEMA_JSON`).

Changelog:
  2026-07-23 (audit consolidation): created; absorbs the byte-identical
    CSV_HEADERS / REQUIRED_INPUT_HEADERS / GROUP_SEPARATOR / MESSAGE_CHANNELS /
    SCHEMA_DOC / SCHEMA_JSON copies (and discover.py's identically-shaped
    CONTACT_CSV_HEADERS, which now imports CSV_HEADERS) from the discover/messages
    primitives, imports/messages/match_local_candidates, and
    common/contact_fields.
"""

from __future__ import annotations

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
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
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
