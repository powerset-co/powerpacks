"""Messages-vertical utilities: the `contacts.csv` row model and column
ownership, tolerant field parsers, message-contact field readers, and the two
POLICY functions the import applies to a contact row — the deterministic "worth
researching" floor (`contact_floor_reason`) and the selection decision built on
it (`classify_contact`). Both are first-rule-wins and readable end to end; the
import's loop consumes their verdicts instead of interleaving them with its
bookkeeping.

CSV cells arrive as arbitrary user/state text; the parsers never raise — they map
unparseable input to a neutral value (None / 0 / "") so row processing stays
total.

`.powerpacks/messages/contacts.csv` has TWO writers, which is why the ownership
constants live here rather than being implied by whoever wrote a column last:

  discovery (`discover/messages/`)   emits all 19 columns and owns the VALUES of
                                     the 11 metadata ones
  the matcher (`match_local_candidates.py`)  owns the VALUES of the 7 in
                                     MATCH_ANNOTATION_COLUMNS and rewrites only
                                     those, in place
  `skip`                             is owned by NEITHER — see USER_OWNED_COLUMNS

Changelog:
  2026-07-30 (no re-export hop): this module no longer imports
    `MessageContactRow` purely so its two consumers could reach it here. Both
    name the module that DEFINES it (`discover/messages/models.py`) — the graph
    checker compares row models by identity, and a second module handing the
    same object out is one more place that can be asked to hand out a different
    one. The now-unused `row_model_for` / `CSV_HEADERS` imports went with it.
  2026-07-30 (visible decision): added `classify_contact` / `ContactSelection`,
    lifted out of `importer.selected_contacts_people`, where the same rules were
    spelled as an inline status test, a mid-loop `skip("suggested_not_attached")`
    with a comment explaining that the row keeps going, and a floor call —
    interleaved with three accumulators, so reading the policy meant simulating
    the loop. Also DELETED the dead `message_source` fallback from
    `messages_source_channels`: `contacts.csv` has one `source` column
    (`schemas/message_contacts.CSV_HEADERS`), `message_source` is a LEGACY INPUT
    header the schema-mismatch error tells a user to rename, and both readers of
    this file normalize rows to the canonical headers before any import code sees
    them, so the second key could never be the one that was set.
  2026-07-25 (declared contract): added `MessageContactRow`,
    `MATCH_ANNOTATION_COLUMNS`, and `USER_OWNED_COLUMNS` so the two writers of
    contacts.csv declare disjoint `owns_columns`. Moved the mid-file `import re`
    and `latest_interaction` import into the top-of-file block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packs.ingestion.schemas.people_schema import latest_interaction

# The columns `match_local_candidates.py` writes, and the ONLY ones it writes.
# It rewrites the whole file to update them (csv has no in-place cell write), so
# its declared `writes` mode is "annotate", not "full_rewrite".
MATCH_ANNOTATION_COLUMNS = (
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
)

# Owned by NEITHER writer, and deliberately in neither `owns_columns` tuple.
# `skip` is documented in schemas/contacts-csv.md as "yes/true to exclude from
# research" — a USER mark. The discovery extractors seed it empty/False and
# merge_contacts ORs whatever it reads, but no code path anywhere sets it true
# (verified 2026-07-25: 0 of 873 real rows carry a value). Its only reader is
# `contact_floor_reason` below. Claiming it for a writer would be a lie that
# lets a future writer clobber a user's mark.
USER_OWNED_COLUMNS = ("skip",)

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


def normalize_bool(value: Any) -> bool | None:
    """Tri-state bool: True/False for recognized tokens, None for anything else."""
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return None


def parse_int_field(value: Any) -> int:
    """Int from a CSV cell ('42', '42.0', '' -> 42, 42, 0); never raises."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def split_full_name(full_name: str) -> tuple[str, str]:
    """(first, rest) on the first whitespace; ('', '') for an empty name."""
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def normalize_phoneish(value: str) -> str:
    """Digits only — the comparable core of a phone-shaped string."""
    return "".join(ch for ch in value or "" if ch.isdigit())


DEFAULT_MIN_MESSAGE_COUNT = 1
# Group-appearance-only contacts below this volume are low-signal noise
# (someone from a group thread, not a relationship) unless opted in. A positive
# WhatsApp direct-chat count is explicit relationship evidence and bypasses it.
GROUP_ONLY_MIN_MESSAGES = 10

MIN_NAME_TOKENS = 2
MIN_TOKEN_LEN = 2
MIN_TOTAL_ALPHA = 5
BLOCKED_LAST_NAME_TOKENS = {"hinge", "raya", "tinder", "bumble"}
NAME_CLEAN_RE = re.compile(r"[^A-Za-zÀ-ÿ'’\-\s]")
MULTISPACE_RE = re.compile(r"\s+")
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


def normalize_name(name: str) -> str:
    """Strip non-name characters and collapse whitespace."""
    cleaned = NAME_CLEAN_RE.sub(" ", name or "")
    return MULTISPACE_RE.sub(" ", cleaned).strip()


def normalize_last_name_tokens(name: str) -> set[str]:
    """Lowercased tokens after the first name ('' names -> empty set)."""
    cleaned = normalize_name(name).lower()
    parts = cleaned.split()
    if len(parts) < 2:
        return set()
    return {token for token in parts[1:] if token}


def has_searchable_name(name: str) -> bool:
    """True when the saved name has enough real tokens/letters to research."""
    cleaned = normalize_name(name)
    if not cleaned:
        return False
    tokens = [t for t in cleaned.split(" ") if len(t) >= MIN_TOKEN_LEN]
    if len(tokens) < MIN_NAME_TOKENS:
        return False
    alpha = sum(1 for ch in cleaned if ch.isalpha())
    return alpha >= MIN_TOTAL_ALPHA


def bad_name_reason(name: str, phone: str = "") -> str:
    """First reason the saved name is unusable ("" = usable): the name is just
    the phone number, empty, carries a blocked app token (dating-app saves),
    or is too thin to research."""
    phone_digits = normalize_phoneish(phone)
    raw_name_digits = normalize_phoneish(name)
    if phone_digits and raw_name_digits and phone_digits.endswith(raw_name_digits):
        return "name_is_phone"
    cleaned = normalize_name(name)
    if not cleaned:
        return "no_name"
    if normalize_last_name_tokens(cleaned) & BLOCKED_LAST_NAME_TOKENS:
        return "blocked_name_token"
    if not has_searchable_name(cleaned):
        return "bad_name"
    return ""


def has_whatsapp_direct_messages(row: dict[str, str]) -> bool:
    """Whether discovery found a real WhatsApp direct-message thread."""
    return parse_int_field(row.get("whatsapp_message_count")) > 0


def contact_floor_reason(
    row: dict[str, str],
    *,
    min_message_count: int,
    include_group_only: bool,
) -> str:
    """First failing floor reason for an unmatched contact ("" = passes)."""
    phone = (row.get("phone") or "").strip()
    if "@" in phone:
        return "email_handle"
    digits = normalize_phoneish(phone)
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return "short_code_or_invalid_phone"
    if normalize_bool(row.get("skip", "")) is True:
        return "skip_flag"
    name_reason = bad_name_reason(row.get("name") or "", phone)
    if name_reason:
        return name_reason
    message_count = parse_int_field(row.get("message_count"))
    if message_count < min_message_count:
        return "below_min_messages"
    if (
        not include_group_only
        and normalize_bool(row.get("is_in_group_chats", "")) is True
        and not has_whatsapp_direct_messages(row)
        and message_count < GROUP_ONLY_MIN_MESSAGES
    ):
        return "group_only_low_signal"
    return ""


def messages_source_channels(row: dict[str, str]) -> list[str]:
    """Channels the contact was seen on ('imessage'/'whatsapp'), from the
    source column plus any positive per-channel count; ['messages'] fallback."""
    channels: list[str] = []
    raw = (row.get("source") or "").strip().lower()
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        if parse_int_field(row.get(key)) > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def contact_interaction_counts(row: dict[str, str]) -> dict[str, int]:
    """Positive per-channel DM counts, keyed by channel."""
    counts: dict[str, int] = {}
    for count_key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        count = parse_int_field(row.get(count_key))
        if count > 0:
            counts[channel] = count
    return counts


def contact_last_interaction(row: dict[str, str]) -> str:
    """Most recent activity across the per-channel and legacy last-message columns."""
    return latest_interaction(
        row.get("imessage_last_message"),
        row.get("whatsapp_last_message"),
        row.get("last_message"),
    )


# --- what the import does with one contact row --------------------------------

MATCHED = "matched"
CANDIDATE = "candidate"
DROPPED = "dropped"


@dataclass(frozen=True)
class ContactSelection:
    """What the import does with one `contacts.csv` row, and why.

    `outcome` is one of MATCHED (attach message activity to the person the
    matcher resolved), CANDIDATE (a research candidate for deep-context), or
    DROPPED. `skips` are the skip counters this row contributes to the manifest,
    in the order they are recorded — a row can contribute more than one, because
    a parked suggestion is counted whether or not the row then clears the floor.
    """

    outcome: str
    skips: tuple[str, ...] = ()


def classify_contact(
    row: dict[str, str],
    *,
    min_message_count: int,
    include_group_only: bool,
) -> ContactSelection:
    """Decide one contact row. First rule wins:

    1. a `matched` row carrying a resolved person id attaches to that person;
    2. a `suggested` row is NEVER auto-attached — the deep-context cluster judge
       decides — so it is counted `suggested_not_attached`, parked in candidate
       evidence, and then floor-tested like any unmatched row;
    3. a row failing the deterministic worth-researching floor is dropped,
       carrying that floor's reason;
    4. anything left is a research candidate.

    Deduplication is NOT decided here: whether a row collides with one already
    kept is a property of the run so far, not of the row, so the import's loop
    owns those counters (`duplicate_matched_person`, `duplicate_phone`).
    """
    match_status = (row.get("match_status") or "").strip().lower()
    if match_status == "matched" and (row.get("matched_person_id") or "").strip():
        return ContactSelection(MATCHED)
    parked = ("suggested_not_attached",) if match_status == "suggested" else ()
    floor_reason = contact_floor_reason(
        row,
        min_message_count=min_message_count,
        include_group_only=include_group_only,
    )
    if floor_reason:
        return ContactSelection(DROPPED, (*parked, floor_reason))
    return ContactSelection(CANDIDATE, parked)
