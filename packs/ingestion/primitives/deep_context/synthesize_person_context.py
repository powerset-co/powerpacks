"""[2/4] Synthesize structured facts from each person's message bundle (map step).

Reads the raw bundles from ``collect_person_context`` and, for each person, fans
out parallel OpenAI **Responses** calls (medium/high reasoning) that extract a
structured profile from the message bodies. Heavy reasoning runs on OpenAI; the
local box only streams JSON, so memory/CPU stay tiny on weak machines.

Per person: messages are chunked by a character budget; each chunk is one call.
Most people are a single chunk (one cheap call). For chatty people we process
chunks sequentially with an **adaptive early-stop** — once N consecutive chunks
add no new facts we stop spending. Persons are processed concurrently through a
bounded pool (``drain_pool``), checkpointed per person so a crash/interrupt
resumes cleanly.

After a completed full collection, facts without a remaining raw bundle are
removed before selection so obsolete identities cannot be re-billed. Scoped and
dry runs never prune the paid cache.

Outputs (fixed dir):
  <out-dir>/<person_id>.jsonl   one line per chunk: {chunk_index, facts, usage}
  <out-dir>/manifest.json       counts + token/cost totals

Changelog:
  2026-07-31 (professional content v5): CONTENT POLICY block in the prompt —
    dossiers are read in a professional context, so every field focuses on
    professional substance (work, deals, studies, skills, neutral relationship
    labels); personal life appears only as neutral congratulatory-grade
    milestones (wedding, child, graduation, home, relocation) and everything
    else personal stays out of every field, with quoting limited to
    professional content. Deliberately allowlist-phrased — the prompt names
    what belongs, not the unsavory categories it displaces. Such messages
    still count toward `network_worth` as relationship evidence. Intentional
    semantic change: SYNTHESIS_CONTRACT_VERSION bumps and every dossier
    resynthesizes on the next run.
  2026-07-31 (network-value worth v4): a personal connection with purely social
    threads still qualifies as yes when the person themselves is a confidently
    identified valuable network connection (founder/executive/investor/
    researcher/industry figure); suspected-but-unconfirmed notability is maybe,
    and fame without a real relationship stays no. Folds into the v3 evidence
    bar before any store migrated, so one contract bump covers both.
  2026-07-31 (professional worth v3): `network_worth` is an evidence bar, not a
    warmth bar — yes requires ANY work-related signal in the messages (job,
    employer, studies, projects, hiring, investing, collaboration); a real but
    purely personal relationship with zero work-related prose is no, and a
    transact-only service provider stays no even with a visible occupation.
    Intentional semantic change: SYNTHESIS_CONTRACT_VERSION bumps and every
    dossier resynthesizes on the next run. Human network_worth stays sticky.
  2026-07-30 (contact-info identifiers v2): `identifiers` is redefined as contact
    info to reach the person — their own emails and phone numbers only; URLs,
    handles, third-party and mailbox-owner endpoints are explicitly excluded
    (a contact's own URLs belong in `owned_identifiers`). This is an intentional
    semantic change, so SYNTHESIS_CONTRACT_VERSION bumps and every dossier
    resynthesizes on the next run. The deterministic `contact_identifiers`
    policy stays in force at every render/consumer regardless.
  2026-07-30 (house style): `_plan()` returns the frozen `SynthesisPlan` instead of
    a 3-tuple, and the run's numbers accumulate in `SynthesisTally` instead of a
    `counter`/`stop_reasons`/`usage_total` trio of string-keyed dicts. `execute()`
    has ONE exit: the "nothing pending" case is the `if plan.paths:` block being
    skipped, not a second 25-field copy of the manifest — same values on both
    paths (people=0, concurrency=0, zero tokens, $0), and `load_env()` plus the
    OpenAI client still happen only when there is work. Sections read select ->
    call -> mirror -> report. No behavior change.
  2026-07-27 (declared contract): `SynthesizePersonContext` is a
    `pipeline/contract.py:Node` named `deep_synthesize`. It declares the
    `{person_id}` raw-bundle template + owner.json as inputs and the `{person_id}`
    facts template + the machine-worth column slice of overrides/review.csv
    (row model `ReviewRow`, `owns_columns` = the `llm_worth*` pair plus the
    `llm_reject*` legacy-spam fields it clears) as outputs; the final manifest
    write moved into the Node template (same keys, plus the declared
    `fingerprints` block). `run(args)` became `execute()`. `--dry-run` cost
    estimation BYPASSES the node (`estimate()`, called plainly from main())
    because a dry run writes no manifest today — a free estimate must never
    overwrite a completed facts manifest. The spend path is unchanged: same
    flags, same payload, same gates, no OpenAI call added or moved.
  2026-07-27: prune orphan facts only after an authoritative full collection.
  2026-07-23 (audit dedup): now_iso, write_json import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tiktoken
from pydantic import Field

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context.common import (
    emit,
    ensure_no_review_session,
    FACTS_DIR,
    FACTS_MANIFEST,
    FACTS_TEMPLATE,
    LINKEDIN_OVERRIDES_CSV,
    load_env,
    load_owner,
    owner_background_block,
    OWNER_JSON,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.candidates import llm_network_worth
from packs.ingestion.primitives.deep_context.review_store import (
    ReviewRow,
    has_human_worth,
    load_override_rows,
    mirror_facts_worth,
    parent_ids_by_person,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

DEFAULT_CHUNK_CHARS = 9000
DEFAULT_TARGET_CONFIDENCE = 0.85   # stop deepening once the profile is this confident
DEFAULT_SATURATION_ROUNDS = 2      # ...or after this many batches add nothing new
DEFAULT_MAX_BATCHES = 20           # ...or this many batches (~1600 msgs) — hard ceiling
DEFAULT_MAX_RETRIES = 6
DEFAULT_CHUNK_PEOPLE = 200         # people loaded into memory at once (streaming bound)
SYNTHESIS_CONTRACT_VERSION = "professional-content-v5"
# Calibrated from real runs: ~10 chunks/s wall at high concurrency (ranged 6.7
# on flex tier to 11.7 on default tier). Used only for the --dry-run ETA; actual
# rate scales with --concurrency and your OpenAI usage tier.
CHUNKS_PER_SEC = 10.0

SYSTEM_PROMPT = (
    "You build a rich profile of ONE person (the CONTACT) from messages between me "
    "(the mailbox owner) and them. Messages are tagged direction=from_them (the contact's "
    "own words) or from_me (my words addressed to them). Extract durable facts ABOUT THE "
    "CONTACT — never attribute my identity to them.\n\n"
    "Pull employer(s) with current/past status, title, school, field of study, location, "
    "how I know them / our relationship, recurring topics we discuss, notable events with "
    "rough dates, and their contact identifiers. Prefer specific, evidence-backed facts "
    "over guesses; set low confidence when the signal is thin. Leave a field empty rather "
    "than inventing it.\n\n"
    "CONTENT POLICY — the profile is read in a professional context (think: an investor "
    "reviewing their network). Focus every field you write — summary, relationship, "
    "topics, events, shared context, and any quoted evidence — on professional "
    "substance: work, companies, roles, projects, deals, investments, studies, skills, "
    "public achievements, and how I know them (a neutral relationship label like friend, "
    "partner, family member, or college roommate is fine). From their personal life, "
    "include only notable congratulatory-grade milestones, stated neutrally: an "
    "engagement or wedding, a child born, a graduation, a new home, a big relocation. "
    "Everything else about their personal life stays out of the profile, in every field. "
    "Quote a message only for its professional content; if a message carries no "
    "professional substance and no milestone, extract nothing from it. Such messages "
    "still count as relationship evidence for `network_worth` (a real friendship is "
    "real), but their content stays out of the profile.\n\n"
    "`identifiers` is CONTACT INFO TO REACH THIS PERSON: email addresses and phone numbers "
    "ONLY, and only ones clearly the CONTACT's own — the address/number they send from, "
    "one in their signature, or one they explicitly state is theirs. NEVER include: any "
    "URL or link (websites, social posts, maps, calendar/scheduling links, tracking or "
    "campaign links), usernames/handles, dates, physical addresses; the mailbox owner's "
    "(MY) email or phone; or anyone else's contact info — a third party's contact card, a "
    "referral, a quoted address, or another participant on a group thread is NEVER this "
    "person's identifier. When unsure whose it is, leave it out.\n"
    "Separately fill `owned_identifiers` with identifiers clearly owned by the CONTACT — "
    "their own emails/phones AND their own URLs (personal site, portfolio, a social "
    "profile they present as theirs). The same strictness applies: quoted, referred, "
    "third-party, or merely-mentioned items are NEVER owned by the CONTACT. Do not treat "
    "the supplied Known phones as message evidence; those are already source-record "
    "identifiers.\n\n"
    "Also decide `network_worth`: is this contact worth keeping in my PROFESSIONAL "
    "network? Use only the message dossier and the contact identifiers supplied with it. "
    "Never use or infer a LinkedIn profile. The bar is EVIDENCE OF PROFESSIONAL CAPACITY "
    "in the messages, not relationship warmth.\n"
    "- yes: a real two-way relationship AND the messages carry ANY work-related signal "
    "about them — their job, employer, title, industry, projects, startup, "
    "studies/field, hiring, investing, professional asks or introductions, or "
    "working/collaborating together. One credible signal is enough; colleagues, "
    "collaborators, founders, investors, operators, researchers, professors, and "
    "classmates with visible school/career context all belong here. A personal contact "
    "(partner, family member, friend) with purely social threads ALSO qualifies when "
    "the person themselves is a valuable network connection — a founder, executive, "
    "investor, researcher, or recognized industry figure — identified with reasonable "
    "confidence from the messages or their identity (name, email domain). Knowing a "
    "valuable person personally IS network value.\n"
    "- no: clearly automated/broadcast mail, newsletters, receipts/notifications, mass "
    "marketing, cold sales/recruiting/SEO/agency outreach I did not meaningfully engage "
    "with, spam, or a purely transactional support/vendor/service exchange (a service "
    "provider I only book and pay stays no even though their occupation is visible). "
    "ALSO no: any real personal relationship whose messages contain ZERO work-related "
    "prose AND no identifiable professional standing. Warmth without professional "
    "evidence or standing is no — and fame without a real two-way relationship is "
    "still no (a broadcast from a notable person is noise).\n"
    "- maybe: use only when the evidence is genuinely balanced or incomplete — a thin "
    "work-related hint you cannot confirm, suspected-but-unconfirmed notability (a "
    "name you cannot confidently identify is maybe, never a guessed yes), or messages "
    "too sparse to tell noise from a real tie. Maybe is exceptional, not a catch-all. "
    "Never use maybe merely because their seniority or professional value is unknown. "
    "A plausible phone area code may be a weak positive prior when the message "
    "evidence is sparse, but never invent an identity or biographical fact.\n"
    "Give a terse concrete reason citing the work-related evidence, the identified "
    "professional standing, or the absence of both."
)

# Appended when an owner.json bio is present: lets the model infer era/school/
# employer overlaps between the owner and the contact from message content.
OWNER_PROMPT_SUFFIX = (
    "\n\nUse MY background below as context to infer SHARED CONTEXT with the contact: if the "
    "messages suggest we overlapped at the same school, employer, place, or time period "
    "(e.g. they discuss coursework/projects during my school years, or a team/workplace during "
    "my tenure somewhere), record it in `shared_context` with the specific overlap and the "
    "message evidence. Only infer an overlap when the message content supports it — do NOT "
    "assume overlap just because dates align. Leave `shared_context` empty if nothing supports it.\n\n"
)

# Strict JSON schema (every object: all props required + additionalProperties:false).
FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "canonical_name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "employers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "status": {"type": "string", "enum": ["current", "past", "unknown"]},
                },
                "required": ["name", "role", "status"],
            },
        },
        "title": {"type": "string"},
        "school": {"type": "string"},
        "field_of_study": {"type": "string"},
        "location": {"type": "string"},
        "relationship_to_owner": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "notable_events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["date", "summary"],
            },
        },
        "identifiers": {"type": "array", "items": {"type": "string"},
                        "description": "Contact info to reach THIS person: their own email addresses and phone numbers only. Never URLs/links, handles, dates, the mailbox owner's endpoints, or anyone else's contact info."},
        "owned_identifiers": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "emails": {"type": "array", "items": {"type": "string"}},
                "phones": {"type": "array", "items": {"type": "string"}},
                "urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["emails", "phones", "urls"],
        },
        "shared_context": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "overlap": {"type": "string", "enum": ["school", "employer", "location", "era", "other"]},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["overlap", "detail", "evidence"],
            },
        },
        "confidence": {"type": "number"},
        "is_owner": {"type": "boolean", "description": "True if this 'contact' is actually the mailbox owner on another email address."},
        "network_worth": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {"type": "string", "enum": ["yes", "maybe", "no"]},
                "reason": {"type": "string"},
            },
            "required": ["decision", "reason"],
        },
    },
    "required": [
        "canonical_name", "aliases", "employers", "title", "school", "field_of_study",
        "location", "relationship_to_owner", "topics", "notable_events", "identifiers", "owned_identifiers",
        "shared_context", "confidence", "is_owner", "network_worth",
    ],
}

# Facts are a cache. Bump the explicit contract for an intentional semantic change;
# the prompt/schema fingerprint catches accidental drift alongside it.
SYNTHESIS_VERSION = hashlib.sha1(json.dumps({
    "contract": SYNTHESIS_CONTRACT_VERSION,
    "prompt": SYSTEM_PROMPT,
    "schema": FACT_SCHEMA,
}, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def owner_identity_block(owner: dict[str, Any]) -> str:
    """Tell the model who I am, so it can flag a 'contact' that is really ME on another address."""
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
    """Group messages into chunks under a character budget (>=1 chunk if any)."""
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    used = 0
    for msg in messages:
        size = len(msg.get("text") or "")
        if cur and used + size > chunk_chars:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(msg)
        used += size
    if cur:
        chunks.append(cur)
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
        # Group-chat NAMES are a relationship signal (e.g. "Family", "College Crew").
        lines.append(f"Shared group chats (names only): {', '.join(groups)}")
    threads = person.get("thread_participants") or []
    if threads:
        lines.append("")
        lines.append("EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):")
        for t in threads[:25]:
            lines.append(f"- {t.get('subject') or '(no subject)'} — {', '.join(t.get('participants') or [])}")
    lines += ["", "MESSAGES (most relevant, chronological):"]
    for msg in chunk:
        date = (msg.get("at") or "")[:10]
        who = "THEM" if msg.get("direction") == "from_them" else "ME"
        chan = msg.get("channel", "")
        subject = msg.get("subject") or ""
        head = f"[{chan} {date} {who}]"
        if subject:
            head += f" {subject}"
        lines.append(f"{head}: {msg.get('text') or ''}")
    return "\n".join(lines)


def worth_channel_policy(person: dict[str, Any]) -> str:
    """Return the source-specific rubric used by the one synthesis worth judge."""
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


def fact_keys(facts: dict[str, Any]) -> set[str]:
    """Comparable keys for adaptive early-stop (did this chunk add anything new?)."""
    keys: set[str] = set()
    for emp in facts.get("employers") or []:
        if emp.get("name"):
            keys.add(f"emp:{emp['name'].lower()}")
    for field in ("title", "school", "location", "field_of_study"):
        if facts.get(field):
            keys.add(f"{field}:{str(facts[field]).lower()}")
    for topic in facts.get("topics") or []:
        keys.add(f"topic:{str(topic).lower()}")
    for ident in facts.get("identifiers") or []:
        keys.add(f"id:{str(ident).lower()}")
    for kind in ("emails", "phones", "urls"):
        for ident in (facts.get("owned_identifiers") or {}).get(kind) or []:
            keys.add(f"owned:{kind}:{str(ident).lower()}")
    return keys


def render_batch(person: dict[str, Any], batch: list[dict[str, Any]], prior: dict[str, Any] | None) -> str:
    """Render one deepening batch, prefixed with the running profile to refine."""
    parts = []
    if prior:
        compact = {k: v for k, v in prior.items() if v not in ("", [], None)}
        parts.append(
            "PROFILE SO FAR (refine and EXTEND from the older messages below; keep prior "
            "facts unless a message contradicts them; raise `confidence` only as the picture "
            "gets more complete and certain):\n" + json.dumps(compact, ensure_ascii=False)
        )
    parts.append(render_chunk(person, batch) + worth_channel_policy(person))
    return "\n\n".join(parts)


async def synthesize_person(
    client: Any,
    person: dict[str, Any],
    batches: list[list[dict[str, Any]]],
    *,
    model: str,
    effort: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    system_prompt: str,
    target_confidence: float,
    saturation_rounds: int,
    max_batches: int,
) -> dict[str, Any]:
    """Incrementally grok a person: refine ONE running profile batch-by-batch
    (newest first), stopping when confident, saturated, or out of messages."""
    profile: dict[str, Any] = {}
    seen: set[str] = set()
    stale = 0
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    batches_used = 0
    messages_used = 0
    errors = 0
    stop_reason = "exhausted"
    for idx, batch in enumerate(batches):
        if idx >= max_batches:
            stop_reason = "max_batches"
            break
        prompt = render_batch(person, batch, profile or None)
        facts, usage, error = await _call_one(
            client, prompt, model=model, effort=effort, semaphore=semaphore,
            max_retries=max_retries, system_prompt=system_prompt,
        )
        for key in usage_total:
            usage_total[key] += usage.get(key, 0)
        batches_used += 1
        messages_used += len(batch)
        if error:
            errors += 1
        if facts:
            profile = facts
        new_keys = fact_keys(facts) - seen
        seen |= fact_keys(facts)
        stale = stale + 1 if not new_keys else 0
        conf = float(facts.get("confidence") or 0.0)
        if conf >= target_confidence:
            stop_reason = "confident"
            break
        if stale >= saturation_rounds:
            stop_reason = "saturated"
            break
    return {
        "person_id": person.get("person_id"),
        "facts": profile,
        "usage": usage_total,
        "batches_used": batches_used,
        "batches_total": len(batches),
        "messages_used": messages_used,
        "messages_available": person.get("messages_available", len(person.get("messages") or [])),
        "final_confidence": round(float(profile.get("confidence") or 0.0), 2),
        "stop_reason": stop_reason,
        "errors": errors,
    }


async def _call_one(
    client: Any,
    prompt: str,
    *,
    model: str,
    effort: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    system_prompt: str,
) -> tuple[dict[str, Any], dict[str, int], str]:
    kwargs = responses_kwargs(model, effort=effort, schema=FACT_SCHEMA, schema_name="person_facts")
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    **kwargs,
                )
                return parse_json_response(response, "synthesize"), usage_tokens(response), ""
            except Exception as exc:  # noqa: BLE001 - classify then retry/record
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {}, {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}, f"{type(exc).__name__}: {exc}"[:300]


def pending_target_paths(
    raw_dir: Path,
    facts_dir: Path,
    *,
    force: bool,
    person_id: str,
    rejudge: bool = False,
    review_rows: dict[str, dict[str, str]] | None = None,
) -> list[Path]:
    """Bundle paths needing synthesis — WITHOUT loading message bodies into memory.

    Streaming relies on this: we hold only the path list (cheap), then load bundle
    bodies one chunk at a time. The 'has messages' check is deferred to load time.

    Normal runs are monotonic: keep terminal machine Yes/No and human Yes/No,
    while retrying missing/Maybe verdicts. A facts record from an earlier
    synthesis contract is stale even with a terminal worth decision, so a
    contract bump automatically rebuilds it. ``rejudge`` deliberately ignores
    both caches; the review writer still preserves the human-owned column."""
    paths: list[Path] = []
    rows = review_rows or {}
    parent_ids = parent_ids_by_person(facts_dir.parent / "index.json")
    for path in sorted(raw_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        pid = path.stem
        if person_id and pid != person_id:
            continue
        if not force and not rejudge:
            facts_path = facts_dir / f"{pid}.jsonl"
            if _facts_version(facts_path) != SYNTHESIS_VERSION:
                paths.append(path)
                continue
            if has_human_worth(rows, pid, parent_ids):
                continue
            existing = llm_network_worth(pid, facts_dir).get("decision", "")
            if existing in {"yes", "no"}:
                continue
        paths.append(path)
    return paths


def _facts_version(path: Path) -> str:
    """Version stamped on the latest fixed-path facts artifact, if readable."""
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return ""
    return str(records[-1].get("synthesis_version") or "") if records else ""


def _load_bundle(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def prune_orphan_facts(raw_dir: Path, facts_dir: Path, *, scoped: bool, dry_run: bool) -> int:
    """Drop facts whose source bundle left a completed full collection.

    Facts are a paid cache, so an absent or incomplete collection manifest is
    never authority to delete them. Scoped and dry runs are non-authoritative.
    """
    if scoped or dry_run:
        return 0
    try:
        manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if manifest.get("status") != "completed":
        return 0
    current_ids = {
        path.stem for path in raw_dir.glob("*.json") if path.name != "manifest.json"
    }
    removed = 0
    for facts_path in facts_dir.glob("*.jsonl"):
        if facts_path.stem in current_ids:
            continue
        facts_path.unlink()
        removed += 1
    return removed


def _chunked(seq: list[Any], size: int) -> Any:
    for i in range(0, len(seq), max(1, size)):
        yield seq[i:i + size]


@dataclass(frozen=True)
class SynthesisPlan:
    """Everything the free preamble decides, before a single token is spent:
    who the owner is, the exact system prompt that will be sent, and which
    bundles still need synthesis. Both the paid run and the `--dry-run` estimate
    start from this same value, so they can never disagree about the population.
    """

    owner: dict[str, Any] | None
    system_prompt: str
    paths: list[Path]


@dataclass
class SynthesisTally:
    """What the run accumulated, one field per manifest number.

    Replaces a `counter` dict, a `stop_reasons` dict and a `usage_total` dict
    threaded through the result callback by string key; `record` is the whole
    update, applied once per completed person.
    """

    people_done: int = 0
    errors: int = 0
    batches: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0})

    def record(self, result: dict[str, Any]) -> None:
        for key in self.tokens:
            self.tokens[key] += result["usage"].get(key, 0)
        self.people_done += 1
        self.errors += result["errors"]
        self.batches += result["batches_used"]
        reason = result["stop_reason"]
        self.stop_reasons[reason] = self.stop_reasons.get(reason, 0) + 1


class SynthesizePersonContextManifest(StageManifest):
    """The stage's typed manifest payload — same keys as the raw dict it replaces.
    `updated_at` is stamped in `execute()` so the emitted payload keeps it, exactly
    as the raw dict did (the manifest writer preserves a payload-set value)."""
    source: str = "synthesize_person_context"
    people: int = 0
    chunk_people: int = 0
    people_done: int = 0
    batches_run: int = 0
    avg_batches_per_person: float = 0.0
    stop_reasons: dict[str, int] = Field(default_factory=dict)
    errors: int = 0
    model: str = ""
    synthesis_version: str = SYNTHESIS_VERSION
    reasoning_effort: str = ""
    owner_context: bool = False
    orphan_facts_removed: int = 0
    rejudge: bool = False
    target_confidence: float = DEFAULT_TARGET_CONFIDENCE
    max_batches: int = DEFAULT_MAX_BATCHES
    concurrency: int = 0
    tokens: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    out_dir: str = ""
    worth_sync: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0
    updated_at: str = ""


class SynthesizePersonContext(Node):
    """Builds per-person facts from raw bundles through OpenAI Responses calls
    (SPENDS). Construct with explicit paths/config and call `run()` — except
    `--dry-run`, where the caller invokes `estimate()` (a free counting pass)
    directly so the estimate never writes the facts manifest."""

    name = "deep_synthesize"
    # Both inputs tolerate absence: no bundles is the pre-collect state (the run
    # completes with people=0) and owner.json (produced in-graph by the owner
    # node) is an optional reasoning anchor, also skippable via --no-owner.
    inputs = (
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
    )
    # review.csv has three writers with disjoint column slices (see
    # review_store.OVERRIDE_COLUMNS): synthesis owns the mirrored machine worth
    # plus the legacy llm_reject spam values it retires; reconciliation owns the
    # action/link columns; the human alone owns network_worth.
    outputs = (
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(
            path=str(LINKEDIN_OVERRIDES_CSV),
            row_model=ReviewRow,
            writes="upsert",
            owns_columns=(
                "llm_worth",
                "llm_worth_reason",
                "llm_reject",
                "llm_reject_confidence",
                "llm_reject_reason",
            ),
            required=False,
        ),
    )
    payload = SynthesizePersonContextManifest
    manifest = str(FACTS_MANIFEST)

    def __init__(
        self,
        *,
        raw_dir: Path | None = None,
        out_dir: Path | None = None,
        review_csv: Path | None = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        target_confidence: float = DEFAULT_TARGET_CONFIDENCE,
        saturation_rounds: int = DEFAULT_SATURATION_ROUNDS,
        max_batches: int = DEFAULT_MAX_BATCHES,
        concurrency: int = 0,
        chunk_people: int = DEFAULT_CHUNK_PEOPLE,
        timeout: int = 120,
        max_retries: int = DEFAULT_MAX_RETRIES,
        person: str = "",
        no_owner: bool = False,
        force: bool = False,
        rejudge: bool = False,
    ) -> None:
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.facts_dir = Path(out_dir or FACTS_DIR)
        self.review_csv = Path(review_csv or LINKEDIN_OVERRIDES_CSV)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.chunk_chars = chunk_chars
        self.target_confidence = target_confidence
        self.saturation_rounds = saturation_rounds
        self.max_batches = max_batches
        self.concurrency = concurrency
        self.chunk_people = chunk_people
        self.timeout = timeout
        self.max_retries = max_retries
        self.person = person
        self.no_owner = no_owner
        self.force = force
        self.rejudge = rejudge

    def bindings(self) -> dict[str, str]:
        return {
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            str(LINKEDIN_OVERRIDES_CSV): str(self.review_csv),
            self.manifest: str(self.facts_dir / "manifest.json"),
        }

    def _plan(self) -> SynthesisPlan:
        """The shared free preamble of both the paid run and the dry-run estimate."""
        owner = load_owner() if not self.no_owner else None
        system_prompt = SYSTEM_PROMPT + (
            owner_identity_block(owner) + OWNER_PROMPT_SUFFIX + owner_background_block(owner) if owner else "")
        review_rows = load_override_rows(self.review_csv)
        # Only the path list is held in memory; bundle bodies are loaded one chunk at a
        # time, so peak RAM is bounded by --chunk-people, not the network size.
        paths = pending_target_paths(
            self.raw_dir,
            self.facts_dir,
            force=self.force,
            rejudge=self.rejudge,
            person_id=self.person,
            review_rows=review_rows,
        )
        return SynthesisPlan(owner=owner, system_prompt=system_prompt, paths=paths)

    def _batches(self, messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        newest = sorted(messages, key=lambda m: m.get("at") or "", reverse=True)
        return chunk_messages(newest, self.chunk_chars)[: self.max_batches]

    def estimate(self) -> dict[str, Any]:
        """The --dry-run cost estimate: count batches/tokens, spend and write
        NOTHING (beyond today's facts-dir mkdir). Deliberately NOT `execute()` —
        it bypasses the run template so the estimate never becomes a manifest."""
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        encoder = tiktoken.get_encoding("o200k_base")
        plan = self._plan()
        # Stream bundles one at a time to tally tokens without holding them all.
        profile_carry_tokens = 350
        floor_tokens = ceiling_tokens = ceiling_batches = people = 0
        for path in plan.paths:
            bundle = _load_bundle(path)
            if not bundle.get("messages"):
                continue
            people += 1
            batches = self._batches(bundle["messages"])
            if batches:
                floor_tokens += len(encoder.encode(plan.system_prompt + render_batch(bundle, batches[0], None)))
            for i, b in enumerate(batches):
                ceiling_tokens += len(encoder.encode(plan.system_prompt + render_batch(bundle, b, None)))
                ceiling_tokens += profile_carry_tokens if i > 0 else 0
                ceiling_batches += 1
        return {
            "source": "synthesize_person_context",
            "status": "dry_run",
            "people": people,
            "batches_ceiling": ceiling_batches,
            "model": self.model,
            "synthesis_version": SYNTHESIS_VERSION,
            "reasoning_effort": reasoning_effort(self.reasoning_effort),
            "owner_context": bool(plan.owner),
            # A dry run is never authority to delete the paid facts cache
            # (`prune_orphan_facts` returns 0 for it), so the estimate always
            # reports zero — the key stays for payload parity with a real run.
            "orphan_facts_removed": 0,
            "rejudge": bool(self.rejudge),
            "target_confidence": self.target_confidence,
            "max_batches": self.max_batches,
            "estimated_cost_floor_usd": estimate_cost_usd(floor_tokens, people * 750, self.model),
            "estimated_cost_ceiling_usd": estimate_cost_usd(ceiling_tokens, ceiling_batches * 750, self.model),
            "estimated_wall_seconds_ceiling": round(ceiling_batches / CHUNKS_PER_SEC, 1),
            "note": "approximate (output/reasoning tokens vary with --reasoning-effort); floor=1 batch each, ceiling=all batches. Confidence/saturation usually stops near the floor.",
            "updated_at": now_iso(),
        }

    def execute(self) -> SynthesizePersonContextManifest:
        started = time.monotonic()

        # ---- SELECT: what still needs synthesis, and nothing stale. ----------
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        # Drop facts whose bundle left a completed full collection BEFORE
        # selection, so an obsolete identity cannot be re-billed. `--dry-run`
        # never reaches execute() (it bypasses to estimate()), so dry_run=False.
        orphan_facts_removed = prune_orphan_facts(
            self.raw_dir, self.facts_dir, scoped=bool(self.person), dry_run=False)
        plan = self._plan()
        tally = SynthesisTally()
        # Effort and concurrency are read from the environment the SPEND path
        # loads. With nothing pending we never touch `.env`, so they keep their
        # pre-`.env` values (no pool was sized, so concurrency is 0) — exactly
        # what the old no-work branch reported.
        concurrency = 0
        effort = reasoning_effort(self.reasoning_effort)

        # ---- CALL: fan the pending bundles out through OpenAI. This block is
        # the stage's ONLY spend, and an empty plan skips it whole — no env load,
        # no client, no tokens.
        if plan.paths:
            load_env()
            effort = reasoning_effort(self.reasoning_effort)  # `.env` may set it
            concurrency = self.concurrency or env_or_profile_int(
                "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=16
            )
            total = len(plan.paths)

            def on_result(result: dict[str, Any]) -> None:
                """Checkpoint ONE finished person: its facts file, then the tally."""
                pid = result["person_id"]
                rec = {
                    "chunk_index": 0,
                    "synthesis_version": SYNTHESIS_VERSION,
                    "facts": result["facts"],
                    "usage": result["usage"],
                    "batches_used": result["batches_used"],
                    "batches_total": result["batches_total"],
                    "messages_used": result["messages_used"],
                    "messages_available": result["messages_available"],
                    "final_confidence": result["final_confidence"],
                    "stop_reason": result["stop_reason"],
                }
                (self.facts_dir / f"{pid}.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
                tally.record(result)
                if tally.people_done % 25 == 0:
                    print(f"[synthesize] {tally.people_done}/{total} people", file=sys.stderr, flush=True)

            async def driver() -> None:
                client = make_async_client(timeout=self.timeout)
                semaphore = asyncio.Semaphore(max(1, concurrency))
                try:
                    # Process people in bounded chunks: load bodies -> batch -> drain -> free.
                    # Only one chunk's bundles/batches are resident at a time.
                    for chunk_paths in _chunked(plan.paths, self.chunk_people):
                        bundles = [b for b in (_load_bundle(p) for p in chunk_paths) if b.get("messages")]
                        local_batches = {b["person_id"]: self._batches(b["messages"]) for b in bundles}
                        coros = [
                            synthesize_person(
                                client, bundle, local_batches[bundle["person_id"]],
                                model=self.model, effort=effort, semaphore=semaphore,
                                max_retries=self.max_retries, system_prompt=plan.system_prompt,
                                target_confidence=self.target_confidence,
                                saturation_rounds=self.saturation_rounds, max_batches=self.max_batches,
                            )
                            for bundle in bundles
                        ]
                        await drain_pool(coros, on_result)
                finally:
                    await client.close()

            asyncio.run(driver())

        # ---- MIRROR the machine worth onto review.csv, then report. ----------
        # Runs on every path, including the no-work one: facts written by an
        # earlier interrupted run still need their worth column mirrored.
        worth_sync = mirror_facts_worth(
            self.review_csv,
            self.facts_dir,
            include_human_rows=bool(self.rejudge),
        )
        billed_output = tally.tokens["output_tokens"] + tally.tokens["reasoning_tokens"]
        return SynthesizePersonContextManifest(
            status="completed",
            people=len(plan.paths),
            chunk_people=self.chunk_people,
            people_done=tally.people_done,
            batches_run=tally.batches,
            avg_batches_per_person=round(tally.batches / max(1, tally.people_done), 2),
            stop_reasons=tally.stop_reasons,
            errors=tally.errors,
            model=self.model,
            synthesis_version=SYNTHESIS_VERSION,
            reasoning_effort=effort,
            owner_context=bool(plan.owner),
            orphan_facts_removed=orphan_facts_removed,
            rejudge=bool(self.rejudge),
            target_confidence=self.target_confidence,
            max_batches=self.max_batches,
            concurrency=concurrency,
            tokens=tally.tokens,
            estimated_cost_usd=estimate_cost_usd(tally.tokens["input_tokens"], billed_output, self.model),
            out_dir=str(self.facts_dir),
            worth_sync=worth_sync,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            updated_at=now_iso(),
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Synthesize structured facts from message bundles (OpenAI Responses).")
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--out-dir", default=str(FACTS_DIR))
    p.add_argument("--review-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="Per-batch char budget")
    p.add_argument("--target-confidence", type=float, default=DEFAULT_TARGET_CONFIDENCE, help="Stop deepening once the profile reaches this confidence")
    p.add_argument("--saturation-rounds", type=int, default=DEFAULT_SATURATION_ROUNDS, help="Stop after N consecutive batches add no new facts")
    p.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES, help="Hard ceiling on deepening batches per person")
    p.add_argument("--concurrency", type=int, default=0, help="0 = from usage tier")
    p.add_argument("--chunk-people", type=int, default=DEFAULT_CHUNK_PEOPLE, help="People held in memory per streaming chunk")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    p.add_argument("--person", default="", help="Only this person id")
    p.add_argument("--no-owner", action="store_true", help="Ignore owner.json (skip shared-context inference)")
    p.add_argument("--force", action="store_true", help="Re-synthesize even if facts exist")
    p.add_argument(
        "--rejudge",
        action="store_true",
        help="Rejudge every message-backed dossier despite cached machine/human worth; preserve the human column",
    )
    p.add_argument("--dry-run", action="store_true", help="Estimate calls/cost, spend nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    ensure_no_review_session("synthesize_person_context")
    args = build_parser().parse_args(argv)
    node = SynthesizePersonContext(
        raw_dir=Path(args.raw_dir),
        out_dir=Path(args.out_dir),
        review_csv=Path(args.review_csv),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        chunk_chars=args.chunk_chars,
        target_confidence=args.target_confidence,
        saturation_rounds=args.saturation_rounds,
        max_batches=args.max_batches,
        concurrency=args.concurrency,
        chunk_people=args.chunk_people,
        timeout=args.timeout,
        max_retries=args.max_retries,
        person=args.person,
        no_owner=args.no_owner,
        force=args.force,
        rejudge=args.rejudge,
    )
    if args.dry_run:
        # The free estimate bypasses the run template: it writes no manifest
        # today, and must never overwrite a completed one with an estimate.
        emit(node.estimate())
        return 0
    payload = node.run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
