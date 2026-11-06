"""Block 02 Collect: one bounded bundle of message bodies per candidate, read locally. Free.

For every non-owner candidate, read its messages from the Gmail archive (msgvault.db), iMessage
(chat.db) and WhatsApp (wacli.db) by its normalized emails and phones, cap them, and upsert one
`bundles` row holding the bundle as JSON plus a fingerprint of those bytes. A candidate with no
messages gets no row. A rerun recollects every candidate; the store is the record.

Created: 2026-10-06
"""
from __future__ import annotations

import argparse
import hashlib
import json
import atexit
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from collections.abc import Iterator

from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB
from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageEntry, Person
from packs.ingestion.primitives.deep_context_v2.collect.readers import CHAT_MESSAGE_CAP, WACLI_DB_RELATIVE, ContextSources
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.owner import read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB

MAX_GROUP_SIZE = 25      # iMessage groups larger than this are not the candidate's context
# People are collected in parallel, each worker process with its own read-only stores. The reads wait
# on disk (a 279k-message archive: 1,000 people took 18.9 s on one reader, 3.9 s on 16) and the Python
# after them holds the GIL (16 threads: 391 s against 272 s for one), so processes, not threads.
WORKERS = 8
DEFAULT_LIMIT = 100_000  # more candidates than any store has; --limit N narrows a run to the first N


class Collect(Node):
    name = "collect"
    reads = ("owner", "candidates", "candidate_identifiers", "candidate_sources")
    writes = ("bundles",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, limit: int, chat_db: Path, *,
                 msgvault_db: Path = DEFAULT_MSGVAULT_DB) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        # msgvault and chat.db live under the home directory; only the WhatsApp store is under the data root.
        self.chat_db = chat_db
        self.msgvault_db = msgvault_db
        self.wacli_db = data_root / WACLI_DB_RELATIVE

    # No required_files: a store is opened only for candidates whose channel came from it, and
    # a candidate carries a channel only because the importer read that store. It exists.

    def execute(self) -> dict[str, int]:
        people: list[Person] = queries.candidates_to_collect(self.conn, self.limit)
        # The store says which channels were linked; only those stores are opened.
        channels: set[str] = queries.channels_present(self.conn)
        owner_name: str = read_owner(self.conn).name
        contact_names: dict[str, str] = {row["normalized_value"]: row["display_name"] for row in self.conn.execute(
            "SELECT i.normalized_value, c.display_name FROM candidate_identifiers i "
            "JOIN candidates c ON c.candidate_id = i.candidate_id ORDER BY c.candidate_id"
        )}

        worker_args: WorkerArgs = (channels, self.msgvault_db, self.chat_db, self.wacli_db, owner_name, contact_names)

        counts: dict[str, int] = {
            "candidates": len(people),
            "bundles": 0,
            "no_messages": 0,
            "capped": 0,
            "messages_gmail": 0,
            "messages_imessage": 0,
            "messages_imessage_group": 0,
            "messages_whatsapp": 0,
            "messages_whatsapp_group": 0,
        }
        self.progress("people", 0, len(people))
        # Workers only read the message stores; this thread alone writes bundles, in candidate order,
        # so the store ends up exactly as a one-by-one run leaves it.
        for index, (person, collected) in enumerate(collect_all(people, worker_args), 1):
            self.progress("people", index, len(people))
            if collected is None:
                counts["no_messages"] += 1
                continue
            bundle, payload_json = collected
            # The fingerprint is over exactly the stored bytes.
            fingerprint: str = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            queries.upsert_bundle(self.conn, person.person_id, payload_json, fingerprint, now_iso())
            counts["bundles"] += 1
            counts["capped"] += bundle.capped
            for message in bundle.messages:
                counts["messages_" + message.channel] += 1
        return counts


def collect_one(sources: ContextSources, person: Person) -> tuple[CollectionBundle, str] | None:
    """One person's bundle and its stored JSON (sorted keys, compact), or None when they have no messages."""
    # Direct messages across all of the person's channels, newest-first, capped at the depth.
    # `available` is how many there were before the cap.
    messages: list[MessageEntry]
    available: int
    messages, available = sources.collect_person(person)
    if not messages:
        return None
    bundle: CollectionBundle = CollectionBundle.of(
        person,
        messages=messages,
        groups=list(dict.fromkeys(sources.imessage_groups(person) + [
            message.subject for message in messages if message.channel == "whatsapp_group"
        ])),
        thread_participants=sources.thread_participants(person),  # who else was on the email threads
        available=available,
    )
    return bundle, json.dumps(bundle.to_payload(), sort_keys=True, separators=(",", ":"))


WorkerArgs = tuple[set[str], Path, Path, Path, str, dict[str, str]]  # open_worker's arguments
_SOURCES: ContextSources | None = None  # this worker process's stores


def open_worker(channels: set[str], msgvault_db: Path, chat_db: Path, wacli_db: Path, owner_name: str,
                contact_names: dict[str, str]) -> None:
    global _SOURCES
    _SOURCES = ContextSources(channels=channels, msgvault_db=msgvault_db, chat_db=chat_db, wacli_db=wacli_db,
                              deep_cap=CHAT_MESSAGE_CAP, max_group_size=MAX_GROUP_SIZE, owner_name=owner_name,
                              contact_names=contact_names)
    atexit.register(_SOURCES.close)


def collect_in_worker(person: Person) -> tuple[CollectionBundle, str] | None:
    assert _SOURCES is not None  # open_worker ran when this process started
    return collect_one(_SOURCES, person)


def collect_all(people: list[Person], worker_args: WorkerArgs
                ) -> Iterator[tuple[Person, tuple[CollectionBundle, str] | None]]:
    """(person, collect_one result) for every person, in order, collected by WORKERS processes."""
    with ProcessPoolExecutor(max_workers=WORKERS, initializer=open_worker, initargs=worker_args) as pool:
        yield from zip(people, pool.map(collect_in_worker, people, chunksize=16))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect one bounded message bundle per candidate into the v2 store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="first N candidates, for a quick check")
    parser.add_argument("--chat-db", type=Path, default=DEFAULT_CHAT_DB, help="an iMessage store other than this Mac's")
    args = parser.parse_args(argv)
    manifest = Collect(open_store(store_path(args.data_root)), args.data_root, args.limit, args.chat_db).run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
