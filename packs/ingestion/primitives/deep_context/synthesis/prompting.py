"""Pinned schema, prompts, message batching, and prompt rendering for synthesis.

Changelog:
- 2026-08-08: render_chunk now labels a group message from a third
  participant OTHER-IN-GROUP instead of collapsing it onto THEM. Only bundles
  that actually carry a FROM_OTHER message change their rendered bytes, so
  input_evidence_fingerprint (and the paid-synthesis cache) only moves for
  those parents.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    MessageDirection,
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
WORTH_POLICY = load_prompt("worth_policy")
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
# every parent through synthesis again at real cost. owner_identity_block below
# is rendered against a FIXED FAKE owner ("OWNER_NAME" / owner@example.test),
# never the real OwnerProfile, so SYNTHESIS_VERSION stays identical across
# owners and across owner-profile edits — only its template text can move it.
# The real owner text still enters the paid-cache key, just downstream: see
# input_evidence_fingerprint's system_prompt argument.
SYNTHESIS_VERSION = hashlib.sha1(
    json.dumps(
        {
            "contract": SYNTHESIS_CONTRACT_VERSION,
            "system_prompt": SYSTEM_PROMPT,
            "schema": FACT_SCHEMA,
            "worth_policy": WORTH_POLICY,
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
).hexdigest()[:12]  # Truncated: a cache-busting version tag, not a security digest.


#  Policy is a visible decision (see AGENTS.md): one literal table, first-rule
#  match on MessageDirection's three possible values. FROM_OTHER only ever
#  reaches a group-channel message (see MessageDirection.of_group), but the
#  label set covers every MessageDirection member uniformly.
_DIRECTION_LABEL: dict[MessageDirection, str] = {
    MessageDirection.FROM_ME: "ME",
    MessageDirection.FROM_THEM: "THEM",
    # Distinct from THEM on purpose: a third participant in a shared group is
    # not the contact this dossier is about, and must not read as if it were.
    MessageDirection.FROM_OTHER: "OTHER-IN-GROUP",
}


def render_chunk(
    person: CollectionBundle,
    chunk: Sequence[MessageEntry],
) -> str:
    """Render one batch of messages as the plain-text block the model reads.

    Renders as:
        CONTACT: Jordan Bravo
        Known emails: jordan@example.com
        Known phones: (none)
        Channels: gmail, imessage

        EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):
        - Intro to the team — jordan@example.com, casey@example.com

        MESSAGES (most relevant, chronological):
        [gmail 2026-01-05 THEM] Re: intro: Great meeting you at the conference!
        [imessage 2026-01-06 ME]: Likewise, let's grab coffee sometime.
        [imessage_group 2026-01-07 OTHER-IN-GROUP] Founders: Someone else in the
        shared group chat said this — not Jordan.
    """
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
        for thread in person.thread_participants[:25]:  # silent cap: bounds prompt size, not a data limit
            lines.append(
                f"- {thread.subject or '(no subject)'} — "
                f"{', '.join(thread.participants)}"
            )
    lines += ["", "MESSAGES (most relevant, chronological):"]
    for message in chunk:
        # message.at can be "" for rows msgvault stored without a timestamp; kept
        # deliberately (see MessageEntry.from_payload) — renders as an empty date
        # rather than dropping the message.
        date = (message.at or "")[:10]
        who = _DIRECTION_LABEL[message.direction]
        head = f"[{message.channel} {date} {who}]"
        if message.subject:
            head += f" {message.subject}"
        lines.append(f"{head}: {message.text or ''}")
    return "\n".join(lines)


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
    parts.append(
        render_chunk(person, batch) + "\n\nWORTH SOURCE POLICY:\n" + WORTH_POLICY
    )
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

    Every batch is rendered with prior=None, even though real synthesis threads
    the accumulated prior-batch facts into render_batch on batch 2+. So this
    fingerprints the evidence and system context, not the exact prompt bytes a
    later batch actually sends — which is what keeps the key stable across a
    multi-batch run instead of chasing the model's own prior output.
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
    """Pack messages newest-first into <=chunk_chars batches, capped at max_batches.

    Messages sort newest-first, so hitting max_batches returns early and drops
    whatever is left — i.e. the OLDEST messages, not the tail of the list. A cap
    here truncates history, it does not trim the most recent chunk.

    Sort is stable and keys on `at` alone, so messages tied on `at` keep the
    order the bundle carried in; that tie order is itself a deliberate choice
    documented in collection/models.py:_dedupe_by_payload, and it is here that
    the tie-break becomes actual prompt bytes.
    """
    if max_batches <= 0:
        return []
    newest = sorted(messages, key=lambda message: message.at or "", reverse=True)
    chunks: list[list[MessageEntry]] = []
    current: list[MessageEntry] = []
    used = 0
    for message in newest:
        size = len(message.text or "")
        # `current and` guards a single oversized message from being dropped:
        # it still gets its own one-message batch instead of never starting one.
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
