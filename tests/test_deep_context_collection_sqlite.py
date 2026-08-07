from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.collection import collect_person_context
from packs.ingestion.primitives.deep_context.collection import planning
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    EmailMessage,
    MessageChannel,
    MessageEntry,
)
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.shared.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.shared.readiness_models import readiness_payload
from packs.ingestion.primitives.deep_context.collection.collect_person_context import CollectPersonContext
from packs.ingestion.primitives.deep_context.db.models import (
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.shared.csv_io import CsvIO
from deep_context_sqlite_test_helpers import message_payload


class SqliteCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
                PersonRow("person-1", "parent-1", display_name="Jordan Bravo"),
                PersonIdentifiersProjection("person-1", (PersonIdentifierRow("person-1", "phone", "+15550100"),)),
                PersonSourcesProjection("person-1", (PersonSourceRow("person-1", "imessage"),)),
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _collector(self, **kwargs: object) -> CollectPersonContext:
        return CollectPersonContext(
            db=self.db,
            out_dir=self.root / "raw",
            msgvault_db=self.root / "missing-msgvault.db",
            chat_db=self.root / "missing-chat.db",
            wacli_db=self.root / "missing-wacli.db",
            **kwargs,
        )

    def test_collect_hydrates_sqlite_and_projects_full_bundle(self) -> None:
        message = MessageEntry.of(
            MessageChannel.IMESSAGE,
            "2026-08-06T12:00:00Z",
            from_me=False,
            text="Synthetic hello",
        )
        with (
            mock.patch.object(
                collect_person_context.context_sources,
                "probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "collect_person",
                return_value=([message], 1),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "imessage_groups",
                return_value=[],
            ),
            mock.patch.object(
                collect_person_context,
                "now_iso",
                return_value="2026-08-06T12:10:00Z",
            ),
        ):
            result = self._collector().execute()

        self.assertEqual((result.people_total, result.people_with_context), (1, 1))
        bundle_path = self.root / "raw/parent-1.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        artifact = artifacts(self.db, kind="source_bundle")[0]
        self.assertIsNone(artifact.person_id)
        self.assertEqual(artifact.parent_id, "parent-1")
        self.assertEqual(json.loads(artifact.payload_json or "{}"), bundle)
        self.assertEqual(
            json.loads(artifact.payload_json or "{}")["messages"],
            [message.to_payload()],
        )
        self.assertEqual(
            hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "69b8797f4ebe9118442055e2a728ed62e3b2b78ead574848da400a1918afa0ee",
        )
        projected_bundle = CollectionBundle.from_payload(bundle)
        self.assertIsNotNone(projected_bundle)
        self.assertEqual(
            prompting.input_evidence_fingerprint(
                projected_bundle,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
            ),
            "0a858ed3986af1f8be38275136717df1132e591c10538340f772e004fd301909",
        )

    def test_gmail_payload_round_trip_preserves_synthesis_fingerprint(self) -> None:
        email_context = mock.Mock()
        email_context.recent_emails_for.return_value = (
            [
                EmailMessage(
                    "2026-01-02T03:04:05Z",
                    "jordan@example.test",
                    "contact",
                    "Launch",
                    "Ready to ship.",
                )
            ],
            1,
        )
        messages = collect_person_context.context_sources._read_gmail(
            Person(
                "parent-1",
                "Jordan Bravo",
                emails=["jordan@example.test"],
                source_channels=["gmail_msgvault"],
            ),
            email_context,
            {"owner@example.test"},
            1600,
        )
        payload = json.loads(
            json.dumps(
                {
                    "person_id": "parent-1",
                    "full_name": "Jordan Bravo",
                    "emails": ["jordan@example.test"],
                    "phones": [],
                    "source_channels": ["gmail_msgvault"],
                    "groups": [],
                    "thread_participants": [],
                    "messages": [message.to_payload() for message in messages],
                    "messages_available": 1,
                    "capped": False,
                    "collected_at": "2026-01-02T04:00:00Z",
                },
                separators=(",", ":"),
            )
        )
        bundle = CollectionBundle.from_payload(payload)

        self.assertIsNotNone(bundle)
        self.assertEqual(payload["messages"][0]["channel"], "gmail")
        self.assertIn(
            "[gmail 2026-01-02 THEM] Launch: Ready to ship.",
            prompting.render_chunk(bundle, bundle.messages),
        )
        self.assertEqual(
            prompting.input_evidence_fingerprint(
                bundle,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
            ),
            "cf9e7860b77def893f2bbbaab32908b9414e6edd11950ddaeb471daf23430c2d",
        )

    def test_message_payload_parser_accepts_only_persisted_channels(self) -> None:
        for channel in ("gmail", "imessage", "imessage_group", "whatsapp"):
            with self.subTest(channel=channel):
                row = MessageEntry.from_payload(message_payload("hello", channel=channel))
                self.assertIsNotNone(row)
                self.assertEqual(row.channel, MessageChannel(channel))
        self.assertIsNone(
            MessageEntry.from_payload(message_payload("hello", channel="unknown"))
        )

        for timestamp in (None, ""):
            with self.subTest(timestamp=timestamp):
                payload = message_payload("hello")
                if timestamp is None:
                    payload.pop("at")
                else:
                    payload["at"] = timestamp
                row = MessageEntry.from_payload(payload)
                self.assertIsNotNone(row)
                self.assertEqual(row.at, "")

        for field in ("channel", "direction", "text"):
            with self.subTest(missing=field):
                payload = message_payload("hello")
                payload.pop(field)
                self.assertIsNone(MessageEntry.from_payload(payload))

    def test_collect_removes_projection_when_current_bundle_disappears(self) -> None:
        bundle_path = self.root / "raw/parent-1.json"
        bundle_path.parent.mkdir()
        bundle_path.write_text(
            json.dumps(
                {
                    "person_id": "parent-1",
                    "emails": [],
                    "phones": ["+15550100"],
                    "source_channels": ["imessage"],
                    "messages": [message_payload("old")],
                    "collection_policy": {
                        "deep_cap": 1600,
                        "include_groups": False,
                        "max_group_size": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        project_parent_source_bundle(self.db, bundle_path, "parent-1")

        with (
            mock.patch.object(
                collect_person_context.context_sources,
                "probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "collect_person",
                return_value=([], 0),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "imessage_groups",
                return_value=[],
            ),
        ):
            self._collector(force=True).execute()

        self.assertFalse(bundle_path.exists())
        self.assertFalse(artifacts(self.db, kind="source_bundle"))

    def test_matching_projection_skips_source_reads_without_artifact_file(self) -> None:
        bundle_path = self.root / "raw/parent-1.json"
        bundle_path.parent.mkdir()
        bundle_path.write_text(
            json.dumps(
                {
                    "person_id": "parent-1",
                    "emails": [],
                    "phones": ["+15550100"],
                    "source_channels": ["imessage"],
                    "messages": [message_payload("Projected context")],
                    "collection_policy": {
                        "deep_cap": 1600,
                        "include_groups": False,
                        "max_group_size": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        project_parent_source_bundle(self.db, bundle_path, "parent-1")
        bundle_path.unlink()

        with (
            mock.patch.object(
                collect_person_context.context_sources,
                "probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "collect_person",
            ) as collect,
        ):
            result = self._collector().execute()

        collect.assert_not_called()
        self.assertEqual(result.people_skipped_existing, 1)
        self.assertEqual(result.people_with_context, 1)

    def test_default_scope_removes_projected_group_bundle_before_recollection(self) -> None:
        bundle_path = self.root / "raw/parent-1.json"
        bundle_path.parent.mkdir()
        bundle_path.write_text(
            json.dumps(
                {
                    "person_id": "parent-1",
                    "emails": [],
                    "phones": ["+15550100"],
                    "source_channels": ["imessage"],
                    "messages": [
                        message_payload(
                            "Private group context",
                            channel="imessage_group",
                            subject="Founders",
                        )
                    ],
                    "collection_policy": {
                        "deep_cap": 1600,
                        "include_groups": True,
                        "max_group_size": 25,
                    },
                }
            ),
            encoding="utf-8",
        )
        project_parent_source_bundle(self.db, bundle_path, "parent-1")

        with (
            mock.patch.object(
                collect_person_context.context_sources,
                "probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "collect_person",
                return_value=([], 0),
            ),
            mock.patch.object(
                collect_person_context.context_sources.ContextSources,
                "imessage_groups",
                return_value=[],
            ),
        ):
            result = self._collector().execute()

        self.assertEqual(result.bundles_purged_for_scope, 1)
        self.assertFalse(bundle_path.exists())
        self.assertFalse(artifacts(self.db, kind="source_bundle"))

    def test_collection_skips_owner_member_without_hiding_family(self) -> None:
        self.db.project_rows(
            (
                PersonSourcesProjection(
                    "person-1",
                    (
                        PersonSourceRow("person-1", "imessage"),
                        PersonSourceRow("person-1", "linkedin_csv"),
                    ),
                ),
                PersonRow("owner-person", "parent-1", is_owner=1),
                PersonIdentifiersProjection(
                    "owner-person", (PersonIdentifierRow("owner-person", "email", "owner@example.test"),)
                ),
                PersonSourcesProjection("owner-person", (PersonSourceRow("owner-person", "gmail_msgvault"),)),
                ParentRow("owner-only", "parent-worth:owner-only", "Owner Only"),
                PersonRow("owner-only-person", "owner-only", is_owner=1),
                PersonIdentifiersProjection(
                    "owner-only-person", (PersonIdentifierRow("owner-only-person", "phone", "+15550199"),)
                ),
                PersonSourcesProjection("owner-only-person", (PersonSourceRow("owner-only-person", "imessage"),)),
            )
        )

        people = planning.source_parents(self.db)

        self.assertEqual([person.person_id for person in people], ["parent-1"])
        self.assertEqual(people[0].emails, [])
        self.assertEqual(people[0].phones, ["+15550100"])
        self.assertEqual(people[0].source_channels, ["imessage", "linkedin_csv"])

    def test_readiness_counts_current_people_input_and_sqlite_outputs(self) -> None:
        self.db.project_rows(
            (
                ParentRow("parent-2", "parent-worth:parent-2", "Casey Delta"),
                PersonRow("candidate:email:casey@example.test", "parent-2", display_name="Casey Delta"),
                PersonIdentifiersProjection(
                    "candidate:email:casey@example.test",
                    (
                        PersonIdentifierRow(
                            "candidate:email:casey@example.test",
                            "email",
                            "casey@example.test",
                        ),
                    ),
                ),
                PersonSourcesProjection(
                    "candidate:email:casey@example.test",
                    (PersonSourceRow("candidate:email:casey@example.test", "gmail_msgvault"),),
                ),
            )
        )
        bundle_path = self.root / "raw/parent-1.json"
        bundle_path.parent.mkdir()
        bundle_path.write_text(
            json.dumps(
                {
                    "person_id": "parent-1",
                    "messages": [message_payload("Synthetic hello")],
                }
            ),
            encoding="utf-8",
        )
        project_parent_source_bundle(self.db, bundle_path, "parent-1")
        wacli = self.root / "wacli.db"
        wacli.touch()
        people_csv = self.root / "people.csv"
        CsvIO.write_dict_rows(
            people_csv,
            ["id", "full_name", "primary_email", "primary_phone", "source_channels"],
            [
                {
                    "id": "person-1",
                    "full_name": "Jordan Bravo",
                    "primary_phone": "+15550100",
                    "source_channels": "imessage",
                },
                {
                    "id": "candidate:email:casey@example.test",
                    "full_name": "Casey Delta",
                    "primary_email": "casey@example.test",
                    "source_channels": "gmail_msgvault",
                },
            ],
        )

        with (
            mock.patch.object(
                collect_person_context.context_sources,
                "probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}),
        ):
            result = CheckReadiness(
                db=self.db,
                people_csv=people_csv,
                msgvault_db=self.root / "missing-msgvault.db",
                chat_db=self.root / "missing-chat.db",
                wacli_db=wacli,
            ).run()

        payload = readiness_payload(result)
        self.assertEqual(
            list(payload),
            [
                "source",
                "status",
                "ready",
                "message_people",
                "candidates",
                "messages",
                "checks",
                "advice",
                "updated_at",
                "next_command",
            ],
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.message_people, 2)
        self.assertEqual(
            payload["candidates"],
            {
                "total": 1,
                "per_source": {"gmail_msgvault": 1},
                "with_dossiers": 0,
            },
        )
        self.assertEqual(
            payload["messages"],
            {
                "total": 1,
                "per_source": {"imessage": 1},
            },
        )
        self.assertEqual(result.checks.people_csv.status, "ok")


if __name__ == "__main__":
    unittest.main()
