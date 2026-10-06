"""Merged dossiers use current parent identity and every contact's evidence."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle, MessageChannel, MessageEntry, MessageObservation,
)
from packs.ingestion.primitives.deep_context.db.context_queries import dossier_message_count
from packs.ingestion.primitives.deep_context.db.models import OwnerContextRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact, project_person_source_bundle
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.check_readiness import sqlite_counts
from packs.ingestion.primitives.deep_context.synthesis.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
from packs.ingestion.primitives.deep_context.synthesis.validate_dossiers import collect_rows
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class ParentMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows((OwnerContextRow("owner", json.dumps({"name": "Mailbox Owner"}),
                                             str(self.root / "owner.json"), "owner"),))

    def _merged(self, *, phone: bool):
        name = "Jordan Bravo" if phone else "Casey Delta"
        parent_id, absorbed = "parent-aaa11111", "parent-zzz22222"
        counts = (2, 103) if phone else (11, 13)
        people = ("person-first", "person-second")
        self.db.project_rows(tuple(
            row for person, parent in zip(people, (parent_id, absorbed))
            for row in (ParentRow(parent, f"parent-worth:{parent}", name, f"current-{parent}"),
                        PersonRow(person, parent, display_name=name))
        ))
        self.db.replace_imported_people(tuple(PeopleRow(id=person, full_name=name) for person in people))
        for index, person in enumerate(people):
            is_phone = phone and index == 1
            channel = MessageChannel.WHATSAPP if is_phone else MessageChannel.GMAIL
            offsets = range(counts[index]) if index == 0 or phone else range(8, 21)
            messages = [MessageEntry.of(channel, (datetime(2026, 1, 1, tzinfo=timezone.utc) +
                                                  timedelta(days=offset)).isoformat(),
                                       from_me=False, text=f"Synthetic message {offset}") for offset in offsets]
            payload = {"person_id": person, "full_name": name,
                       "emails": [] if is_phone else [f"contact{index}@example.com"],
                       "phones": ["+15550100123"] if is_phone else [],
                       "source_channels": ["whatsapp" if is_phone else "gmail_msgvault"],
                       "messages": [message.to_payload() for message in messages]}
            bundle = CollectionBundle.from_payload(payload)
            path = self.root / f"{person}.json"
            path.write_text(json.dumps(bundle.to_payload()))
            project_person_source_bundle(self.db, path, person)
            facts = {"canonical_name": name, "relationship_to_owner": "Known collaborator",
                     "topics": [f"Independent topic {index}"], "confidence": 0.9}
            record = {"facts": facts, "messages_used": len(messages), "messages_available": len(messages),
                      "batches_used": index + 1, "stop_reason": "completed",
                      "source_channels": payload["source_channels"],
                      "messages": [MessageObservation.of(message).to_payload() for message in messages],
                      "updated_at": f"2026-02-0{index + 1}T00:00:00Z"}
            path = self.root / f"{person}.jsonl"
            path.write_text(json.dumps(record) + "\n")
            project_person_fact(self.db, path, person)
        normalize_parent_cache(self.db, raw_dir=self.root / "raw", facts_dir=self.root / "facts")
        self.db.merge_parents(parent_id, absorbed)
        normalize_parent_cache(self.db, raw_dir=self.root / "raw", facts_dir=self.root / "facts")
        evidence = {row.artifact_key: row for row in artifacts(self.db) if row.kind in {"facts", "source_bundle"}}
        facts_before = [tuple(row) for row in self.db.query("SELECT * FROM facts ORDER BY subject_key")]
        ComposeDossier(db=self.db, dossier_dir=self.root / "dossiers", index_md=self.root / "index.md").execute()
        body = (self.root / "dossiers" / f"current-{parent_id}.md").read_text()
        front = dict(line.split(": ", 1) for line in body.split("---", 2)[1].strip().splitlines())
        expected = 105 if phone else 21
        self.assertEqual(front["person_id"], parent_id)
        self.assertEqual(front["slug"], f"current-{parent_id}")
        self.assertEqual(json.loads(front["emails"]), ["contact0@example.com"] if phone else
                         ["contact0@example.com", "contact1@example.com"])
        self.assertEqual(json.loads(front["phones"]), ["+15550100123"] if phone else [])
        self.assertEqual(json.loads(front["source_channels"]), ["gmail_msgvault", "whatsapp"] if phone else
                         ["gmail_msgvault"])
        self.assertEqual(int(front["message_count"]), expected)
        self.assertIn(f"_grokked {expected} of {expected} messages over 3 batch(es)", body)
        self.assertIn("Independent topic 0", body)
        self.assertIn("Independent topic 1", body)
        self.assertEqual(sqlite_counts(self.db).messages.total, expected)
        # The relationship judge is told the same distinct count the dossier shows.
        self.assertEqual(dossier_message_count(self.db, parent_id), expected)
        rows = collect_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].messages_used, rows[0].messages_available), (expected, expected))
        self.assertEqual({row.artifact_key: row for row in artifacts(self.db)
                          if row.kind in {"facts", "source_bundle"}}, evidence)
        self.assertEqual([tuple(row) for row in self.db.query("SELECT * FROM facts ORDER BY subject_key")], facts_before)

    def test_email_and_phone_parent_metadata_uses_all_105_messages(self):
        self._merged(phone=True)

    def test_overlapping_email_parent_metadata_counts_21_unique_messages(self):
        self._merged(phone=False)
