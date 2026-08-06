"""Pinned schema, prompts, message batching, and prompt rendering for synthesis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt

SYNTHESIS_CONTRACT_VERSION = "relationship-category-v6"
SYSTEM_PROMPT = load_prompt("person_synthesis_system")
OWNER_PROMPT_SUFFIX = f"\n\n{load_prompt('owner_context_suffix')}\n\n"
FACT_SCHEMA: dict[str, Any] = json.loads(
    Path(__file__).with_name("fact_schema.json").read_text(encoding="utf-8")
)
SYNTHESIS_VERSION = hashlib.sha1(json.dumps({
    "contract": SYNTHESIS_CONTRACT_VERSION,
    "prompt": SYSTEM_PROMPT,
    "schema": FACT_SCHEMA,
}, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def owner_identity_block(owner: dict[str, Any]) -> str:
    """Tell the model who I am, so it can detect a mailbox-owner alias."""
    name = owner.get("name") or ""
    emails = owner.get("emails") or []
    if not name and not emails:
        return ""
    return (
        f"\n\nMAILBOX OWNER (ME): {name} <{', '.join(emails) or 'unknown email'}>. You are profiling ONE "
        "OTHER person, not me. OWNER-ALIAS CHECK: if the CONTACT shares MY name AND one of my email "
        "addresses above appears among the thread participants (or anywhere in the messages), then this "
        "'contact' is almost certainly ME using a different email — set is_owner=true and "
        "relationship_to_owner='This is the mailbox owner (me) on another email address.' Do NOT flag a "
        "mere namesake whose threads do NOT include one of my own addresses. Default is_owner=false.\n"
    )


def chunk_messages(messages: list[dict[str, Any]], chunk_chars: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for message in messages:
        size = len(message.get("text") or "")
        if current and used + size > chunk_chars:
            chunks.append(current)
            current, used = [], 0
        current.append(message)
        used += size
    if current:
        chunks.append(current)
    return chunks


def render_chunk(person: dict[str, Any], chunk: list[dict[str, Any]]) -> str:
    lines = [
        f"CONTACT: {person.get('full_name') or '(unknown)'}",
        f"Known emails: {', '.join(person.get('emails') or []) or '(none)'}",
        f"Known phones: {', '.join(person.get('phones') or []) or '(none)'}",
        f"Channels: {', '.join(person.get('source_channels') or []) or '(none)'}",
    ]
    groups = person.get("groups") or []
    if groups:
        lines.append(f"Shared group chats (names only): {', '.join(groups)}")
    threads = person.get("thread_participants") or []
    if threads:
        lines.extend((
            "",
            "EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):",
        ))
        for thread in threads[:25]:
            lines.append(
                f"- {thread.get('subject') or '(no subject)'} — "
                f"{', '.join(thread.get('participants') or [])}"
            )
    lines += ["", "MESSAGES (most relevant, chronological):"]
    for message in chunk:
        date = (message.get("at") or "")[:10]
        who = "THEM" if message.get("direction") == "from_them" else "ME"
        head = f"[{message.get('channel', '')} {date} {who}]"
        if message.get("subject"):
            head += f" {message['subject']}"
        lines.append(f"{head}: {message.get('text') or ''}")
    return "\n".join(lines)


def worth_channel_policy(person: dict[str, Any]) -> str:
    channels = {
        str(channel or "").strip().lower()
        for channel in person.get("source_channels") or []
        if str(channel or "").strip()
    }
    channels.update(
        str(message.get("channel") or "").strip().lower()
        for message in person.get("messages") or []
        if str(message.get("channel") or "").strip()
    )
    email_present = bool(channels & {"gmail", "email"})
    phone_present = bool(channels & {"imessage", "whatsapp", "sms", "phone"})
    if email_present and phone_present:
        rule = (
            "This dossier has both email and phone-message context. Bias toward yes when "
            "either channel shows a genuine human relationship; automated noise in one "
            "channel must not erase real correspondence in the other. Use maybe only when "
            "both channels remain genuinely ambiguous."
        )
    elif email_present:
        rule = (
            "This is an email-backed dossier. Bias toward yes for clearly human, "
            "person-directed correspondence, including sparse, one-off, old, academic, "
            "or plausibly important professional contacts. Use no only for clear automated "
            "mail, broadcast/transactional noise, or unengaged cold spam. Maybe should be rare."
        )
    elif phone_present:
        rule = (
            "This is a phone-message-backed dossier. Repeated or clearly two-way personal "
            "or professional conversation is yes. Sparse context, a bare number, or an "
            "uncertain one-sided exchange may be maybe; automated service traffic or obvious "
            "spam is no. A name or area code is weak context only."
        )
    else:
        rule = (
            "The source is unclear. Judge only the supplied message context and identifiers; "
            "prefer maybe over inventing a relationship when the evidence is truly sparse."
        )
    return "\n\nWORTH SOURCE POLICY:\n" + rule


def render_batch(
    person: dict[str, Any], batch: list[dict[str, Any]], prior: dict[str, Any] | None,
) -> str:
    parts = []
    if prior:
        compact = {key: value for key, value in prior.items() if value not in ("", [], None)}
        parts.append(
            "PROFILE SO FAR (refine and EXTEND from the older messages below; keep prior "
            "facts unless a message contradicts them; raise `confidence` only as the picture "
            "gets more complete and certain):\n" + json.dumps(compact, ensure_ascii=False)
        )
    parts.append(render_chunk(person, batch) + worth_channel_policy(person))
    return "\n\n".join(parts)


def batches(
    messages: list[dict[str, Any]], *, chunk_chars: int, max_batches: int,
) -> list[list[dict[str, Any]]]:
    newest = sorted(messages, key=lambda message: message.get("at") or "", reverse=True)
    return chunk_messages(newest, chunk_chars)[:max_batches]
