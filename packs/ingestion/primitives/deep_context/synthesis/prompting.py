"""Pinned schema, prompts, message batching, and prompt rendering for synthesis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    MessageEntry,
)
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.db.models import OwnerProfile

SYNTHESIS_CONTRACT_VERSION = "relationship-category-v6"
DEFAULT_TARGET_CONFIDENCE = 0.85
SYSTEM_PROMPT = load_prompt("person_synthesis_system")
OWNER_PROMPT_SUFFIX = f"\n\n{load_prompt('owner_context_suffix')}\n\n"
OWNER_IDENTITY_CHECK = load_prompt("owner_identity_check")
WORTH_POLICIES = {
    "mixed": load_prompt("worth_policy_mixed"),
    "email": load_prompt("worth_policy_email"),
    "phone": load_prompt("worth_policy_phone"),
    "unknown": load_prompt("worth_policy_unknown"),
}
FACT_SCHEMA: dict[str, Any] = json.loads(
    Path(__file__).with_name("fact_schema.json").read_text(encoding="utf-8")
)


def owner_identity_block(owner: OwnerProfile) -> str:
    """Tell the model who I am, so it can detect a mailbox-owner alias."""
    name = owner.name
    emails = owner.emails
    if not name and not emails:
        return ""
    rendered = OWNER_IDENTITY_CHECK.format(
        name=name, emails=", ".join(emails) or "unknown email",
    )
    return f"\n\n{rendered}\n"


# This hash is a paid-cache contract: changing any prompt input here forces
# every parent through synthesis again at real cost. Runtime owner content is
# also covered by input_evidence_fingerprint's rendered system prompt.
SYNTHESIS_VERSION = hashlib.sha1(
    json.dumps(
        {
            "contract": SYNTHESIS_CONTRACT_VERSION,
            "system_prompt": SYSTEM_PROMPT,
            "schema": FACT_SCHEMA,
            "worth_policies": WORTH_POLICIES,
            "owner_prompt_suffix": OWNER_PROMPT_SUFFIX,
            "owner_identity_check": OWNER_IDENTITY_CHECK,
            "owner_identity_block": owner_identity_block(
                OwnerProfile(
                    name="OWNER_NAME",
                    emails=("owner@example.test",),
                )
            ),
        },
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:12]


def render_chunk(
    person: CollectionBundle,
    chunk: Sequence[MessageEntry],
) -> str:
    lines = [
        f"CONTACT: {person.full_name or '(unknown)'}",
        f"Known emails: {', '.join(person.emails) or '(none)'}",
        f"Known phones: {', '.join(person.phones) or '(none)'}",
        f"Channels: {', '.join(person.source_channels) or '(none)'}",
    ]
    if person.groups:
        lines.append(
            f"Shared group chats (names only): {', '.join(person.groups)}"
        )
    if person.thread_participants:
        lines.extend((
            "",
            "EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):",
        ))
        for thread in person.thread_participants[:25]:
            lines.append(
                f"- {thread.subject or '(no subject)'} — "
                f"{', '.join(thread.participants)}"
            )
    lines += ["", "MESSAGES (most relevant, chronological):"]
    for message in chunk:
        date = (message.at or "")[:10]
        who = "THEM" if message.direction == "from_them" else "ME"
        head = f"[{message.channel} {date} {who}]"
        if message.subject:
            head += f" {message.subject}"
        lines.append(f"{head}: {message.text or ''}")
    return "\n".join(lines)


def worth_channel_policy(person: CollectionBundle) -> str:
    channels = {
        str(channel or "").strip().lower()
        for channel in person.source_channels
        if str(channel or "").strip()
    }
    channels.update(
        (message.channel or "").strip().lower()
        for message in person.messages
        if (message.channel or "").strip()
    )
    email_present = bool(channels & {"gmail", "email"})
    phone_present = bool(channels & {"imessage", "whatsapp", "sms", "phone"})
    policy = (
        "mixed" if email_present and phone_present
        else "email" if email_present
        else "phone" if phone_present
        else "unknown"
    )
    return "\n\nWORTH SOURCE POLICY:\n" + WORTH_POLICIES[policy]


def render_batch(
    person: CollectionBundle,
    batch: Sequence[MessageEntry],
    prior: SynthesizedFacts | None,
) -> str:
    parts = []
    if prior:
        compact = {
            key: value
            for key, value in prior.to_payload().items()
            if value not in ("", [], None)
        }
        parts.append(
            "PROFILE SO FAR (refine and EXTEND from the older messages below; keep prior "
            "facts unless a message contradicts them; raise `confidence` only as the picture "
            "gets more complete and certain):\n" + json.dumps(compact, ensure_ascii=False)
        )
    parts.append(render_chunk(person, batch) + worth_channel_policy(person))
    return "\n\n".join(parts)


def input_evidence_fingerprint(
    person: CollectionBundle,
    *,
    system_prompt: str,
    chunk_chars: int,
    max_batches: int,
) -> str:
    """Hash the bounded source evidence and system context sent to synthesis.

    PINNED SERIALIZATION: this is the paid-cache key. The renderers and batching
    policy above define its contents, so unrendered bundle metadata cannot cause
    a paid rerun.
    """
    prompts = [
        render_batch(person, batch, None)
        for batch in batches(
            person.messages,
            chunk_chars=chunk_chars,
            max_batches=max_batches,
        )
    ]
    payload = json.dumps(
        {"system_prompt": system_prompt, "source_prompts": prompts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def batches(
    messages: Sequence[MessageEntry], *, chunk_chars: int, max_batches: int,
) -> list[list[MessageEntry]]:
    if max_batches <= 0:
        return []
    newest = sorted(messages, key=lambda message: message.at or "", reverse=True)
    chunks: list[list[MessageEntry]] = []
    current: list[MessageEntry] = []
    used = 0
    for message in newest:
        size = len(message.text or "")
        if current and used + size > chunk_chars:
            chunks.append(current)
            if len(chunks) == max_batches:
                return chunks
            current, used = [], 0
        current.append(message)
        used += size
    if current:
        chunks.append(current)
    return chunks
