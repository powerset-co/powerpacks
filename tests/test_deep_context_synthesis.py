from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    MessageEntry,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    OwnerContextRow,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import SynthesizePersonContext
from packs.ingestion.primitives.deep_context.synthesis import (
    normalization,
    prompting,
    runner,
    selection,
)
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisConfig
from packs.ingestion.primitives.deep_context.shared import openai_responses
from deep_context_sqlite_test_helpers import message_payload


class _FakeResponses:
    def __init__(self, response):
        self.response = response

    async def create(self, **kwargs):
        return self.response


class _FakeClient:
    def __init__(self, response):
        self.responses = _FakeResponses(response)

    async def close(self) -> None:
        return None


class _FailingResponses:
    async def create(self, **kwargs):
        raise RuntimeError("synthetic provider failure")


class _FailingClient:
    responses = _FailingResponses()

    async def close(self) -> None:
        return None


class _KeyedResponses:
    """Returns a canned response keyed by a marker substring of the rendered
    user prompt, so a multi-batch person can get distinguishable per-batch
    facts back instead of one fixed response for every call."""

    def __init__(self, by_marker: dict[str, object]):
        self.by_marker = by_marker
        self.calls: list[str] = []

    async def create(self, **kwargs):
        prompt = kwargs["input"][1]["content"]
        self.calls.append(prompt)
        for marker, response in self.by_marker.items():
            if marker in prompt:
                return response
        raise AssertionError(f"no fixture response matched prompt: {prompt[:200]}")


class _KeyedClient:
    def __init__(self, by_marker: dict[str, object]):
        self.responses = _KeyedResponses(by_marker)

    async def close(self) -> None:
        return None


class DeepContextSynthesisTests(unittest.TestCase):
    @staticmethod
    def fingerprint(bundle):
        parsed = CollectionBundle.from_payload(bundle)
        if parsed is None:
            raise AssertionError("invalid bundle fixture")
        return prompting.input_evidence_fingerprint(
            parsed,
            system_prompt=prompting.SYSTEM_PROMPT,
            chunk_chars=9000,
            max_batches=20,
        )

    def test_schema_asset_and_version_match_pinned_contract(self) -> None:
        asset = json.loads(Path(prompting.__file__).with_name("fact_schema.json").read_text(encoding="utf-8"))
        canonical = json.dumps(
            asset,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(asset, prompting.FACT_SCHEMA)
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            "417f25c6ac74e1008038ef317cfe026b0a142423914c3ead33ad37f8e3086a79",
        )
        self.assertEqual(prompting.SYNTHESIS_VERSION, "17f80443e758")

    def test_bundle_evidence_fingerprint_serialization_is_pinned(self) -> None:
        self.assertEqual(
            self.fingerprint({"person_id": "p1", "messages": []}),
            "faf93accb97ae052c1248f3fa5ba7cb82b0397189429df0db1ecfa4c131791f0",
        )
        self.assertEqual(
            self.fingerprint(
                {
                    "person_id": "p1",
                    "messages": [
                        message_payload(
                            "hello",
                            channel="whatsapp",
                            at="2026-01-01",
                        )
                    ],
                    "messages_available": 1,
                }
            ),
            "11c3bb41f0e9383e3284eb4136f2792b5fe8062f81daf4bb78bdceda0445225a",
        )

    def test_terminal_provider_failure_returns_no_fabricated_facts(self) -> None:
        async def exercise():
            config = openai_responses.OpenAIResponsesConfig(
                "fixture-model",
                "low",
                1,
                30,
                0,
            )
            caller = openai_responses.OpenAIResponsesCaller(
                config,
                client=_FailingClient(),
            )
            return await runner.call_one(
                caller,
                "fixture prompt",
                system_prompt="fixture system",
            )

        result = asyncio.run(exercise())

        self.assertTrue(result.failed)
        self.assertIsNone(result.facts)

    def test_synthesize_person_fans_out_batches_and_merges_without_a_prior(self) -> None:
        by_marker = {
            "alpha-marker": SimpleNamespace(
                output_text=json.dumps({
                    "canonical_name": "Jordan Bravo",
                    "employers": [{"name": "Acme", "role": "Eng", "status": "current"}],
                    "confidence": 0.6,
                }),
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=2,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
            ),
            "beta-marker": SimpleNamespace(
                output_text=json.dumps({
                    "canonical_name": "Jordan Bravo",
                    "employers": [{"name": "Beta Co", "role": "PM", "status": "past"}],
                    "confidence": 0.7,
                }),
                usage=SimpleNamespace(
                    input_tokens=11,
                    output_tokens=3,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
            ),
        }

        async def exercise():
            responses_config = openai_responses.OpenAIResponsesConfig(
                "fixture-model", "low", 4, 30, 0,
            )
            client = _KeyedClient(by_marker)
            caller = openai_responses.OpenAIResponsesCaller(responses_config, client=client)
            person = CollectionBundle.from_payload({
                "person_id": "parent-multi",
                "full_name": "Jordan Bravo",
                "messages": [
                    message_payload("alpha-marker text", at="2026-01-02T00:00:00Z"),
                    message_payload("beta-marker text", at="2026-01-01T00:00:00Z"),
                ],
            })
            config = SynthesisConfig(
                raw_dir=Path("/raw"),
                facts_dir=Path("/facts"),
                responses=responses_config,
                chunk_chars=1,
                max_batches=20,
                force=False,
                rejudge=False,
            )
            result = await runner.synthesize_person(
                caller, person, config=config, system_prompt="fixture system",
            )
            return result, client.responses.calls

        result, prompts = asyncio.run(exercise())

        self.assertFalse(result.total_failure)
        self.assertEqual(result.record.batches_used, 2)
        self.assertEqual(result.record.batches_total, 2)
        self.assertEqual(result.record.stop_reason, "completed")
        self.assertEqual(
            sorted(employer.name for employer in result.record.facts.employers),
            ["Acme", "Beta Co"],
        )
        # Every batch renders independently: prior=None on every call, never a
        # threaded "PROFILE SO FAR" from an earlier batch in this run.
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertNotIn("PROFILE SO FAR", prompt)

    def test_synthesize_person_single_batch_never_calls_merge(self) -> None:
        """The single-batch short-circuit (chunks[0].facts) must never route
        through merge_batch_facts — merging is only meaningful once there is
        more than one batch to reduce."""
        response = SimpleNamespace(
            output_text=json.dumps({
                "canonical_name": "Jordan Bravo",
                "confidence": 0.5,
            }),
            usage=SimpleNamespace(
                input_tokens=5,
                output_tokens=1,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )

        async def exercise():
            responses_config = openai_responses.OpenAIResponsesConfig(
                "fixture-model", "low", 4, 30, 0,
            )
            caller = openai_responses.OpenAIResponsesCaller(
                responses_config, client=_FakeClient(response),
            )
            person = CollectionBundle.from_payload({
                "person_id": "parent-single",
                "full_name": "Jordan Bravo",
                "messages": [message_payload("hello", at="2026-01-01T00:00:00Z")],
            })
            config = SynthesisConfig(
                raw_dir=Path("/raw"),
                facts_dir=Path("/facts"),
                responses=responses_config,
                chunk_chars=9000,
                max_batches=20,
                force=False,
                rejudge=False,
            )
            with mock.patch.object(
                runner,
                "merge_batch_facts",
                side_effect=AssertionError("a single batch must not be merged"),
            ):
                return await runner.synthesize_person(
                    caller, person, config=config, system_prompt="fixture system",
                )

        result = asyncio.run(exercise())

        self.assertEqual(result.record.batches_used, 1)
        self.assertEqual(result.record.facts.canonical_name, "Jordan Bravo")

    def test_synthesize_person_total_failure_is_not_persisted(self) -> None:
        async def exercise():
            responses_config = openai_responses.OpenAIResponsesConfig(
                "fixture-model", "low", 4, 30, 0,
            )
            caller = openai_responses.OpenAIResponsesCaller(responses_config, client=_FailingClient())
            person = CollectionBundle.from_payload({
                "person_id": "parent-failed",
                "full_name": "Jordan Bravo",
                "messages": [message_payload("hello", at="2026-01-01T00:00:00Z")],
            })
            config = SynthesisConfig(
                raw_dir=Path("/raw"),
                facts_dir=Path("/facts"),
                responses=responses_config,
                chunk_chars=9000,
                max_batches=20,
                force=False,
                rejudge=False,
            )
            return await runner.synthesize_person(
                caller, person, config=config, system_prompt="fixture system",
            )

        result = asyncio.run(exercise())

        self.assertTrue(result.total_failure)
        self.assertIsNone(result.record.facts)
        self.assertEqual(result.record.stop_reason, "failed")
        # No fingerprint computed for a record that is never persisted or matched.
        self.assertEqual(result.record.input_evidence_fingerprint, "")

    def test_mocked_node_run_skips_persisting_a_total_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            facts_dir = root / "facts"
            raw_dir.mkdir()
            bundle = {
                "person_id": "parent-1",
                "full_name": "Jordan Bravo",
                "source_channels": ["gmail_msgvault"],
                "messages": [
                    message_payload(
                        "Ready.",
                        channel="gmail",
                        at="2026-01-02T03:04:05Z",
                        subject="Launch",
                    )
                ],
            }
            (raw_dir / "parent-1.json").write_text(json.dumps(bundle), encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows(
                (
                    OwnerContextRow(
                        "owner",
                        json.dumps({"name": "Mailbox Owner"}),
                        str(root / "owner.json"),
                        "0" * 64,
                    ),
                    ParentRow("parent-1", "parent-1"),
                    PersonRow("person-1", "parent-1"),
                    ArtifactRow(
                        "source-bundle:parent-1",
                        "source_bundle",
                        "parent-1",
                        str(raw_dir / "parent-1.json"),
                        "1" * 64,
                        "projected",
                        payload_json=json.dumps(bundle),
                    ),
                )
            )
            node = SynthesizePersonContext(
                db=database,
                raw_dir=raw_dir,
                out_dir=facts_dir,
                concurrency=1,
            )
            plan = node._plan()

            with mock.patch.object(
                openai_responses,
                "AsyncOpenAI",
                return_value=_FailingClient(),
            ):
                payload = node.run()

            self.assertEqual(payload.people_done, 1)
            self.assertEqual(payload.total_failures, 1)
            self.assertFalse((facts_dir / "parent-1.jsonl").exists())
            self.assertEqual(database.query("SELECT COUNT(*) FROM facts")[0][0], 0)
            # Not cached as done: the person is still pending on the next run.
            self.assertEqual(
                [
                    bundle.person_id
                    for bundle in selection.pending_target_bundles(
                        database,
                        system_prompt=plan.system_prompt,
                        chunk_chars=9000,
                        max_batches=20,
                        force=False,
                    )
                ],
                ["parent-1"],
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
                bundle = {
                    "person_id": parent_id,
                    "messages": [message_payload(suffix)],
                }
                (raw_dir / f"{parent_id}.json").write_text(
                    json.dumps(bundle),
                    encoding="utf-8",
                )
                if suffix == "missing":
                    projected.extend(
                        (
                            ParentRow(parent_id, parent_id),
                            PersonRow(child_id, parent_id),
                            ArtifactRow(
                                f"source-bundle:{parent_id}",
                                "source_bundle",
                                parent_id,
                                str(raw_dir / f"{parent_id}.json"),
                                "1" * 64,
                                "projected",
                                payload_json=json.dumps(bundle),
                            ),
                        )
                    )
                    continue
                record = {
                    "synthesis_version": ("old" if suffix == "stale" else prompting.SYNTHESIS_VERSION),
                    "input_evidence_fingerprint": (
                        "old-fingerprint" if suffix == "changed" else self.fingerprint(bundle)
                    ),
                    "facts": {"network_worth": {"decision": "yes", "reason": "pinned"}},
                }
                (facts_dir / f"{parent_id}.jsonl").write_text(
                    json.dumps(record) + "\n",
                    encoding="utf-8",
                )
                projected.extend(
                    (
                        ParentRow(parent_id, parent_id),
                        PersonRow(child_id, parent_id),
                        ArtifactRow(
                            f"source-bundle:{parent_id}",
                            "source_bundle",
                            parent_id,
                            str(raw_dir / f"{parent_id}.json"),
                            "1" * 64,
                            "projected",
                            payload_json=json.dumps(bundle),
                        ),
                        ArtifactRow(
                            f"facts:{parent_id}",
                            "facts",
                            parent_id,
                            str(facts_dir / f"{parent_id}.jsonl"),
                            "0" * 64,
                            "projected",
                            input_fingerprint=record["input_evidence_fingerprint"],
                            payload_json=json.dumps(record),
                        ),
                    )
                )
            database.project_rows(tuple(projected))

            bundles = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=False,
            )

            self.assertEqual(
                [bundle.person_id for bundle in bundles],
                ["parent-changed", "parent-missing", "parent-stale"],
            )
            self.assertEqual(
                [
                    bundle.person_id
                    for bundle in selection.pending_target_bundles(
                        database,
                        system_prompt=prompting.SYSTEM_PROMPT,
                        chunk_chars=9000,
                        max_batches=20,
                        force=True,
                    )
                ],
                ["parent-changed", "parent-missing", "parent-stale", "parent-unchanged"],
            )

    def test_legacy_child_facts_without_stored_fingerprint_are_not_silently_skipped(self) -> None:
        """The legacy-child shortcut used to hash the CURRENT bundle and compare
        it against itself moments later — an unconditional match. This proves
        the fix: a legacy child FACTS artifact with no recorded
        input_fingerprint (true of every legacy record on a real install)
        must never read as a cache hit, even though a source_bundle exists.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir, facts_dir = root / "raw", root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            database = Db(root / "deep-context.sqlite")
            parent_id, child_id = "parent-legacy", "person-legacy"
            bundle = {
                "person_id": parent_id,
                "messages": [message_payload("brand new message")],
            }
            child_record = {
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "facts": {"network_worth": {"decision": "yes", "reason": "legacy"}},
            }
            database.project_rows(
                (
                    ParentRow(parent_id, parent_id, "Jordan Bravo"),
                    PersonRow(child_id, parent_id),
                    ArtifactRow(
                        f"source-bundle:{parent_id}",
                        "source_bundle",
                        parent_id,
                        str(raw_dir / f"{parent_id}.json"),
                        "1" * 64,
                        "projected",
                        payload_json=json.dumps(bundle),
                    ),
                    ArtifactRow(
                        f"facts:{child_id}",
                        "facts",
                        parent_id,
                        str(facts_dir / f"{child_id}.jsonl"),
                        "2" * 64,
                        "projected",
                        person_id=child_id,
                        # input_fingerprint intentionally omitted: this legacy
                        # record predates that field, like every legacy record
                        # on a real install (see selection._stored_legacy_fingerprint).
                        payload_json=json.dumps(child_record),
                    ),
                    FactRow(
                        child_id,
                        parent_id,
                        f"facts:{child_id}",
                        child_id,
                        "yes",
                        "legacy",
                        0.5,
                        facts_json=json.dumps(child_record["facts"]),
                    ),
                )
            )

            bundles = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=False,
            )

            self.assertEqual([bundle.person_id for bundle in bundles], [parent_id])

    def test_legacy_child_facts_with_a_real_stored_fingerprint_still_skip(self) -> None:
        """Complements the test above: when a legacy child artifact DOES carry
        a real recorded fingerprint that matches the current bundle, the
        parent still gets its earned fast-path skip — the fix removes the
        fabricated match, not legitimate reuse.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir, facts_dir = root / "raw", root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            database = Db(root / "deep-context.sqlite")
            parent_id, child_id = "parent-legacy", "person-legacy"
            bundle = {
                "person_id": parent_id,
                "messages": [message_payload("unchanged message")],
            }
            real_fingerprint = self.fingerprint(bundle)
            child_record = {
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "input_evidence_fingerprint": real_fingerprint,
                "facts": {"network_worth": {"decision": "yes", "reason": "legacy"}},
            }
            database.project_rows(
                (
                    ParentRow(parent_id, parent_id, "Jordan Bravo"),
                    PersonRow(child_id, parent_id),
                    ArtifactRow(
                        f"source-bundle:{parent_id}",
                        "source_bundle",
                        parent_id,
                        str(raw_dir / f"{parent_id}.json"),
                        "1" * 64,
                        "projected",
                        payload_json=json.dumps(bundle),
                    ),
                    ArtifactRow(
                        f"facts:{child_id}",
                        "facts",
                        parent_id,
                        str(facts_dir / f"{child_id}.jsonl"),
                        "2" * 64,
                        "projected",
                        person_id=child_id,
                        input_fingerprint=real_fingerprint,
                        payload_json=json.dumps(child_record),
                    ),
                    FactRow(
                        child_id,
                        parent_id,
                        f"facts:{child_id}",
                        child_id,
                        "yes",
                        "legacy",
                        0.5,
                        facts_json=json.dumps(child_record["facts"]),
                    ),
                )
            )

            bundles = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=False,
            )

            self.assertEqual(bundles, [])

    def test_model_changed_forces_full_replan(self) -> None:
        """A --model/--reasoning-effort switch must not silently keep serving
        facts a different model produced — model_changed is the gate
        SynthesizePersonContext computes from the stage's own manifest.json
        (see _model_or_effort_changed) and threads into selection.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir, facts_dir = root / "raw", root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            database = Db(root / "deep-context.sqlite")
            parent_id, child_id = "parent-one", "person-one"
            bundle = {"person_id": parent_id, "messages": [message_payload("hi")]}
            record = {
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "input_evidence_fingerprint": self.fingerprint(bundle),
                "facts": {"network_worth": {"decision": "yes", "reason": "pinned"}},
            }
            database.project_rows(
                (
                    ParentRow(parent_id, parent_id),
                    PersonRow(child_id, parent_id),
                    ArtifactRow(
                        f"source-bundle:{parent_id}",
                        "source_bundle",
                        parent_id,
                        str(raw_dir / f"{parent_id}.json"),
                        "1" * 64,
                        "projected",
                        payload_json=json.dumps(bundle),
                    ),
                    ArtifactRow(
                        f"facts:{parent_id}",
                        "facts",
                        parent_id,
                        str(facts_dir / f"{parent_id}.jsonl"),
                        "0" * 64,
                        "projected",
                        input_fingerprint=record["input_evidence_fingerprint"],
                        payload_json=json.dumps(record),
                    ),
                )
            )

            unchanged = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=False,
            )
            after_model_swap = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=False,
                model_changed=True,
            )

            self.assertEqual(unchanged, [])
            self.assertEqual([bundle.person_id for bundle in after_model_swap], [parent_id])

    def test_model_or_effort_changed_reads_the_stage_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir, facts_dir = root / "raw", root / "facts"
            raw_dir.mkdir()
            facts_dir.mkdir()
            database = Db(root / "deep-context.sqlite")
            node = SynthesizePersonContext(
                db=database,
                raw_dir=raw_dir,
                out_dir=facts_dir,
                model="gpt-5.2",
                reasoning_effort="medium",
            )
            # No manifest yet (first run): nothing to compare against.
            self.assertFalse(node._model_or_effort_changed())

            (facts_dir / "manifest.json").write_text(
                json.dumps({"model": "gpt-5.2", "reasoning_effort": "medium"}),
                encoding="utf-8",
            )
            self.assertFalse(node._model_or_effort_changed())

            (facts_dir / "manifest.json").write_text(
                json.dumps({"model": "gpt-5.1", "reasoning_effort": "medium"}),
                encoding="utf-8",
            )
            self.assertTrue(node._model_or_effort_changed())

            (facts_dir / "manifest.json").write_text(
                json.dumps({"model": "gpt-5.2", "reasoning_effort": "high"}),
                encoding="utf-8",
            )
            self.assertTrue(node._model_or_effort_changed())

    def test_selection_skips_owner_only_parent_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Db(Path(directory) / "deep-context.sqlite")
            owner_bundle = {
                "person_id": "parent-owner-only",
                "messages": [message_payload("owner cache")],
            }
            mixed_bundle = {
                "person_id": "parent-mixed",
                "messages": [message_payload("family cache")],
            }
            database.project_rows(
                (
                    ParentRow("parent-owner-only", "parent-owner-only"),
                    PersonRow("owner-only", "parent-owner-only", is_owner=1),
                    ArtifactRow(
                        "source-bundle:parent-owner-only",
                        "source_bundle",
                        "parent-owner-only",
                        "raw/parent-owner-only.json",
                        "1" * 64,
                        "projected",
                        payload_json=json.dumps(owner_bundle),
                    ),
                    ParentRow("parent-mixed", "parent-mixed"),
                    PersonRow("owner-member", "parent-mixed", is_owner=1),
                    PersonRow("family-member", "parent-mixed"),
                    ArtifactRow(
                        "source-bundle:parent-mixed",
                        "source_bundle",
                        "parent-mixed",
                        "raw/parent-mixed.json",
                        "2" * 64,
                        "projected",
                        payload_json=json.dumps(mixed_bundle),
                    ),
                )
            )

            bundles = selection.pending_target_bundles(
                database,
                system_prompt=prompting.SYSTEM_PROMPT,
                chunk_chars=9000,
                max_batches=20,
                force=True,
            )

            self.assertEqual(
                [bundle.person_id for bundle in bundles],
                ["parent-mixed"],
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
                "messages": [message_payload("cached message")],
            }
            record = {
                "facts": {"network_worth": {"decision": "yes", "reason": "cached"}},
                "final_confidence": 0.9,
            }
            raw_path = raw_dir / f"{child_id}.json"
            fact_path = facts_dir / f"{child_id}.jsonl"
            raw_path.write_text(json.dumps(bundle), encoding="utf-8")
            fact_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows(
                (
                    OwnerContextRow(
                        "owner",
                        json.dumps({"name": "Mailbox Owner"}),
                        str(root / "owner.json"),
                        "0" * 64,
                    ),
                    ParentRow(parent_id, "parent-one"),
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
                    ArtifactRow(
                        f"facts:{child_id}",
                        "facts",
                        parent_id,
                        str(fact_path),
                        "2" * 64,
                        "projected",
                        person_id=child_id,
                        payload_json=json.dumps(record),
                    ),
                    FactRow(
                        child_id,
                        parent_id,
                        f"facts:{child_id}",
                        child_id,
                        "yes",
                        "cached",
                        0.9,
                        facts_json=json.dumps(record["facts"]),
                    ),
                )
            )
            before = [dict(row) for row in database.query("SELECT * FROM artifacts ORDER BY artifact_key")]

            payload = SynthesizePersonContext(
                db=database,
                raw_dir=raw_dir,
                out_dir=facts_dir,
            ).estimate()

            self.assertEqual(payload["status"], "dry_run")
            # This legacy child FACTS artifact carries no input_fingerprint (it
            # predates that field, like every legacy record on a real install),
            # so nothing on disk proves its facts match the CURRENT bundle above
            # — selection correctly reports it pending rather than a fabricated
            # skip. See selection._stored_legacy_fingerprint.
            self.assertEqual(payload["people"], 1)
            self.assertEqual(
                [dict(row) for row in database.query("SELECT * FROM artifacts ORDER BY artifact_key")],
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
                "messages": [message_payload("new cached message")],
            }
            raw_path = raw_dir / f"{child_id}.json"
            raw_path.write_text(json.dumps(bundle), encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows(
                (
                    OwnerContextRow(
                        "owner",
                        json.dumps({"name": "Mailbox Owner"}),
                        str(root / "owner.json"),
                        "0" * 64,
                    ),
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
                )
            )
            before = [dict(row) for row in database.query("SELECT * FROM artifacts ORDER BY artifact_key")]

            stage = SynthesizePersonContext(
                db=database,
                raw_dir=raw_dir,
                out_dir=facts_dir,
            )
            payload = stage.estimate()

            self.assertEqual(payload["people"], 1)
            self.assertEqual(
                [dict(row) for row in database.query("SELECT * FROM artifacts ORDER BY artifact_key")],
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
            "source_channels": ["gmail_msgvault"],
            "groups": ["Founders"],
            "thread_participants": [
                {"subject": "Launch", "participants": ["me@example.com", "jordan@example.com"]},
            ],
        }
        message = message_payload(
            "Ready to ship.",
            channel="gmail",
            at="2026-01-02T03:04:05Z",
            subject="Launch",
        )
        expected = (
            "CONTACT: Jordan Bravo\n"
            "Known emails: jordan@example.com\n"
            "Known phones: +15550100\n"
            "Channels: gmail_msgvault\n"
            "Shared group chats (names only): Founders\n\n"
            "EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):\n"
            "- Launch — me@example.com, jordan@example.com\n\n"
            "MESSAGES (most relevant, chronological):\n"
            "[gmail 2026-01-02 THEM] Launch: Ready to ship."
        )
        person_row = CollectionBundle.from_payload(person)
        message_row = MessageEntry.from_payload(message)
        self.assertIsNotNone(person_row)
        self.assertIsNotNone(message_row)
        self.assertEqual(prompting.render_chunk(person_row, [message_row]), expected)

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
                ("person-a", "no", "no", "gmail_msgvault"),
                ("person-b", None, "maybe", "imessage"),
            ):
                bundle = {
                    "person_id": person_id,
                    "full_name": "Jordan Bravo",
                    "emails": [f"{person_id}@example.test"] if channel == "gmail_msgvault" else [],
                    "phones": ["+15550100"] if channel == "imessage" else [],
                    "source_channels": [channel],
                    "messages": [
                        message_payload(
                            person_id,
                            channel="gmail" if channel == "gmail_msgvault" else channel,
                            at="2026-01-01",
                        )
                    ],
                    "messages_available": 1,
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
                rows.extend(
                    (
                        ArtifactRow(
                            f"source-bundle:{person_id}",
                            "source_bundle",
                            "parent-1",
                            str(raw_path),
                            person_id * 4,
                            "projected",
                            person_id=person_id,
                            payload_json=json.dumps(bundle),
                        ),
                        ArtifactRow(
                            f"facts:{person_id}",
                            "facts",
                            "parent-1",
                            str(fact_path),
                            (machine_worth or "unjudged") * 16,
                            "projected",
                            person_id=person_id,
                            payload_json=json.dumps(record),
                        ),
                        FactRow(
                            person_id,
                            "parent-1",
                            f"facts:{person_id}",
                            person_id,
                            machine_worth,
                            person_id,
                            0.8,
                            facts_json=json.dumps(fact_payload),
                        ),
                    )
                )
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
            parent_artifact = database.query("SELECT payload_json FROM artifacts WHERE artifact_key='facts:parent-1'")[
                0
            ]
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
            database.project_rows(
                (
                    ParentRow("parent-1", "parent-worth:parent-1"),
                    PersonRow("person-a", "parent-1"),
                    ArtifactRow(
                        "facts:person-a",
                        "facts",
                        "parent-1",
                        str(path),
                        "1" * 64,
                        "projected",
                        person_id="person-a",
                        payload_json=json.dumps(record),
                    ),
                    FactRow(
                        "person-a",
                        "parent-1",
                        "facts:person-a",
                        "person-a",
                        "maybe",
                        "cached",
                        facts_json=json.dumps(record["facts"]),
                    ),
                )
            )

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
                "source_channels": ["gmail_msgvault"],
                "messages": [
                    message_payload(
                        "Ready.",
                        channel="gmail",
                        at="2026-01-02T03:04:05Z",
                        subject="Launch",
                    )
                ],
            }
            (raw_dir / "parent-1.json").write_text(json.dumps(bundle), encoding="utf-8")
            database = Db(root / "deep-context.sqlite")
            database.project_rows(
                (
                    OwnerContextRow(
                        "owner",
                        json.dumps({"name": "Mailbox Owner"}),
                        str(root / "owner.json"),
                        "0" * 64,
                    ),
                    ParentRow("parent-1", "parent-1"),
                    PersonRow("person-1", "parent-1"),
                    ArtifactRow(
                        "source-bundle:parent-1",
                        "source_bundle",
                        "parent-1",
                        str(raw_dir / "parent-1.json"),
                        "1" * 64,
                        "projected",
                        payload_json=json.dumps(bundle),
                    ),
                )
            )
            facts = {
                "canonical_name": "Jordan Bravo",
                "relationship_category": "work",
                "confidence": 0.91,
                "network_worth": {"decision": "yes", "reason": "Real correspondence"},
            }
            usage = {"input_tokens": 12, "output_tokens": 3, "reasoning_tokens": 4}
            response = SimpleNamespace(
                output_text=json.dumps(facts),
                usage=SimpleNamespace(
                    input_tokens=12,
                    output_tokens=3,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=4),
                ),
            )
            node = SynthesizePersonContext(
                db=database,
                raw_dir=raw_dir,
                out_dir=facts_dir,
                concurrency=1,
            )
            plan = node._plan()

            with (
                mock.patch.object(
                    openai_responses,
                    "AsyncOpenAI",
                    return_value=_FakeClient(response),
                ),
            ):
                payload = node.run()

            expected_fingerprint = prompting.input_evidence_fingerprint(
                CollectionBundle.from_payload(bundle),
                system_prompt=plan.system_prompt,
                chunk_chars=9000,
                max_batches=20,
            )
            record = {
                "chunk_index": 0,
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "input_evidence_fingerprint": expected_fingerprint,
                "facts": facts,
                "usage": usage,
                "batches_used": 1,
                "batches_total": 1,
                "messages_used": 1,
                "messages_available": 1,
                "final_confidence": 0.91,
                "stop_reason": "completed",
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
            artifact = database.query("SELECT input_fingerprint FROM artifacts WHERE artifact_key='facts:parent-1'")[0]
            self.assertEqual(artifact["input_fingerprint"], expected_fingerprint)


if __name__ == "__main__":
    unittest.main()
