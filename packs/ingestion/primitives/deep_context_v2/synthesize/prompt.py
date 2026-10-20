"""The synthesis prompt: system text, the strict fact schema, message batching and batch rendering.

SYNTHESIS_VERSION hashes the prompt templates and the schema with a fixed placeholder owner,
so it is the same for every owner and moves only when the template text or the schema does.

Created: 2026-10-06
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageChannel, MessageDirection, MessageEntry

from packs.ingestion.primitives.deep_context_v2 import assets

_HERE = Path(__file__).parent
SYNTHESIS_CONTRACT_VERSION = "relationship-category-v6"


SYSTEM_PROMPT = assets.text(_HERE, "person_synthesis_system.txt")
EMAIL_SYSTEM_PROMPT = (
    "You build a rich profile of ONE person (the CONTACT) from selected messages. Gmail messages were sent "
    "by the contact or an account owner, but may be addressed to other people; use the actual per-message "
    "From/To/Cc/Bcc headers. The account owner addresses identify the mailbox owner. Cc does not imply "
    "nonparticipation, and membership of a thread is not proof of direct correspondence. Bodies are excerpts; "
    "quoted or forwarded text may remain and is not necessarily the sender's own words. Missing headers "
    "mean unknown recipients. Phone-message direction labels identify their senders. Extract durable facts "
    "ABOUT THE CONTACT — never attribute the owner's or another participant's identity to them."
    + "\n\n" + SYSTEM_PROMPT.split("\n\n", 1)[1]
)
OWNER_PROMPT_SUFFIX = f"\n\n{assets.text(_HERE, 'owner_context_suffix.txt')}\n\n"
OWNER_IDENTITY_CHECK = assets.text(_HERE, "owner_identity_check.txt")
FACT_SCHEMA: dict[str, Any] = assets.json_file(_HERE, "fact_schema.json")


def owner_identity_block(name: str, emails: Sequence[str]) -> str:
    """Tell the model who the owner is, so it can tell when a candidate is the owner on another address."""
    if not name and not emails:
        return ""
    rendered = OWNER_IDENTITY_CHECK.format(name=name, emails=", ".join(emails) or "unknown email")
    return f"\n\n{rendered}\n"


SYNTHESIS_VERSION = hashlib.sha1(
    json.dumps(
        {
            "contract": SYNTHESIS_CONTRACT_VERSION,
            "system_prompt": SYSTEM_PROMPT,
            "schema": FACT_SCHEMA,
            "owner_prompt_suffix": OWNER_PROMPT_SUFFIX,
            "owner_identity_check": OWNER_IDENTITY_CHECK,
            "owner_identity_block": owner_identity_block("OWNER_NAME", ("owner@example.test",)),
        },
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:12]  # a version tag, not a security digest

_DIRECTION_LABEL = {
    MessageDirection.FROM_ME: "ME",
    MessageDirection.FROM_THEM: "THEM",
    # A third participant in a shared group is not the candidate and must not read as one.
    MessageDirection.FROM_OTHER: "OTHER-IN-GROUP",
}


def render_batch(person: CollectionBundle, batch: Sequence[MessageEntry], *,
                 direction_labels: Mapping[MessageDirection, str] = _DIRECTION_LABEL) -> str:
    """One batch as the plain-text block the model reads: the candidate's keys, its threads, then the messages."""
    lines = [
        f"CONTACT: {person.full_name or '(unknown)'}",
        f"Known emails: {', '.join(person.emails) or '(none)'}",
        f"Known phones: {', '.join(person.phones) or '(none)'}",
        f"Channels: {', '.join(person.source_channels) or '(none)'}",
    ]
    if person.groups:
        lines.append(f"Shared group chats (names only): {', '.join(person.groups)}")
    if person.thread_participants:
        lines.append("")
        lines.append(
            "EMAIL THREAD PARTICIPANTS (thread context only; these are not per-message recipient roles):"
        )
        for thread in person.thread_participants[:25]:  # bounds prompt size
            lines.append(f"- {thread.subject or '(no subject)'} — {', '.join(thread.participants)}")
    lines.append("")
    lines.append("MESSAGES (most relevant, chronological):")
    if any(message.channel == MessageChannel.GMAIL for message in batch):
        lines.append("Gmail headers give actual addressing; Cc does not imply nonparticipation. "
                     "Bodies are excerpts and may retain quoted or forwarded words; do not assign those to the sender.")
    for message in batch:
        date = (message.at or "")[:10]
        if message.channel == MessageChannel.GMAIL:
            role = "" if message.email_headers is not None else f" {_DIRECTION_LABEL[message.direction]}; recipients unavailable"
            head = f"[{message.channel} {date}{role}]"
        else:
            head = f"[{message.channel} {date} {direction_labels[message.direction]}]"
        if message.subject:
            head += f" {message.subject}"
        if message.email_headers is not None:
            lines.append(f"{head}\n{message.email_headers.render()}\nBody excerpt: {message.text or ''}")
        else:
            lines.append(f"{head}: {message.text or ''}")
    return "\n".join(lines)


def _newest_first_key(message: MessageEntry) -> str:
    return message.at or ""


def batches(messages: Sequence[MessageEntry], *, chunk_chars: int, max_batches: int) -> list[list[MessageEntry]]:
    """Pack messages newest first into batches of at most `chunk_chars`; stop at `max_batches`, dropping the oldest.

    The sort is stable on `at` alone, so messages with equal timestamps keep the bundle's order.
    A single message longer than `chunk_chars` gets a batch of its own.
    """
    newest = sorted(messages, key=_newest_first_key, reverse=True)
    chunks: list[list[MessageEntry]] = []
    current: list[MessageEntry] = []
    used = 0
    for message in newest:
        size = message.content_chars()
        if current and used + size > chunk_chars:
            chunks.append(current)
            if len(chunks) == max_batches:
                return chunks
            current = []
            used = 0
        current.append(message)
        used += size
    if current:
        chunks.append(current)
    return chunks
