"""Synthetic bundles, actual SQLite schema, and an in-process fake provider."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageChannel, MessageDirection, MessageEntry, ThreadParticipants
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context_v2.synthesize import facts, prompt, synthesize


def payload(**changes):
    value = dict(canonical_name="Jordan Bravo", aliases=["Jordan"], employers=[dict(name="Synthetic Labs", role="Engineer", status="current")], title="Engineer", school="", field_of_study="", location="", relationship_to_owner="colleague", relationship_category="work", topics=["robotics project planning"], notable_events=[dict(date="2026-01", summary="started robotics project")], identifiers=["jordan@example.com"], owned_identifiers=dict(emails=["jordan@example.com"], phones=[], urls=[]), shared_context=[dict(overlap="employer", detail="worked at Synthetic Labs", evidence="our project")], confidence=0.8, is_owner=False)
    value.update(changes)
    return value


def message(text="synthetic hello", at="2026-01-01", direction=MessageDirection.FROM_THEM):
    return MessageEntry(MessageChannel.GMAIL, at, direction, "Synthetic subject", text)


def bundle(candidate="candidate:email:jordan@example.com", messages=None):
    return CollectionBundle(candidate, "Jordan Bravo", ("jordan@example.com",), (), ("gmail",), ("Synthetic group",), (ThreadParticipants("Synthetic project", ("owner@example.com", "jordan@example.com")),), tuple(messages if messages is not None else [message()]), 1, False)


class FactsTests(unittest.TestCase):

    def test_roundtrip_all_fields(self):
        value = payload()
        parsed = facts.SynthesizedFacts.from_payload(value)
        self.assertEqual(parsed.to_payload(), value)
        self.assertIsInstance(parsed.employers[0], facts.EmployerFact)
        self.assertIsInstance(parsed.notable_events[0], facts.NotableEvent)
        self.assertIsInstance(parsed.shared_context[0], facts.SharedContextFact)


    def test_collapsed_output_json_roundtrips_as_consumer_facts(self):
        newest = facts.SynthesizedFacts.from_payload(payload())
        older = facts.SynthesizedFacts.from_payload(payload(title="Senior engineer", confidence=1.5, is_owner=True))
        collapsed = facts.collapse([newest, older])
        emitted = collapsed.to_payload()
        Draft202012Validator(prompt.FACT_SCHEMA).validate(emitted)
        consumed = facts.SynthesizedFacts.from_payload(json.loads(json.dumps(emitted, allow_nan=False)))
        self.assertEqual(consumed, collapsed)
        self.assertEqual(consumed.confidence, 1.5)  # No confidence-range policy in the schema.


    def test_valid_internal_string_tuples_emit_json_lists_unchanged(self):
        value = payload(aliases=["Jordan", "J Bravo"], topics=["robotics"], identifiers=["jordan@example.com"], owned_identifiers=dict(emails=["jordan@example.com"], phones=["+15550100"], urls=["https://example.com/jordan"]))
        valid = facts.SynthesizedFacts.from_payload(value)
        for field in ("aliases", "topics", "identifiers"):
            self.assertIsInstance(getattr(valid, field), tuple)
        for field in ("emails", "phones", "urls"):
            self.assertIsInstance(getattr(valid.owned_identifiers, field), tuple)
        emitted = valid.to_payload()
        self.assertEqual(emitted, value)
        self.assertEqual(facts.SynthesizedFacts.from_payload(json.loads(json.dumps(emitted, allow_nan=False))), valid)


    def test_empty_lists_and_unknown_category_are_valid(self):
        value = payload(canonical_name="", aliases=[], employers=[], topics=[], notable_events=[], shared_context=[], identifiers=[], relationship_category="unknown")
        self.assertEqual(facts.SynthesizedFacts.from_payload(value).to_payload(), value)

    def test_collapse_majorities_newest_employer_and_owner(self):
        newest = facts.SynthesizedFacts.from_payload(payload(aliases=[" Jordan ", "Jordan Bravo"], identifiers=[" X@example.com ", ""], confidence=0.4))
        older = facts.SynthesizedFacts.from_payload(payload(canonical_name="J Bravo", aliases=["J"], employers=[dict(name="synthetic labs", role="Intern", status="past"), dict(name="Old Labs", role="Intern", status="past")], title="Senior engineer", school="Synthetic College", location="Synthetic City", field_of_study="Robotics", relationship_category="personal", relationship_to_owner="a longstanding project colleague", identifiers=["x@example.com"], owned_identifiers=dict(emails=["JORDAN@example.com", "other@example.com"], phones=["+15550100"], urls=["https://example.com"]), confidence=0.9, is_owner=True))
        result = facts.collapse([newest, older, replace(older, canonical_name="Jordan Bravo", relationship_category="work")])
        self.assertEqual(result.canonical_name, "Jordan Bravo")
        self.assertEqual(result.relationship_category, "work")
        self.assertEqual(result.employers[0], newest.employers[0])
        self.assertEqual(len(result.employers), 2)
        self.assertEqual(result.aliases, ("Jordan", "J"))
        self.assertEqual(result.identifiers, ("X@example.com",))
        self.assertEqual(result.owned_identifiers.emails, ("jordan@example.com", "other@example.com"))
        self.assertEqual(result.title, "Senior engineer")
        self.assertEqual(result.relationship_to_owner, "a longstanding project colleague")
        self.assertTrue(result.is_owner)
        self.assertEqual(result.confidence, 0.9)
        self.assertEqual(len(result.topics), 1)
        self.assertEqual(len(result.shared_context), 1)
        self.assertEqual(len(result.notable_events), 1)

    def test_collapse_blank_scalars_lists_and_event_sorting(self):
        first = facts.SynthesizedFacts.from_payload(payload(canonical_name=" ", aliases=[" "], employers=[dict(name=" ", role="", status="unknown")], title="", school="", location="", topics=[" "], notable_events=[dict(date="", summary="undated event"), dict(date="2025", summary="early event")], shared_context=[dict(overlap="school", detail=" ", evidence="")], relationship_to_owner="", relationship_category="unknown"))
        result = facts.collapse([first])
        self.assertEqual(result.canonical_name, "")
        self.assertEqual(result.title, "")
        self.assertEqual(result.employers, ())
        self.assertEqual(result.topics, ())
        self.assertEqual(result.shared_context, ())
        self.assertEqual([e.date for e in result.notable_events], ["2025", ""])

    def test_nearduplicate_longest_survives_and_distinct_order(self):
        values = [("alpha beta gamma delta", 1), ("alpha beta gamma delta epsilon", 2), ("unrelated completely separate words", 3)]
        self.assertEqual(facts._collapse_near_duplicates(values), [values[1], values[2]])
        self.assertEqual(facts._unique([" A ", "a", "", "B"]), ("A", "B"))


class PromptTests(unittest.TestCase):
    def test_batches_newest_stable_ties_cap_and_oversize(self):
        values = [message("a" * 4, "2025"), message("b" * 4, "2026"), message("c" * 4, "2026"), message("d" * 20, "2027")]
        self.assertEqual(prompt.batches(values, chunk_chars=8, max_batches=2), [[values[3]], [values[1], values[2]]])
        self.assertEqual(prompt.batches([], chunk_chars=8, max_batches=2), [])
        self.assertEqual(prompt.batches([message("", "")], chunk_chars=8, max_batches=2)[0][0].text, "")

    def test_render_directions_threads_and_empty_fallbacks(self):
        rendered = prompt.render_batch(bundle(), [message(direction=d) for d in MessageDirection])
        for label in ("ME", "THEM", "OTHER-IN-GROUP"):
            self.assertIn(f"2026-01-01 {label}] Synthetic subject", rendered)
        self.assertIn("Shared group chats (names only): Synthetic group", rendered)
        self.assertIn("owner@example.com, jordan@example.com", rendered)
        empty = replace(bundle(), full_name="", emails=(), source_channels=(), groups=(), thread_participants=())
        self.assertIn("CONTACT: (unknown)", prompt.render_batch(empty, []))
        self.assertIn("Known phones: (none)", prompt.render_batch(empty, []))

    def test_thread_limit_and_owner_block(self):
        threads = tuple(ThreadParticipants(f"thread-{i}", ()) for i in range(30))
        rendered = prompt.render_batch(replace(bundle(), thread_participants=threads), [])
        self.assertIn("thread-24", rendered)
        self.assertNotIn("thread-25", rendered)
        self.assertEqual(prompt.owner_identity_block("", ()), "")
        self.assertIn("unknown email", prompt.owner_identity_block("Casey Synthetic", ()))
        self.assertIn("owner@example.com", prompt.owner_identity_block("", ("owner@example.com",)))

    def test_fingerprint_tracks_paid_inputs_not_concurrency(self):
        config = OpenAIResponsesConfig("synthetic", "medium", 2, 10, 1)
        base = synthesize.input_fingerprint(("batch",), "system", config)
        self.assertEqual(base, synthesize.input_fingerprint(("batch",), "system", replace(config, concurrency=3)))
        for prompts, system, changed in [(("other",), "system", config), (("batch",), "other", config), (("batch",), "system", replace(config, model="other")), (("batch",), "system", replace(config, effort="high"))]:
            self.assertNotEqual(base, synthesize.input_fingerprint(prompts, system, changed))


class SynthesizeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.conn = open_store(store_path(self.root))
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO owner VALUES ('owner', ?, 'owner-key', '2026')", (json.dumps(dict(name="Casey Synthetic", emails=["owner@example.com"])),))
        self.config = OpenAIResponsesConfig("synthetic", "medium", 2, 10, 1)
        patcher = patch.object(synthesize.OpenAIResponsesConfig, "resolve", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.responses = {}
        test = self
        class FakeCaller:
            def __init__(self, config):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                pass
            async def call(self, **kwargs):
                test.calls.append(kwargs)
                response = test.responses.get(kwargs["user_prompt"], payload())
                if isinstance(response, Exception):
                    raise response
                return copy.deepcopy(response)
        patcher = patch.object(synthesize, "OpenAIResponsesCaller", FakeCaller)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, value):
        self.conn.execute("INSERT INTO candidates VALUES (?, ?, 0, '{}', '2026')", (value.person_id, value.full_name))
        self.conn.execute("INSERT INTO bundles VALUES (?, ?, 'bundle-key', '2026')", (value.person_id, json.dumps(value.to_payload())))
        self.conn.commit()

    def node(self, limit=10):
        return synthesize.Synthesize(self.conn, self.root, limit=limit)

    def test_run_persists_manifest_facts_and_reuses_paid_key(self):
        self.seed(bundle())
        node = self.node()
        manifest = node.run()
        self.assertEqual(manifest.counts, dict(work=1, facts_written=1, failed=0))
        row = self.conn.execute("SELECT * FROM facts").fetchone()
        persisted = json.loads(row["facts_json"])
        Draft202012Validator(prompt.FACT_SCHEMA).validate(persisted)
        consumed = facts.SynthesizedFacts.from_payload(persisted)
        self.assertEqual(facts.SynthesizedFacts.from_payload(json.loads(json.dumps(consumed.to_payload(), allow_nan=False))), consumed)
        self.assertEqual(consumed.to_payload(), payload())
        self.assertEqual(row["model"], "synthetic")
        self.assertEqual(node.work(), [])
        self.assertEqual(node.run().counts["facts_written"], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["schema"], prompt.FACT_SCHEMA)
        self.assertIn("Casey Synthetic", self.calls[0]["system_prompt"])
        saved = json.loads((self.root / "deep-context/v2-manifests/synthesize.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertNotEqual(row["input_fingerprint"], synthesize.input_fingerprint(("changed",), node.prompt, self.config))

    def test_partial_failure_keeps_success_and_resume_only_failed(self):
        self.seed(bundle())
        other = replace(bundle("candidate:email:other@example.com"), full_name="Other Synthetic")
        self.seed(other)
        node = self.node()
        self.responses[synthesize.batch_prompts(other)[0]] = RuntimeError("synthetic failure")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "1 of 2 candidates failed; 1 written"):
            node.run()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 1)
        self.assertEqual(len(node.work()), 1)
        saved = json.loads((self.root / "deep-context/v2-manifests/synthesize.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.responses.clear()
        self.assertEqual(node.run().counts["facts_written"], 1)
        self.assertEqual(len(self.calls), 3)


    def test_multi_batch_reaches_collapse(self):
        self.seed(bundle(messages=[message("a" * 9001, "2026"), message("old", "2025")]))
        node = self.node()
        prompts = node.work()[0].prompts
        self.assertEqual(len(prompts), 2)
        self.responses[prompts[1]] = payload(title="Senior", confidence=0.9)
        self.assertEqual(node.run().counts["facts_written"], 1)
        result = json.loads(self.conn.execute("SELECT facts_json FROM facts").fetchone()[0])
        Draft202012Validator(prompt.FACT_SCHEMA).validate(result)
        consumed = facts.SynthesizedFacts.from_payload(result)
        self.assertEqual(consumed.to_payload(), result)
        self.assertEqual(facts.SynthesizedFacts.from_payload(json.loads(json.dumps(consumed.to_payload(), allow_nan=False))), consumed)
        self.assertEqual(result["title"], "Senior")
        self.assertEqual(len(self.calls), 2)

    def test_empty_store_and_limit_zero(self):
        self.assertEqual(self.node().run().counts, dict(work=0, facts_written=0, failed=0))
        self.seed(bundle())
        self.assertEqual(self.node(limit=0).run().counts["work"], 0)
        self.assertEqual(self.calls, [])

    def test_dry_estimate_uses_bounded_work_without_provider(self):
        self.seed(bundle())
        node = self.node()
        encoder = type("Encoder", (), {"encode": lambda self, value: list(value)})()
        with patch.object(synthesize.tiktoken, "get_encoding", return_value=encoder), patch.object(synthesize, "estimate_cost_usd", return_value=0.01):
            result = node.estimate()
        self.assertEqual((result["bundles"], result["pending"], result["calls"], result["work"]), (1, 1, 1, 1))
        self.assertEqual(result["input_tokens"], len(node.prompt + node.work()[0].prompts[0]))
        self.assertEqual(result["output_tokens_assumed"], 750)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "deep-context/v2-manifests").exists())

    def test_cli_paid_path_uses_real_store_and_manifest(self):
        self.seed(bundle())
        output = io.StringIO()
        with redirect_stdout(output):
            code = synthesize.main(["--data-root", str(self.root), "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertIn("completed work=1 facts_written=1 failed=0", output.getvalue())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_cli_dry_run_returns_json_without_manifest(self):
        self.seed(bundle())
        encoder = type("Encoder", (), {"encode": lambda self, value: [1]})()
        output = io.StringIO()
        with patch.object(synthesize.tiktoken, "get_encoding", return_value=encoder), redirect_stdout(output):
            code = synthesize.main(["--data-root", str(self.root), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["calls"], 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "deep-context/v2-manifests").exists())

    def test_empty_message_bundle_fails_without_provider_or_facts(self):
        self.seed(bundle(messages=[]))
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "1 of 1 candidates failed"):
            self.node().run()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
