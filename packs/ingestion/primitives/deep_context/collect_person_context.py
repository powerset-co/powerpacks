"""[1/4] Collect per-person message context from Gmail and chat DMs.

For each person in the merged people.csv who has any message channel, stream a
recent, adaptively-sampled window of their actual message BODIES into one
ephemeral JSON bundle per person. Only people with >= 1 message produce a bundle;
zero-interaction contacts are skipped.

Reads message bodies - that deep inspection is the whole point. iMessage and
WhatsApp read DMs by default. The explicit ``--include-groups`` option also reads
small iMessage group-chat bodies; WhatsApp groups remain excluded. Bundles live under
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
import csv
import json
import sys
import time
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

from packs.ingestion.primitives.deep_context import sources
from packs.ingestion.primitives.deep_context.candidates import (
    candidate_key_of,
    is_candidate_id,
    load_candidates,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    RAW_DIR,
    Person,
    emit,
    load_people,
    now_iso,
    write_json,
)

# Each channel is its own vertical with this deep cap: Gmail, iMessage, and WhatsApp
# each pool up to DEFAULT_DEEP_CAP recent messages independently, then they're
# concatenated (so no channel crowds out another). The incremental synthesizer groks
# the blended pool newest-first and stops on saturation/max-batches, so spend is bounded
# regardless of pool size. A char cap guards memory (raised to fit ~3 full verticals).
DEFAULT_DEEP_CAP = 1600
SAFETY_CHAR_CAP = 1_800_000


def _load_bundle(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bundle_matches_policy(bundle: dict[str, Any], args: argparse.Namespace) -> bool:
    policy = bundle.get("collection_policy")
    if not isinstance(policy, dict):
        return False
    return (
        policy.get("deep_cap") == args.deep_cap
        and policy.get("include_groups") is bool(args.include_groups)
        and (
            not args.include_groups
            or policy.get("max_group_size") == args.max_group_size
        )
    )


def _validate_people_csv(path: Path) -> None:
    with path.open(newline="", encoding="utf-8") as fh:
        fields = set(csv.DictReader(fh).fieldnames or [])
    missing = {"id", "source_channels"} - fields
    if missing:
        raise ValueError(f"people CSV missing required columns: {', '.join(sorted(missing))}")


def _purge_group_scoped_or_untrusted_bundles(out_dir: Path, *, partial: bool) -> int:
    """Discard unsafe raw bundles without deserializing their message bodies."""
    bundle_paths = [path for path in sorted(out_dir.glob("*.json")) if path.name != "manifest.json"]
    if not bundle_paths:
        return 0
    manifest = _load_bundle(out_dir / "manifest.json")
    privacy = manifest.get("privacy")
    safe_to_reuse = (
        manifest.get("privacy_schema_version") == 2
        and isinstance(privacy, dict)
        and privacy.get("group_bodies_present") is False
    )
    if safe_to_reuse:
        return 0
    if partial:
        raise ValueError(
            "existing raw bundles have group-enabled or legacy privacy scope; "
            "run a full default collection without --person/--limit to rebuild them safely"
        )
    for path in bundle_paths:
        path.unlink()
    return len(bundle_paths)


def _retained_group_policy(out_dir: Path) -> tuple[int, int]:
    """Return retained group-message count and the largest known group-size cap."""
    message_count = 0
    max_group_size = 0
    for path in sorted(out_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        bundle = _load_bundle(path)
        messages = bundle.get("messages")
        if not isinstance(messages, list):
            continue
        groups = [
            message for message in messages
            if isinstance(message, dict) and message.get("channel") == "imessage_group"
        ]
        if not groups:
            continue
        message_count += len(groups)
        policy = bundle.get("collection_policy")
        if isinstance(policy, dict) and isinstance(policy.get("max_group_size"), int):
            max_group_size = max(max_group_size, policy["max_group_size"])
    return message_count, max_group_size


def selected_people(args: argparse.Namespace, people_csv: Path) -> Iterator[Person]:
    """People to collect. Default: exactly the merged people.csv selection. With
    ``--include-candidates`` the unresolved research candidates chain after the
    people (one shared ``--limit`` across the union; ``--person candidate:<key>``
    selects a single candidate)."""
    if not getattr(args, "include_candidates", False):
        yield from load_people(people_csv, limit=args.limit, person_id=args.person)
        return

    def union() -> Iterator[Person]:
        yield from load_people(people_csv, person_id=args.person)
        if args.person and not is_candidate_id(args.person):
            return  # a people.csv id can never name a candidate
        yield from load_candidates(candidate_key=candidate_key_of(args.person))

    yield from islice(union(), args.limit or None)


def collect_one(
    person: Person,
    *,
    store: "sources.gni.MsgvaultStore | None",
    accounts: set[str],
    chat_db: Path,
    wacli_db: Path,
    deep_cap: int,
    include_groups: bool = False,
    max_group_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Gather a deep, recency-first pool of one person's messages across sources.

    Returns ``(pool, available)`` where ``available`` is the TRUE total in the
    sources (e.g. all 102k iMessage DMs, or every poolable email), so
    ``capped = available > len(pool)`` is honest. Each channel is its own vertical,
    already capped at ``deep_cap`` by its reader; they're concatenated by priority
    (identity-dense email, then DM bodies newest-first, then — only with
    ``include_groups`` — group-chat bodies), bounded only by the char safety cap."""
    gmail: list[dict[str, Any]] = []
    gmail_total = 0
    if store is not None and person.emails:
        gmail = sources.read_gmail(person, store, accounts, cap=deep_cap)
        gmail_total = sources.count_gmail(person, store, accounts)
    dm_chat: list[dict[str, Any]] = []
    group_chat: list[dict[str, Any]] = []
    true_chat_total = 0
    if person.phones:
        whatsapp = sources.read_whatsapp(person, wacli_db, cap=deep_cap)
        dm_chat.extend(sources.read_imessage(person, chat_db, cap=deep_cap))
        dm_chat.extend(whatsapp)
        # Reuse the WhatsApp pull for the honest total instead of re-querying it.
        # (len(whatsapp) is post-cap; a count_whatsapp_dms() is a clean follow-up.)
        true_chat_total = sources.count_imessage_dms(person, chat_db) + len(whatsapp)
        if include_groups:
            group_chat = sources.read_imessage_group_messages(
                person, chat_db, max_group_size=max_group_size, cap=deep_cap)

    # No shared message cap: each vertical already pooled up to deep_cap, so an
    # email-rich contact and a text-rich contact each keep their full vertical. The
    # char cap is the only cross-channel bound (RAM guard). Priority order keeps the
    # identity-dense email first, then DMs newest-first, then group bodies.
    ordered = list(gmail) \
        + sorted(dm_chat, key=lambda m: m.get("at") or "", reverse=True) \
        + sorted(group_chat, key=lambda m: m.get("at") or "", reverse=True)
    pool: list[dict[str, Any]] = []
    used = 0
    for msg in ordered:
        text = msg.get("text") or ""
        if not text:
            continue
        if used + len(text) > SAFETY_CHAR_CAP and pool:
            break
        pool.append(msg)
        used += len(text)
    pool.sort(key=lambda m: m.get("at") or "")
    available = gmail_total + true_chat_total + len(group_chat)
    return pool, available


def build(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    out_dir = Path(args.out_dir)
    chat_db = Path(args.chat_db).expanduser()
    wacli_db = Path(args.wacli_db)
    people_csv = Path(args.people_csv)
    _validate_people_csv(people_csv)

    store: "sources.gni.MsgvaultStore | None" = None
    accounts: set[str] = set()
    msgvault_db = Path(args.msgvault_db).expanduser()
    if msgvault_db.exists():
        store = sources.gni.MsgvaultStore(msgvault_db)
        try:
            store.connect()
            store.require_schema()
            accounts = store.account_emails()
        except Exception:
            store.close()
            store = None

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

    bundles_purged_for_scope = 0
    if not args.dry_run and not args.include_groups:
        bundles_purged_for_scope = _purge_group_scoped_or_untrusted_bundles(
            out_dir,
            partial=bool(args.person or args.limit),
        )

    people_total = 0
    with_context = 0
    capped = 0
    skipped_existing = 0
    channel_counts = {"gmail": 0, "imessage": 0, "whatsapp": 0}
    total_messages = 0
    try:
        for person in selected_people(args, people_csv):
            people_total += 1
            bundle_path = out_dir / f"{person.person_id}.json"
            if bundle_path.exists() and not args.force and not args.dry_run:
                existing = _load_bundle(bundle_path)
                if _bundle_matches_policy(existing, args):
                    skipped_existing += 1
                    with_context += 1
                    continue
            messages, available = collect_one(
                person,
                store=store,
                accounts=accounts,
                chat_db=chat_db,
                wacli_db=wacli_db,
                deep_cap=args.deep_cap,
                include_groups=args.include_groups,
                max_group_size=args.max_group_size,
            )
            groups = sources.read_imessage_groups(person, chat_db) if person.phones else []
            thread_participants = (sources.gmail_thread_participants(person, store)
                                   if store is not None and person.emails else [])
            if not messages and not groups:
                if not args.dry_run:
                    bundle_path.unlink(missing_ok=True)
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
                "collection_policy": {
                    "deep_cap": args.deep_cap,
                    "include_groups": bool(args.include_groups),
                    "max_group_size": args.max_group_size if args.include_groups else 0,
                },
                "collected_at": now_iso(),
            })
            if with_context % 25 == 0:
                print(f"[collect] {with_context} bundles written", file=sys.stderr, flush=True)
    finally:
        if store is not None:
            store.close()

    elapsed_s = max(time.monotonic() - started, 1e-6)
    retained_group_messages, retained_max_group_size = _retained_group_policy(out_dir)
    group_access_requested = bool(args.include_groups)
    group_bodies_present = retained_group_messages > 0
    manifest = {
        "source": "collect_person_context",
        "status": "completed",
        "privacy_schema_version": 2,
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
        "bundles_purged_for_scope": bundles_purged_for_scope,
        "msgvault_available": store is not None or msgvault_db.exists(),
        "chat_db_available": chat_db.exists(),
        "chat_db_probe": chat_probe,
        "wacli_available": wacli_db.exists(),
        "out_dir": str(out_dir),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
        "privacy": {
            "message_bodies_read": True,
            "dms_only": not (group_access_requested or group_bodies_present),
            "group_body_access_requested": group_access_requested,
            "group_bodies_present": group_bodies_present,
            "group_body_messages_present": retained_group_messages,
            "groups_read": group_access_requested or group_bodies_present,
            "group_source": "imessage" if group_access_requested or group_bodies_present else "",
            "max_group_size": (
                args.max_group_size if group_access_requested else retained_max_group_size
            ),
            "network_called": False,
            "local_only": True,
        },
    }
    if not args.dry_run:
        write_json(out_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collect per-person message bodies (Gmail + chat DMs; optional small iMessage groups).")
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--out-dir", default=str(RAW_DIR))
    p.add_argument("--msgvault-db", default=str(sources.gni.DEFAULT_MSGVAULT_DB))
    p.add_argument("--chat-db", default=str(Path.home() / "Library" / "Messages" / "chat.db"))
    p.add_argument("--wacli-db", default=str(sources.DEFAULT_WACLI_DB))
    p.add_argument("--deep-cap", type=int, default=DEFAULT_DEEP_CAP, help="Max messages pooled per person (raise = costs more at synthesis)")
    p.add_argument("--include-groups", action="store_true", help="Opt-in: also read iMessage GROUP bodies from small shared groups (costs more)")
    p.add_argument("--max-group-size", type=int, default=25, help="Skip groups larger than this many participants")
    p.add_argument("--include-candidates", action="store_true",
                   help="Also collect the unresolved import candidates (import/*/candidates.csv)")
    p.add_argument("--limit", type=int, default=0, help="Limit people (0 = all)")
    p.add_argument("--person", default="", help="Only this person id (candidate:<key> selects a candidate)")
    p.add_argument("--force", action="store_true", help="Rebuild bundles even if present")
    p.add_argument("--dry-run", action="store_true", help="Count messages, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit(build(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
