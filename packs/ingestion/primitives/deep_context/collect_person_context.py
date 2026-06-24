"""[1/4] Collect per-person message context from Gmail + iMessage DMs + WhatsApp DMs.

For each person in the merged people.csv who has any message channel, stream a
recent, adaptively-sampled window of their actual message BODIES into one
ephemeral JSON bundle per person. Only people with >= 1 message produce a bundle;
zero-interaction contacts are skipped.

Reads message bodies — that deep inspection is the whole point. iMessage/WhatsApp
read DMs only (group chats are never read). Bundles live under
``.powerpacks/deep-context/raw/`` (gitignored, ephemeral, purgeable); dossiers keep
synthesized facts, not verbatim text.

Memory: one person's recent window at a time (every source query is per-person
``LIMIT``-bounded), so RSS stays flat regardless of archive size.

Outputs (fixed dir, overwrite in place):
  <out-dir>/<person_id>.json   one bundle per person with >=1 message
  <out-dir>/manifest.json      counts/status/privacy
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context import sources
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    RAW_DIR,
    Person,
    emit,
    load_people,
    now_iso,
    write_json,
)

# Deep recency-first pool kept per person. Matches the incremental synth ceiling
# (~20 batches x 80 msgs). Email (thread-deduped, high-signal) is always kept;
# chat DMs fill the remaining budget newest-first. A safety char cap guards memory.
DEFAULT_DEEP_CAP = 1600
SAFETY_CHAR_CAP = 600_000


def collect_one(
    person: Person,
    *,
    msgvault_con: sqlite3.Connection | None,
    accounts: set[str],
    chat_db: Path,
    wacli_db: Path,
    deep_cap: int,
    include_groups: bool = False,
    max_group_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Gather a deep, recency-first pool of one person's messages across sources.

    Returns ``(pool, available)`` where ``available`` is the TRUE total in the
    sources (e.g. all 102k iMessage DMs), so ``capped = available > len(pool)``
    is honest. Fill priority: email (thread-deduped, identity-dense) is always
    kept, then DM bodies newest-first, then — only with ``include_groups`` —
    group-chat bodies from small shared groups fill any remaining budget."""
    gmail: list[dict[str, Any]] = []
    if msgvault_con is not None and person.emails:
        gmail = sources.read_gmail(person, msgvault_con, accounts)
    dm_chat: list[dict[str, Any]] = []
    group_chat: list[dict[str, Any]] = []
    true_chat_total = 0
    if person.phones:
        dm_chat.extend(sources.read_imessage(person, chat_db, cap=deep_cap))
        dm_chat.extend(sources.read_whatsapp(person, wacli_db, cap=deep_cap))
        true_chat_total = sources.count_imessage_dms(person, chat_db) + len(
            sources.read_whatsapp(person, wacli_db, cap=deep_cap)
        )
        if include_groups:
            group_chat = sources.read_imessage_group_messages(
                person, chat_db, max_group_size=max_group_size, cap=deep_cap)

    pool = list(gmail)
    used = sum(len(m.get("text") or "") for m in pool)
    # DMs fill before group bodies so the 1:1 signal is never crowded out.
    fill = sorted(dm_chat, key=lambda m: m.get("at") or "", reverse=True) + \
        sorted(group_chat, key=lambda m: m.get("at") or "", reverse=True)
    for msg in fill:
        if len(pool) >= deep_cap:
            break
        text = msg.get("text") or ""
        if not text:
            continue
        if used + len(text) > SAFETY_CHAR_CAP and pool:
            break
        pool.append(msg)
        used += len(text)
    pool.sort(key=lambda m: m.get("at") or "")
    available = len(gmail) + true_chat_total + len(group_chat)
    return pool, available


def build(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    out_dir = Path(args.out_dir)
    chat_db = Path(args.chat_db).expanduser()
    wacli_db = Path(args.wacli_db)
    people_csv = Path(args.people_csv)

    msgvault_con: sqlite3.Connection | None = None
    accounts: set[str] = set()
    msgvault_db = Path(args.msgvault_db).expanduser()
    if msgvault_db.exists():
        msgvault_con = sources.gni.connect_msgvault(msgvault_db)
        try:
            sources.gni.require_msgvault_schema(msgvault_con)
            msgvault_con.row_factory = sqlite3.Row
            accounts = sources.bec.account_emails(msgvault_con)
        except Exception:
            msgvault_con.close()
            msgvault_con = None

    # One-time chat.db readability probe so a Full Disk Access denial is loud,
    # not silently swallowed as "0 iMessage messages".
    chat_probe = sources.probe_chat_db(chat_db)
    if chat_probe["exists"] and not chat_probe["readable"]:
        print(
            f"[collect] WARNING: chat.db exists but is unreadable — iMessage will be EMPTY. "
            f"Likely Full Disk Access. error={chat_probe['error']}",
            file=sys.stderr, flush=True,
        )

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    people_total = 0
    with_context = 0
    capped = 0
    skipped_existing = 0
    channel_counts = {"gmail": 0, "imessage": 0, "whatsapp": 0}
    total_messages = 0
    try:
        for person in load_people(people_csv, limit=args.limit, person_id=args.person):
            people_total += 1
            bundle_path = out_dir / f"{person.person_id}.json"
            if bundle_path.exists() and not args.force and not args.dry_run:
                skipped_existing += 1
                with_context += 1
                continue
            messages, available = collect_one(
                person,
                msgvault_con=msgvault_con,
                accounts=accounts,
                chat_db=chat_db,
                wacli_db=wacli_db,
                deep_cap=args.deep_cap,
                include_groups=args.include_groups,
                max_group_size=args.max_group_size,
            )
            groups = sources.read_imessage_groups(person, chat_db) if person.phones else []
            thread_participants = (sources.gmail_thread_participants(person, msgvault_con)
                                   if msgvault_con is not None and person.emails else [])
            if not messages and not groups:
                continue
            with_context += 1
            total_messages += len(messages)
            if available > len(messages):
                capped += 1
            for msg in messages:
                channel_counts[msg["channel"]] = channel_counts.get(msg["channel"], 0) + 1
            if args.dry_run:
                continue
            write_json(bundle_path, {
                "person_id": person.person_id,
                "full_name": person.full_name,
                "emails": person.emails,
                "phones": person.phones,
                "source_channels": person.source_channels,
                "groups": groups,
                "thread_participants": thread_participants,
                "messages": messages,
                "messages_available": available,
                "capped": available > len(messages),
                "collected_at": now_iso(),
            })
            if with_context % 25 == 0:
                print(f"[collect] {with_context} bundles written", file=sys.stderr, flush=True)
    finally:
        if msgvault_con is not None:
            msgvault_con.close()

    elapsed_s = max(time.monotonic() - started, 1e-6)
    manifest = {
        "source": "collect_person_context",
        "status": "completed",
        "dry_run": bool(args.dry_run),
        "people_total": people_total,
        "people_with_context": with_context,
        "people_skipped_existing": skipped_existing,
        "total_messages_sampled": total_messages,
        "people_capped": capped,
        "channel_message_counts": channel_counts,
        "contacts_per_sec": round(people_total / elapsed_s, 1),
        "messages_per_sec": round(total_messages / elapsed_s, 1),
        "ms_per_contact": round(elapsed_s / people_total * 1000, 2) if people_total else 0,
        "deep_cap_per_person": args.deep_cap,
        "groups_included": bool(args.include_groups),
        "max_group_size": args.max_group_size,
        "msgvault_available": msgvault_con is not None or msgvault_db.exists(),
        "chat_db_available": chat_db.exists(),
        "chat_db_probe": chat_probe,
        "wacli_available": wacli_db.exists(),
        "out_dir": str(out_dir),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
        "privacy": {
            "message_bodies_read": True,
            "dms_only": True,
            "groups_read": False,
            "network_called": False,
            "local_only": True,
        },
    }
    if not args.dry_run:
        write_json(out_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collect per-person message bodies (Gmail + iMessage/WhatsApp DMs).")
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--out-dir", default=str(RAW_DIR))
    p.add_argument("--msgvault-db", default=str(sources.gni.DEFAULT_MSGVAULT_DB))
    p.add_argument("--chat-db", default=str(Path.home() / "Library" / "Messages" / "chat.db"))
    p.add_argument("--wacli-db", default=str(sources.DEFAULT_WACLI_DB))
    p.add_argument("--deep-cap", type=int, default=DEFAULT_DEEP_CAP, help="Max messages pooled per person (raise = costs more at synthesis)")
    p.add_argument("--include-groups", action="store_true", help="Opt-in: also read iMessage GROUP bodies from small shared groups (costs more)")
    p.add_argument("--max-group-size", type=int, default=25, help="Skip groups larger than this many participants")
    p.add_argument("--limit", type=int, default=0, help="Limit people (0 = all)")
    p.add_argument("--person", default="", help="Only this person id")
    p.add_argument("--force", action="store_true", help="Rebuild bundles even if present")
    p.add_argument("--dry-run", action="store_true", help="Count messages, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit(build(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
