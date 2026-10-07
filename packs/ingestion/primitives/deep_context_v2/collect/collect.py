"""Block 02 Collect: one bounded bundle of message bodies per candidate, read locally. Free.

Spec: Deep Context Spec, "02 Collect" (keep as is) and the `bundles` DDL. Reads every
non-owner candidate's own identifiers and channels from the v2 store, reads its messages
from msgvault.db, chat.db and wacli.db with v1's readers, and upserts one `bundles` row.
A candidate with no messages gets no row. A rerun recollects every candidate.

From v1:
- deep_context.collection.context_sources: ContextSources, CHAT_MESSAGE_CAP, DEFAULT_WACLI_DB, gni.MsgvaultStore
- deep_context.collection.models: CollectionBundle
- deep_context.shared.common: Person
- common.paths: DEFAULT_MSGVAULT_DB
- discover.messages.extract_imessage: DEFAULT_CHAT_DB

v1 lines copied:
- deep_context/db/context_queries.py:140-171 (collection_sources) -> `_people`, re-pointed at the v2 tables
- deep_context/collection/collect_person_context.py:75-81 (ContextSources construction), 114-143 (per-person loop)
- deep_context/db/projectors.py:196 + common/jsonio.py:100 -> payload_json: the file is written
  sort_keys, then stored compact, so the stored bytes are sorted-key compact JSON (byte-equal to
  v1's stored bundle); content_fingerprint is the sha256 of those stored bytes (v1 hashed a
  pretty-printed file nobody keeps; dropped 2026-10-06 after review)

Dropped:
- context_queries.py:148-152 EXISTS message-source filter: v2 candidate_sources holds message channels only
- context_queries.py:147 `kind IN ('email','phone')`: the DDL CHECK allows nothing else
- collect_person_context.py:96,116-120 person_histories / processed-hash exclusion: carry-forward; rerun recollects
- collect_person_context.py:121-122 `if not messages and history: continue`: prior-run skip
- collect_person_context.py:125-126 `if not messages and not groups`: now `if not messages`, the spec's "no messages, no bundle"
- collect_person_context.py:134-135 dry_run: no dry run in a free block
- collect_person_context.py:144-145 write_json raw/<id>.json + project_person_source_bundle: the store is the record
- collect_person_context.py:146-147 per-25 progress print: one summary line at the end
- collect_person_context.py:113,148-149 try/finally around close: single process, read-only connection
- collect_person_context.py:151-152 normalize_cached_bundles (parent bundles): v2 collects per candidate only
- collect_person_context.py:153-154,156-189 group-body count, privacy receipt, timings, CollectPersonContextManifest: Node.run writes the manifest
- collect_person_context.py:201-216 --db/--out-dir/--msgvault-db/--chat-db/--wacli-db/--deep-cap/--max-group-size: fixed v1 tuning, paths from --data-root

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


def _people(conn: sqlite3.Connection, limit: int | None) -> list[Person]:
    """Each non-owner candidate's own lookup keys: normalized emails and phones, its channels."""
    rows = conn.execute(
        "SELECT candidate_id, display_name FROM candidates WHERE is_owner = 0 ORDER BY candidate_id LIMIT ?",
        (-1 if limit is None else limit,),
    ).fetchall()
    people: dict[str, Person] = {}
    for row in rows:
        people[row["candidate_id"]] = Person(row["candidate_id"], row["display_name"])
    for row in conn.execute("SELECT candidate_id, kind, normalized_value FROM candidate_identifiers ORDER BY 1, 2, 3"):
        if row["candidate_id"] not in people:
            continue
        person = people[row["candidate_id"]]
        if row["kind"] == "email":
            person.emails.append(row["normalized_value"])
        else:
            person.phones.append(row["normalized_value"])
    for row in conn.execute("SELECT candidate_id, source FROM candidate_sources ORDER BY 1, 2"):
        if row["candidate_id"] in people:
            people[row["candidate_id"]].source_channels.append(row["source"])
    return list(people.values())


class Collect(Node):
    name = "collect"
    reads = ("candidates", "candidate_identifiers", "candidate_sources")
    writes = ("bundles",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, limit: int | None) -> None:
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
    parser.add_argument("--limit", type=int, help="first N candidates, for a quick check")
    args = parser.parse_args(argv)
    manifest = Collect(open_store(store_path(args.data_root)), args.data_root, args.limit).run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
