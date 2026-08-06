from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesize_person_context import SynthesizePersonContext
from packs.ingestion.primitives.deep_context.synthesis import (
    normalization,
    prompting,
    runner,
    selection,
)


class _FakeResponses:
    async def create(self, **kwargs):
        return object()


class _FakeClient:
    responses = _FakeResponses()

    async def close(self) -> None:
        return None


class DeepContextSynthesisTests(unittest.TestCase):
    @staticmethod
    def fingerprint(bundle):
        return prompting.input_evidence_fingerprint(
            bundle,
            system_prompt=prompting.SYSTEM_PROMPT,
            chunk_chars=9000,
            max_batches=20,
        )

    def test_schema_asset_and_version_match_pinned_contract(self) -> None:
        asset = json.loads(
            Path(prompting.__file__).with_name("fact_schema.json").read_text(encoding="utf-8")
        )
        canonical = json.dumps(
            asset, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(asset, prompting.FACT_SCHEMA)
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            "417f25c6ac74e1008038ef317cfe026b0a142423914c3ead33ad37f8e3086a79",
        )
        self.assertEqual(prompting.SYNTHESIS_VERSION, "3da778f46bbb")

    def test_bundle_evidence_fingerprint_serialization_is_pinned(self) -> None:
        self.assertEqual(
            self.fingerprint({"person_id": "p1", "messages": []}),
            "faf93accb97ae052c1248f3fa5ba7cb82b0397189429df0db1ecfa4c131791f0",
        )
        self.assertEqual(
            self.fingerprint({
                "person_id": "p1",
                "messages": [{"channel": "whatsapp", "at": "2026-01-01", "text": "hello"}],
                "messages_available": 1,
            }),
            "9b0a07412ade4d102c5a17ee17d09621c5ee6efe8084bdae1f9ab9f535b58ffd",
        )

    def test_selection_reuses_only_matching_artifact_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            database = Db(root / "deep-context.sqlite")
            projected = []
            for suffix in ("unchanged", "changed", "stale", "missing"):
                parent_id = f"parent-{suffix}"
                child_id = f"person-{suffix}"
                bundle = {"person_id": parent_id, "messages": [{"text": suffix}]}
                (raw_dir / f"{parent_id}.json").write_text(
                    json.dumps(bundle), encoding="utf-8",
                )
                if suffix == "missing":
                    projected.extend((
                        ParentRow(parent_id, parent_id),
                        PersonRow(child_id, parent_id),
                        ArtifactRow(
                            f"source-bundle:{parent_id}", "source_bundle",
                            parent_id, str(raw_dir / f"{parent_id}.json"),
                            "1" * 64, "projected",
                            payload_json=json.dumps(bundle),
                        ),
                    ))
                    continue
                record = {
                    "synthesis_version": (
                        "old" if suffix == "stale" else prompting.SYNTHESIS_VERSION
                    ),
                    "input_evidence_fingerprint": (
                        "old-fingerprint"
                        if suffix == "changed"
                        else self.fingerprint(bundle)
                    ),
                    "facts": {"network_worth": {"decision": "yes", "reason": "pinned"}},
                }
                (facts_dir / f"{parent_id}.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8",
                )
                projected.extend((
                    ParentRow(parent_id, parent_id),
                    PersonRow(child_id, parent_id),
                    ArtifactRow(
                        f"source-bundle:{parent_id}", "source_bundle", parent_id,
                        str(raw_dir / f"{parent_id}.json"), "1" * 64,
                        "projected",
                        payload_json=json.dumps(bundle),
                    ),
                    ArtifactRow(
                        f"facts:{parent_id}", "facts", parent_id,
                        str(facts_dir / f"{parent_id}.jsonl"), "0" * 64,
                        "projected",
                        input_fingerprint=record["input_evidence_fingerprint"],
                        payload_json=json.dumps(record),
                    ),
                ))
            database.project_rows(tuple(projected))

            bundles = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=False,
                parent_id="",
            )

            self.assertEqual(
                [bundle["person_id"] for bundle in bundles],
                ["parent-changed", "parent-missing", "parent-stale"],
            )
            self.assertEqual(
                [bundle["person_id"] for bundle in selection.pending_target_bundles(
                    database,
                    system_prompt=prompting.SYSTEM_PROMPT,
                    chunk_chars=9000,
                    max_batches=20,
                    force=True, parent_id="",
                )],
                ["parent-changed", "parent-missing", "parent-stale", "parent-unchanged"],
            )

    def test_selection_skips_owner_only_parent_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Db(Path(directory) / "deep-context.sqlite")
            owner_bundle = {
                "person_id": "parent-owner-only",
                "messages": [{"text": "owner cache"}],
            }
            mixed_bundle = {
                "person_id": "parent-mixed",
                "messages": [{"text": "family cache"}],
            }
            database.project_rows((
                ParentRow("parent-owner-only", "parent-owner-only"),
                PersonRow("owner-only", "parent-owner-only", is_owner=1),
                ArtifactRow(
                    "source-bundle:parent-owner-only", "source_bundle",
                    "parent-owner-only", "raw/parent-owner-only.json", "1" * 64,
                    "projected", payload_json=json.dumps(owner_bundle),
                ),
                ParentRow("parent-mixed", "parent-mixed"),
                PersonRow("owner-member", "parent-mixed", is_owner=1),
                PersonRow("family-member", "parent-mixed"),
                ArtifactRow(
                    "source-bundle:parent-mixed", "source_bundle",
                    "parent-mixed", "raw/parent-mixed.json", "2" * 64,
                    "projected", payload_json=json.dumps(mixed_bundle),
                ),
            ))

            bundles = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=True,
                parent_id="",
            )

            self.assertEqual(
                [bundle["person_id"] for bundle in bundles], ["parent-mixed"],
            )

    def test_estimate_does_not_normalize_or_mutate_child_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            child_id = "person-child"
            parent_id = "parent-one"
            bundle = {
                "person_id": child_id,
                "messages": [{"text": "cached message"}],
            }
            record = {
                "facts": {
                    "network_worth": {"decision": "yes", "reason": "cached"}
                },
                "final_confidence": 0.9,
            }
            raw_path = raw_dir / f"{child_id}.json"
            fact_path = facts_dir / f"{child_id}.jsonl"
            raw_path.write_text(json.dumps(bundle), encoding="utf-8")
            fact_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows((
                ParentRow(parent_id, "parent-one"),
                PersonRow(child_id, parent_id),
                ArtifactRow(
                    f"source-bundle:{child_id}", "source_bundle", parent_id,
                    str(raw_path), "1" * 64, "projected", person_id=child_id,
                    payload_json=json.dumps(bundle),
                ),
                ArtifactRow(
                    f"facts:{child_id}", "facts", parent_id, str(fact_path),
                    "2" * 64, "projected", person_id=child_id,
                    payload_json=json.dumps(record),
                ),
                FactRow(
                    child_id, parent_id, f"facts:{child_id}", child_id,
                    "yes", "cached", 0.9,
                    facts_json=json.dumps(record["facts"]),
                ),
            ))
            before = [dict(row) for row in database.query(
                "SELECT * FROM artifacts ORDER BY artifact_key"
            )]

            payload = SynthesizePersonContext(
                db=database, raw_dir=raw_dir, out_dir=facts_dir,
            ).estimate()

            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["people"], 0)
            self.assertEqual(
                [dict(row) for row in database.query(
                    "SELECT * FROM artifacts ORDER BY artifact_key"
                )],
                before,
            )
            self.assertTrue(raw_path.exists())
            self.assertTrue(fact_path.exists())
            self.assertFalse((raw_dir / f"{parent_id}.json").exists())
            self.assertFalse((facts_dir / f"{parent_id}.jsonl").exists())

    def test_estimate_previews_child_bundle_migration_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            child_id = "person-child"
            parent_id = "parent-one"
            bundle = {
                "person_id": child_id,
                "messages": [{"text": "new cached message"}],
            }
            raw_path = raw_dir / f"{child_id}.json"
            raw_path.write_text(json.dumps(bundle), encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows((
                ParentRow(parent_id, "parent-one", "Jordan Bravo"),
                PersonRow(child_id, parent_id),
                ArtifactRow(
                    f"source-bundle:{child_id}",
                    "source_bundle",
                    parent_id,
                    str(raw_path),
                    "1" * 64,
                    "projected",
                    person_id=child_id,
                    payload_json=json.dumps(bundle),
                ),
            ))
            before = [dict(row) for row in database.query(
                "SELECT * FROM artifacts ORDER BY artifact_key"
            )]

            stage = SynthesizePersonContext(
                db=database, raw_dir=raw_dir, out_dir=facts_dir,
            )
            payload = stage.estimate()

            self.assertEqual(payload["people"], 1)
            self.assertEqual(
                [dict(row) for row in database.query(
                    "SELECT * FROM artifacts ORDER BY artifact_key"
                )],
                before,
            )
            self.assertTrue(raw_path.exists())
            self.assertFalse((raw_dir / f"{parent_id}.json").exists())

            self.assertEqual(len(stage._migrate_parent_cache().bundles), 1)

    def test_rendering_bytes_are_pinned(self) -> None:
        person = {
            "full_name": "Jordan Bravo",
            "emails": ["jordan@example.com"],
            "phones": ["+15550100"],
            "source_channels": ["gmail"],
            "groups": ["Founders"],
            "thread_participants": [
                {"subject": "Launch", "participants": ["me@example.com", "jordan@example.com"]},
            ],
        }
        message = {
            "at": "2026-01-02T03:04:05Z",
            "direction": "from_them",
            "channel": "gmail",
            "subject": "Launch",
            "text": "Ready to ship.",
        }
        expected = (
            "CONTACT: Jordan Bravo\n"
            "Known emails: jordan@example.com\n"
            "Known phones: +15550100\n"
            "Channels: gmail\n"
            "Shared group chats (names only): Founders\n\n"
            "EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):\n"
            "- Launch — me@example.com, jordan@example.com\n\n"
            "MESSAGES (most relevant, chronological):\n"
            "[gmail 2026-01-02 THEM] Launch: Ready to ship."
        )
        self.assertEqual(prompting.render_chunk(person, [message]), expected)

    def test_legacy_child_cache_excludes_unjudged_facts_from_worth_election(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir, facts_dir = root / "raw", root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            database = Db(root / "deep-context.sqlite")
            rows: list[object] = [
                ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
                PersonRow("person-a", "parent-1"),
                PersonRow("person-b", "parent-1"),
            ]
            for person_id, machine_worth, embedded_worth, channel in (
                ("person-a", "no", "no", "gmail"),
                ("person-b", None, "maybe", "imessage"),
            ):
                bundle = {
                    "person_id": person_id,
                    "full_name": "Jordan Bravo",
                    "emails": [f"{person_id}@example.test"] if channel == "gmail" else [],
                    "phones": ["+15550100"] if channel == "imessage" else [],
                    "source_channels": [channel],
                    "messages": [{"channel": channel, "at": "2026-01-01", "text": person_id}],
                    "messages_available": 1,
                    "collection_policy": {
                        "deep_cap": 1600,
                        "include_groups": False,
                        "max_group_size": 0,
                    },
                }
                raw_path = raw_dir / f"{person_id}.json"
                raw_path.write_text(json.dumps(bundle), encoding="utf-8")
                fact_payload = {
                    "canonical_name": "Jordan Bravo",
                    "topics": [person_id],
                    "network_worth": {"decision": embedded_worth, "reason": person_id},
                    "confidence": 0.8,
                }
                record = {
                    "facts": fact_payload,
                    "final_confidence": 0.8,
                }
                fact_path = facts_dir / f"{person_id}.jsonl"
                fact_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
                rows.extend((
                    ArtifactRow(
                        f"source-bundle:{person_id}", "source_bundle", "parent-1",
                        str(raw_path), person_id * 4, "projected", person_id=person_id,
                        payload_json=json.dumps(bundle),
                    ),
                    ArtifactRow(
                        f"facts:{person_id}", "facts", "parent-1", str(fact_path),
                        (machine_worth or "unjudged") * 16, "projected", person_id=person_id,
                        payload_json=json.dumps(record),
                    ),
                    FactRow(
                        person_id, "parent-1", f"facts:{person_id}", person_id,
                        machine_worth, person_id, 0.8, facts_json=json.dumps(fact_payload),
                    ),
                ))
            database.project_rows(tuple(rows))

            migrated = normalization.normalize_parent_cache(
                database,
                raw_dir=raw_dir,
                facts_dir=facts_dir,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
            )

            self.assertEqual(migrated, 1)
            parent_bundle = json.loads((raw_dir / "parent-1.json").read_text())
            self.assertEqual(parent_bundle["person_id"], "parent-1")
            self.assertEqual(len(parent_bundle["messages"]), 2)
            parent_fact = database.query("SELECT * FROM facts")[0]
            self.assertEqual(
                (parent_fact["subject_key"], parent_fact["person_id"], parent_fact["machine_worth"]),
                ("parent-1", None, "no"),
            )
            parent_artifact = database.query(
                "SELECT payload_json FROM artifacts WHERE artifact_key='facts:parent-1'"
            )[0]
            self.assertEqual(
                json.loads(parent_artifact["payload_json"])["synthesis_version"],
                prompting.SYNTHESIS_VERSION,
            )
            self.assertEqual(
                selection.pending_target_bundles(
                    database,
                    system_prompt=prompting.SYSTEM_PROMPT,
                    chunk_chars=9000,
                    max_batches=20,
                    force=False,
                    parent_id="",
                ),
                [],
            )
            self.assertFalse((raw_dir / "person-a.json").exists())
            self.assertFalse((facts_dir / "person-b.jsonl").exists())

    def test_facts_only_child_is_preserved_until_parent_bundle_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts_dir = root / "facts"
            facts_dir.mkdir()
            path = facts_dir / "person-a.jsonl"
            record = {
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "facts": {"network_worth": {"decision": "maybe", "reason": "cached"}},
            }
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows((
                ParentRow("parent-1", "parent-worth:parent-1"),
                PersonRow("person-a", "parent-1"),
                ArtifactRow(
                    "facts:person-a", "facts", "parent-1", str(path),
                    "1" * 64, "projected", person_id="person-a",
                    payload_json=json.dumps(record),
                ),
                FactRow(
                    "person-a", "parent-1", "facts:person-a", "person-a",
                    "maybe", "cached", facts_json=json.dumps(record["facts"]),
                ),
            ))

            migrated = normalization.normalize_parent_cache(
                database,
                raw_dir=root / "raw",
                facts_dir=facts_dir,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
            )

            self.assertEqual(migrated, 0)
            self.assertTrue(path.exists())
            self.assertEqual(database.query("SELECT subject_key FROM facts")[0][0], "person-a")

    def test_mocked_node_run_writes_and_projects_fixed_fact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            bundle = {
                "person_id": "parent-1",
                "full_name": "Jordan Bravo",
                "source_channels": ["gmail"],
                "messages": [{
                    "at": "2026-01-02T03:04:05Z", "direction": "from_them",
                    "channel": "gmail", "subject": "Launch", "text": "Ready.",
                }],
            }
            (raw_dir / "parent-1.json").write_text(json.dumps(bundle), encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows((
                ParentRow("parent-1", "parent-1"),
                PersonRow("person-1", "parent-1"),
                ArtifactRow(
                    "source-bundle:parent-1", "source_bundle", "parent-1",
                    str(raw_dir / "parent-1.json"), "1" * 64, "projected",
                    payload_json=json.dumps(bundle),
                ),
            ))
            facts = {
                "canonical_name": "Jordan Bravo",
                "relationship_category": "work",
                "confidence": 0.91,
                "network_worth": {"decision": "yes", "reason": "Real correspondence"},
            }
            usage = {"input_tokens": 12, "output_tokens": 3, "reasoning_tokens": 4}
            node = SynthesizePersonContext(
                db=database,
                raw_dir=raw_dir,
                out_dir=facts_dir,
                no_owner=True,
                concurrency=1,
            )

            with (
                mock.patch.object(runner, "load_env"),
                mock.patch.object(runner, "make_async_client", return_value=_FakeClient()),
                mock.patch.object(runner, "parse_json_response", return_value=facts.copy()),
                mock.patch.object(runner, "usage_tokens", return_value=usage),
            ):
                payload = node.run()

            record = {
                "chunk_index": 0,
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "input_evidence_fingerprint": self.fingerprint(bundle),
                "facts": facts,
                "usage": usage,
                "batches_used": 1,
                "batches_total": 1,
                "messages_used": 1,
                "messages_available": 1,
                "final_confidence": 0.91,
                "stop_reason": "confident",
            }
            self.assertEqual(
                (facts_dir / "parent-1.jsonl").read_bytes(),
                (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"),
            )
            self.assertEqual(payload.people_done, 1)
            self.assertEqual(payload.tokens, usage)
            row = database.query(
                "SELECT machine_worth, confidence FROM facts WHERE subject_key=?",
                ("parent-1",),
            )[0]
            self.assertEqual((row["machine_worth"], row["confidence"]), ("yes", 0.91))
            artifact = database.query(
                "SELECT input_fingerprint FROM artifacts WHERE artifact_key='facts:parent-1'"
            )[0]
            self.assertEqual(artifact["input_fingerprint"], self.fingerprint(bundle))


if __name__ == "__main__":
    unittest.main()
