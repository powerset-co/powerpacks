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
from packs.ingestion.primitives.deep_context.collection import context_sources
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB

MAX_GROUP_SIZE = 25


DEFAULT_LIMIT = 100_000  # more candidates than any store has; --limit N narrows a run to the first N


def _people(conn: sqlite3.Connection, limit: int) -> list[Person]:
    """The candidates to collect, in the shape v1's message readers take.

    A `Person` carries the lookup keys the readers match messages on (the candidate's normalized
    emails and phones) and its channels (which stores to open for it). One candidate at a time,
    three small queries each; 600 candidates take well under a second. The owner is skipped:
    nobody builds a dossier on the operator.
    """
    people = []
    for row in conn.execute(
        "SELECT candidate_id, display_name FROM candidates WHERE is_owner = 0 ORDER BY candidate_id LIMIT ?",
        (limit,),
    ):
        person = Person(row["candidate_id"], row["display_name"])
        for identifier in conn.execute(
            "SELECT kind, normalized_value FROM candidate_identifiers WHERE candidate_id = ? ORDER BY kind, normalized_value",
            (person.person_id,),
        ):
            if identifier["kind"] == "email":
                person.emails.append(identifier["normalized_value"])
            else:
                person.phones.append(identifier["normalized_value"])
        for source in conn.execute(
            "SELECT source FROM candidate_sources WHERE candidate_id = ? ORDER BY source", (person.person_id,)
        ):
            person.source_channels.append(source["source"])
        people.append(person)
    return people


class Collect(Node):
    name = "collect"
    reads = ("candidates", "candidate_identifiers", "candidate_sources")
    writes = ("bundles",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, limit: int) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        self.wacli_db = data_root / context_sources.DEFAULT_WACLI_DB.relative_to(".powerpacks")

    # No required_files: a store is opened only for candidates whose channel came from it, and
    # a candidate carries a channel only because the importer read that store. It exists.

    def execute(self) -> dict[str, int]:
        people = _people(self.conn, self.limit)
        sources = context_sources.ContextSources(
            store=context_sources.gni.MsgvaultStore(DEFAULT_MSGVAULT_DB),
            chat_db=DEFAULT_CHAT_DB,
            wacli_db=self.wacli_db,
            deep_cap=context_sources.CHAT_MESSAGE_CAP,
            max_group_size=MAX_GROUP_SIZE,
        )
        sources.readiness(people=people)
        counts = {
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
            messages, available = sources.collect_person(person)
            if not messages:
                counts["no_messages"] += 1
                continue
            bundle = CollectionBundle.of(
                person,
                messages=messages,
                groups=sources.imessage_groups(person),
                thread_participants=sources.thread_participants(person),
                available=available,
            )
            payload_json = json.dumps(bundle.to_payload(), sort_keys=True, separators=(",", ":"))
            self.conn.execute(
                "INSERT INTO bundles (candidate_id, payload_json, content_fingerprint, collected_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (candidate_id) DO UPDATE SET payload_json = excluded.payload_json, "
                "content_fingerprint = excluded.content_fingerprint, collected_at = excluded.collected_at",
                (person.person_id, payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest(), now_iso()),
            )
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
