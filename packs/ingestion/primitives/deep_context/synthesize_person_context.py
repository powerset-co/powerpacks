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

Outputs (fixed dir):
  <out-dir>/<person_id>.jsonl   one line per chunk: {chunk_index, facts, usage}
  <out-dir>/manifest.json       counts + token/cost totals

Changelog:
  2026-07-23 (audit dedup): now_iso, write_json import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import tiktoken

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
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    RAW_DIR,
    emit,
    load_env,
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context.candidates import llm_network_worth
from packs.ingestion.primitives.deep_context.review_store import (
    has_human_worth,
    load_override_rows,
    mirror_facts_worth,
)

DEFAULT_CHUNK_CHARS = 9000
DEFAULT_TARGET_CONFIDENCE = 0.85   # stop deepening once the profile is this confident
DEFAULT_SATURATION_ROUNDS = 2      # ...or after this many batches add nothing new
DEFAULT_MAX_BATCHES = 20           # ...or this many batches (~1600 msgs) — hard ceiling
DEFAULT_MAX_RETRIES = 6
DEFAULT_CHUNK_PEOPLE = 200         # people loaded into memory at once (streaming bound)
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
    "rough dates, and any identifiers (emails, phones, social handles, URLs). Prefer "
    "specific, evidence-backed facts over guesses; set low confidence when the signal is "
    "thin. Leave a field empty rather than inventing it.\n\n"
    "Also decide `network_worth`: is this contact worth adding to (or keeping in) my "
    "network? Use only the message dossier and the contact identifiers supplied with it. "
    "Never use or infer a LinkedIn profile. This is primarily a human-vs-noise filter, "
    "not a prestige contest.\n"
    "- yes: a real person I genuinely know or correspond with — family/relatives, friends, classmates, "
    "professors/teachers/mentors, alumni or school contacts, colleagues, collaborators, "
    "warm introductions, founders, investors, operators, researchers, or any meaningful "
    "two-way personal/professional relationship. Old, personal, one-off school, or sparse "
    "professional context is still yes when the human relationship itself is real.\n"
    "- no: clearly automated/broadcast mail, newsletters, receipts/notifications, mass "
    "marketing, cold sales/recruiting/SEO/agency outreach I did not meaningfully engage "
    "with, spam, or a purely transactional support/vendor/service exchange with no durable "
    "human relationship.\n"
    "- maybe: use only when the evidence is genuinely balanced or incomplete about whether "
    "there is a real relationship versus noise. Maybe is exceptional, not a catch-all. "
    "Never use maybe merely because their job, seniority, or professional value is unknown, "
    "or because the relationship is personal, old, academic, or social. Choose yes or no "
    "whenever the messages support either conclusion. A recognizable or notable name, or "
    "a plausible phone area code, may be a weak positive prior when the message evidence "
    "is sparse, but never invent an identity or biographical fact from either.\n"
    "Give a terse concrete reason."
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
        "identifiers": {"type": "array", "items": {"type": "string"}},
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
        "location", "relationship_to_owner", "topics", "notable_events", "identifiers",
        "shared_context", "confidence", "is_owner", "network_worth",
    ],
}


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
    while retrying missing/Maybe verdicts. ``rejudge`` deliberately ignores both
    caches and evaluates every dossier; the review writer still preserves the
    human-owned column."""
    paths: list[Path] = []
    rows = review_rows or {}
    for path in sorted(raw_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        pid = path.stem
        if person_id and pid != person_id:
            continue
        if not force and not rejudge:
            if has_human_worth(rows, pid):
                continue
            existing = llm_network_worth(pid, facts_dir).get("decision", "")
            if existing in {"yes", "no"}:
                continue
        paths.append(path)
    return paths


def _load_bundle(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _chunked(seq: list[Any], size: int) -> Any:
    for i in range(0, len(seq), max(1, size)):
        yield seq[i:i + size]


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    raw_dir = Path(args.raw_dir)
    facts_dir = Path(args.out_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    encoder = tiktoken.get_encoding("o200k_base")

    owner = load_owner() if not args.no_owner else None
    system_prompt = SYSTEM_PROMPT + (
        owner_identity_block(owner) + OWNER_PROMPT_SUFFIX + owner_background_block(owner) if owner else "")
    review_path = Path(args.review_csv)
    review_rows = load_override_rows(review_path)

    # Only the path list is held in memory; bundle bodies are loaded one chunk at a
    # time, so peak RAM is bounded by --chunk-people, not the network size.
    paths = pending_target_paths(
        raw_dir,
        facts_dir,
        force=args.force,
        rejudge=args.rejudge,
        person_id=args.person,
        review_rows=review_rows,
    )

    def make_batches(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        newest = sorted(messages, key=lambda m: m.get("at") or "", reverse=True)
        return chunk_messages(newest, args.chunk_chars)[: args.max_batches]

    if args.dry_run:
        # Stream bundles one at a time to tally tokens without holding them all.
        profile_carry_tokens = 350
        floor_tokens = ceiling_tokens = ceiling_batches = people = 0
        for path in paths:
            bundle = _load_bundle(path)
            if not bundle.get("messages"):
                continue
            people += 1
            batches = make_batches(bundle["messages"])
            if batches:
                floor_tokens += len(encoder.encode(system_prompt + render_batch(bundle, batches[0], None)))
            for i, b in enumerate(batches):
                ceiling_tokens += len(encoder.encode(system_prompt + render_batch(bundle, b, None)))
                ceiling_tokens += profile_carry_tokens if i > 0 else 0
                ceiling_batches += 1
        return {
            "source": "synthesize_person_context",
            "status": "dry_run",
            "people": people,
            "batches_ceiling": ceiling_batches,
            "model": args.model,
            "reasoning_effort": reasoning_effort(args.reasoning_effort),
            "owner_context": bool(owner),
            "rejudge": bool(args.rejudge),
            "target_confidence": args.target_confidence,
            "max_batches": args.max_batches,
            "estimated_cost_floor_usd": estimate_cost_usd(floor_tokens, people * 750, args.model),
            "estimated_cost_ceiling_usd": estimate_cost_usd(ceiling_tokens, ceiling_batches * 750, args.model),
            "estimated_wall_seconds_ceiling": round(ceiling_batches / CHUNKS_PER_SEC, 1),
            "note": "approximate (output/reasoning tokens vary with --reasoning-effort); floor=1 batch each, ceiling=all batches. Confidence/saturation usually stops near the floor.",
            "updated_at": now_iso(),
        }

    if not paths:
        worth_sync = mirror_facts_worth(
            review_path,
            facts_dir,
            include_human_rows=bool(args.rejudge),
        )
        manifest = {
            "source": "synthesize_person_context",
            "status": "completed",
            "people": 0,
            "chunk_people": args.chunk_people,
            "people_done": 0,
            "batches_run": 0,
            "avg_batches_per_person": 0.0,
            "stop_reasons": {},
            "errors": 0,
            "model": args.model,
            "reasoning_effort": reasoning_effort(args.reasoning_effort),
            "owner_context": bool(owner),
            "rejudge": bool(args.rejudge),
            "target_confidence": args.target_confidence,
            "max_batches": args.max_batches,
            "concurrency": 0,
            "tokens": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
            "estimated_cost_usd": 0.0,
            "out_dir": str(facts_dir),
            "worth_sync": worth_sync,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }
        write_json(facts_dir / "manifest.json", manifest)
        return manifest

    load_env()
    concurrency = args.concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=16
    )
    effort = reasoning_effort(args.reasoning_effort)
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    counter = {"done": 0, "errors": 0, "batches": 0}
    stop_reasons: dict[str, int] = {}
    total = len(paths)

    def on_result(result: dict[str, Any]) -> None:
        pid = result["person_id"]
        rec = {
            "chunk_index": 0,
            "facts": result["facts"],
            "usage": result["usage"],
            "batches_used": result["batches_used"],
            "batches_total": result["batches_total"],
            "messages_used": result["messages_used"],
            "messages_available": result["messages_available"],
            "final_confidence": result["final_confidence"],
            "stop_reason": result["stop_reason"],
        }
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        for key in usage_total:
            usage_total[key] += result["usage"].get(key, 0)
        counter["done"] += 1
        counter["errors"] += result["errors"]
        counter["batches"] += result["batches_used"]
        stop_reasons[result["stop_reason"]] = stop_reasons.get(result["stop_reason"], 0) + 1
        if counter["done"] % 25 == 0:
            print(f"[synthesize] {counter['done']}/{total} people", file=sys.stderr, flush=True)

    async def driver() -> None:
        client = make_async_client(timeout=args.timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        try:
            # Process people in bounded chunks: load bodies -> batch -> drain -> free.
            # Only one chunk's bundles/batches are resident at a time.
            for chunk_paths in _chunked(paths, args.chunk_people):
                bundles = [b for b in (_load_bundle(p) for p in chunk_paths) if b.get("messages")]
                local_batches = {b["person_id"]: make_batches(b["messages"]) for b in bundles}
                coros = [
                    synthesize_person(
                        client, bundle, local_batches[bundle["person_id"]],
                        model=args.model, effort=effort, semaphore=semaphore,
                        max_retries=args.max_retries, system_prompt=system_prompt,
                        target_confidence=args.target_confidence,
                        saturation_rounds=args.saturation_rounds, max_batches=args.max_batches,
                    )
                    for bundle in bundles
                ]
                await drain_pool(coros, on_result)
        finally:
            await client.close()

    asyncio.run(driver())

    worth_sync = mirror_facts_worth(
        review_path,
        facts_dir,
        include_human_rows=bool(args.rejudge),
    )
    billed_output = usage_total["output_tokens"] + usage_total["reasoning_tokens"]
    manifest = {
        "source": "synthesize_person_context",
        "status": "completed",
        "people": total,
        "chunk_people": args.chunk_people,
        "people_done": counter["done"],
        "batches_run": counter["batches"],
        "avg_batches_per_person": round(counter["batches"] / max(1, counter["done"]), 2),
        "stop_reasons": stop_reasons,
        "errors": counter["errors"],
        "model": args.model,
        "reasoning_effort": effort,
        "owner_context": bool(owner),
        "rejudge": bool(args.rejudge),
        "target_confidence": args.target_confidence,
        "max_batches": args.max_batches,
        "concurrency": concurrency,
        "tokens": usage_total,
        "estimated_cost_usd": estimate_cost_usd(usage_total["input_tokens"], billed_output, args.model),
        "out_dir": str(facts_dir),
        "worth_sync": worth_sync,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
    write_json(facts_dir / "manifest.json", manifest)
    return manifest


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
    args = build_parser().parse_args(argv)
    emit(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
