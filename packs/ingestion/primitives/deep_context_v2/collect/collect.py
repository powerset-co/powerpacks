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
import sqlite3
from pathlib import Path

from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB
from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageEntry, Person
from packs.ingestion.primitives.deep_context_v2.collect.readers import CHAT_MESSAGE_CAP, WACLI_DB_RELATIVE, ContextSources
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB

MAX_GROUP_SIZE = 25      # iMessage groups larger than this are not the candidate's context
DEFAULT_LIMIT = 100_000  # more candidates than any store has; --limit N narrows a run to the first N


class Collect(Node):
    name = "collect"
    reads = ("candidates", "candidate_identifiers", "candidate_sources")
    writes = ("bundles",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, limit: int) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        # msgvault and chat.db live under the home directory; only the WhatsApp store is under the data root.
        self.wacli_db = data_root / WACLI_DB_RELATIVE

    # No required_files: a store is opened only for candidates whose channel came from it, and
    # a candidate carries a channel only because the importer read that store. It exists.

    def execute(self) -> dict[str, int]:
        people: list[Person] = queries.candidates_to_collect(self.conn, self.limit)
        sources: ContextSources = ContextSources(
            msgvault_db=DEFAULT_MSGVAULT_DB,
            chat_db=DEFAULT_CHAT_DB,
            wacli_db=self.wacli_db,
            deep_cap=CHAT_MESSAGE_CAP,
            max_group_size=MAX_GROUP_SIZE,
        )
        counts: dict[str, int] = {
            "candidates": len(people),
            "bundles": 0,
            "no_messages": 0,
            "capped": 0,
            "messages_gmail": 0,
            "messages_imessage": 0,
            "messages_imessage_group": 0,
            "messages_whatsapp": 0,
        }
        for person in people:
            # Direct messages across all of the person's channels, newest-first, capped at the depth.
            # `available` is how many there were before the cap.
            messages: list[MessageEntry]
            available: int
            messages, available = sources.collect_person(person)
            if not messages:
                counts["no_messages"] += 1
                continue
            bundle: CollectionBundle = CollectionBundle.of(
                person,
                messages=messages,
                groups=sources.imessage_groups(person),                  # names of small iMessage groups shared
                thread_participants=sources.thread_participants(person),  # who else was on the email threads
                available=available,
            )
            # Stored as sorted-key compact JSON; the fingerprint is over exactly those bytes.
            payload_json: str = json.dumps(bundle.to_payload(), sort_keys=True, separators=(",", ":"))
            fingerprint: str = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            queries.upsert_bundle(self.conn, person.person_id, payload_json, fingerprint, now_iso())
            counts["bundles"] += 1
            counts["capped"] += bundle.capped
            for message in messages:
                counts["messages_" + message.channel] += 1
        sources.close()
        return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect one bounded message bundle per candidate into the v2 store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="first N candidates, for a quick check")
    args = parser.parse_args(argv)
    manifest = Collect(open_store(store_path(args.data_root)), args.data_root, args.limit).run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
