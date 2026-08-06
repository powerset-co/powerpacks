from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesize_person_context import SynthesizePersonContext
from packs.ingestion.primitives.deep_context.synthesis import prompting, runner, selection


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
            for person_id in ("unchanged", "changed", "stale", "missing"):
                bundle = {"person_id": person_id, "messages": [{"text": person_id}]}
                (raw_dir / f"{person_id}.json").write_text(
                    json.dumps(bundle), encoding="utf-8",
                )
                if person_id == "missing":
                    projected.extend((
                        ParentRow(f"parent-{person_id}", f"parent-{person_id}"),
                        PersonRow(person_id, f"parent-{person_id}"),
                        ArtifactRow(
                            f"source-bundle:{person_id}", "source_bundle",
                            f"parent-{person_id}", str(raw_dir / f"{person_id}.json"),
                            "1" * 64, "projected", person_id=person_id,
                            payload_json=json.dumps(bundle),
                        ),
                    ))
                    continue
                record = {
                    "synthesis_version": (
                        "old" if person_id == "stale" else prompting.SYNTHESIS_VERSION
                    ),
                    "input_evidence_fingerprint": (
                        "old-fingerprint"
                        if person_id == "changed"
                        else self.fingerprint(bundle)
                    ),
                    "facts": {"network_worth": {"decision": "yes", "reason": "pinned"}},
                }
                (facts_dir / f"{person_id}.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8",
                )
                parent_id = f"parent-{person_id}"
                projected.extend((
                    ParentRow(parent_id, parent_id),
                    PersonRow(person_id, parent_id),
                    ArtifactRow(
                        f"source-bundle:{person_id}", "source_bundle", parent_id,
                        str(raw_dir / f"{person_id}.json"), "1" * 64,
                        "projected", person_id=person_id,
                        payload_json=json.dumps(bundle),
                    ),
                    ArtifactRow(
                        f"facts:{person_id}", "facts", parent_id,
                        str(facts_dir / f"{person_id}.jsonl"), "0" * 64,
                        "projected", person_id=person_id,
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
                person_id="",
            )

            self.assertEqual(
                [bundle["person_id"] for bundle in bundles],
                ["changed", "missing", "stale"],
            )
            self.assertEqual(
                [bundle["person_id"] for bundle in selection.pending_target_bundles(
                    database,
                    system_prompt=prompting.SYSTEM_PROMPT,
                    chunk_chars=9000,
                    max_batches=20,
                    force=True, person_id="",
                )],
                ["changed", "missing", "stale", "unchanged"],
            )

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

    def test_mocked_node_run_writes_and_projects_fixed_fact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            bundle = {
                "person_id": "person-1",
                "full_name": "Jordan Bravo",
                "source_channels": ["gmail"],
                "messages": [{
                    "at": "2026-01-02T03:04:05Z", "direction": "from_them",
                    "channel": "gmail", "subject": "Launch", "text": "Ready.",
                }],
            }
            (raw_dir / "person-1.json").write_text(json.dumps(bundle), encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows((
                ParentRow("parent-1", "parent-1"),
                PersonRow("person-1", "parent-1"),
                ArtifactRow(
                    "source-bundle:person-1", "source_bundle", "parent-1",
                    str(raw_dir / "person-1.json"), "1" * 64, "projected",
                    person_id="person-1", payload_json=json.dumps(bundle),
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
                (facts_dir / "person-1.jsonl").read_bytes(),
                (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"),
            )
            self.assertEqual(payload.people_done, 1)
            self.assertEqual(payload.tokens, usage)
            row = database.query(
                "SELECT machine_worth, confidence FROM facts WHERE subject_key=?",
                ("person-1",),
            )[0]
            self.assertEqual((row["machine_worth"], row["confidence"]), ("yes", 0.91))
            artifact = database.query(
                "SELECT input_fingerprint FROM artifacts WHERE artifact_key='facts:person-1'"
            )[0]
            self.assertEqual(artifact["input_fingerprint"], self.fingerprint(bundle))


if __name__ == "__main__":
    unittest.main()
