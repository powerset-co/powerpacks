"""Pinned schema, prompts, message batching, and prompt rendering for synthesis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt

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
    rendered = OWNER_IDENTITY_CHECK.format(
        name=name, emails=", ".join(emails) or "unknown email",
    )
    return f"\n\n{rendered}\n"


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
    policy = (
        "mixed" if email_present and phone_present
        else "email" if email_present
        else "phone" if phone_present
        else "unknown"
    )
    return "\n\nWORTH SOURCE POLICY:\n" + WORTH_POLICIES[policy]


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


def input_evidence_fingerprint(
    person: dict[str, Any], *, system_prompt: str, chunk_chars: int, max_batches: int,
) -> str:
    """Hash the bounded source evidence and system context sent to synthesis.

    PINNED SERIALIZATION: this is the paid-cache key. The renderers and batching
    policy above define its contents, so unrendered bundle metadata cannot cause
    a paid rerun.
    """
    prompts = [
        render_batch(person, batch, None)
        for batch in batches(
            person.get("messages") or [],
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
    messages: list[dict[str, Any]], *, chunk_chars: int, max_batches: int,
) -> list[list[dict[str, Any]]]:
    if max_batches <= 0:
        return []
    newest = sorted(messages, key=lambda message: message.get("at") or "", reverse=True)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for message in newest:
        size = len(message.get("text") or "")
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
