from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
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

    def test_selection_preserves_terminal_human_and_machine_cache_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            for person_id in ("human", "machine", "maybe", "stale"):
                (raw_dir / f"{person_id}.json").write_text("{}", encoding="utf-8")
                version = "old" if person_id == "stale" else prompting.SYNTHESIS_VERSION
                decision = "maybe" if person_id in {"maybe", "stale"} else "yes"
                record = {
                    "synthesis_version": version,
                    "facts": {"network_worth": {"decision": decision, "reason": "pinned"}},
                }
                (facts_dir / f"{person_id}.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8",
                )

            paths = selection.pending_target_paths(
                raw_dir,
                facts_dir,
                force=False,
                person_id="",
                human_worth_person_ids={"human"},
            )

            self.assertEqual([path.stem for path in paths], ["maybe", "stale"])

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


if __name__ == "__main__":
    unittest.main()
