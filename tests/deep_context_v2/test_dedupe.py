"""Tiny graph fixtures exercise the real store and node; only the provider is fake."""
import contextlib
import io
import json
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jsonschema

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageEntry, MessageChannel, MessageDirection
from packs.ingestion.primitives.deep_context_v2.db import queries_dedupe as queries
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe import blocking, judge
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import Dedupe, mint, main
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, OwnedIdentifiers, EmployerFact


def facts(name="Jordan Bravo"):
    return SynthesizedFacts(name, (), (), "", "", "", "", "", "unknown", (), (), (), OwnedIdentifiers((), (), ()), (), 0.5, False)


class BlockingTests(unittest.TestCase):
    def test_full_initial_and_blank_names(self):
        self.assertEqual(blocking.name_keys("  Jordan  Bravo "), {"fnli:jordan|b", "filn:j|bravo", "words:bravo jordan", "ends:jordan|bravo"})
        self.assertEqual(blocking.name_keys("J Bravo"), {"fnli:j|b", "filn:j|bravo"})
        for name in ("", "   ", "Jordan", "123"):
            self.assertEqual(blocking.name_keys(name), set())

    def test_pairs_unique_sorted_email_local_and_stats(self):
        result = blocking.block(["c", "b", "a"], {"a": "Jordan Bravo", "b": "Jordan Bravo", "c": ""},
                                {"a": [Identifier("email", "same@example.com")], "b": [], "c": [Identifier("email", "same@other.example")]})
        self.assertEqual(result.pairs, [("a", "b"), ("a", "c")])
        self.assertEqual(result.bucketed, 3)
        self.assertEqual(result.stats["local"], blocking.KindStats(1, 2.0, 2, 0))

    def test_oversize_bucket_skipped_at_exact_threshold(self):
        for size, expected in ((3, 3), (4, 0)):
            ids = [str(i) for i in range(size)]
            with patch.object(blocking, "MAX_BUCKET", 3):
                result = blocking.block(ids, dict.fromkeys(ids, "Jordan Bravo"), {i: [] for i in ids})
            self.assertEqual(len(result.pairs), expected)
            self.assertEqual(result.stats["words"].members_in_skipped, size if size == 4 else 0)

    def test_phone_and_single_word_do_not_block(self):
        result = blocking.block(["a", "b"], {"a": "Jordan", "b": "Jordan"}, {i: [Identifier("phone", "+15550100")] for i in ("a", "b")})
        self.assertEqual(result.pairs, [])
        self.assertEqual(result.bucketed, 0)


class JudgeTests(unittest.TestCase):
    def test_decisions_and_no_evidence_are_uncertain(self):
        for decision, expected in (("same", 1), ("different", 0), ("uncertain", None)):
            for evidence in ("same personal phone", "  "):
                result = judge.decision_from_answer(dict(decision=decision, confidence=0.8, reason="Synthetic explanation", identity_evidence=evidence))
                self.assertEqual(result.same_person, expected if evidence.strip() else None)
                self.assertEqual("Individual identity evidence" in result.reason, bool(evidence.strip()))

    def test_malformed_answer_rejected(self):
        valid = dict(decision="same", confidence=0.8, reason="Evidence", identity_evidence="phone")
        for answer in ({}, {**valid, "decision": "yes"}, {**valid, "confidence": 2}, {**valid, "extra": 1}, {**valid, "reason": ""}):
            with self.subTest(answer=answer), self.assertRaises(jsonschema.ValidationError):
                judge.decision_from_answer(answer)


    def test_samples_newest_nonblank_direction_and_truncated(self):
        messages = tuple(MessageEntry(MessageChannel.GMAIL, str(i), MessageDirection.FROM_ME, "", "x" * 210) for i in range(6))
        messages += (MessageEntry(MessageChannel.GMAIL, "9", MessageDirection.FROM_ME, "", "  "),)
        self.assertEqual(judge._sample(messages, MessageDirection.FROM_ME), ["x" * 200] * 4)
        self.assertEqual(judge._sample(messages, MessageDirection.FROM_THEM), [])

    def test_prompt_and_side_roundtrip(self):
        bundle = CollectionBundle("a", "Jordan Bravo", ("jordan@example.com",), (), (), (), (), (), 0, False)
        side = judge.side_of("J Bravo", [Identifier("email", "jordan@example.com")], json.dumps(facts().to_payload()), json.dumps(bundle.to_payload()))
        prompt = judge.user_prompt("Casey Owner", side, side)
        for text in ("Casey Owner", "Jordan Bravo", "J Bravo", "jordan@example.com"):
            self.assertIn(text, prompt)

    def test_prompt_includes_all_fact_lines_and_phone(self):
        detailed = dataclasses.replace(facts(), aliases=("J Bravo",), employers=(EmployerFact("Synthetic Co", "Engineer", "current"),), title="Engineer", school="Synthetic University", location="Test City", relationship_to_owner="coworker", topics=("synthetic topic",))
        side = judge.SolSide("Jordan Bravo", (Identifier("phone", "+15550100"),), detailed, ())
        prompt = judge.user_prompt("Casey Owner", side, side)
        for text in ("also known as: J Bravo", "Synthetic Co", "Engineer", "Synthetic University", "Test City", "coworker", "synthetic topic", "+15550100"):
            self.assertIn(text, prompt)

    def test_prompt_without_name_or_title_skips_empty_employer(self):
        detailed = dataclasses.replace(facts(""), employers=(EmployerFact("", "", "unknown"), EmployerFact("Synthetic Co", "", "past")))
        side = judge.SolSide("", (), detailed, ())
        prompt = judge.user_prompt("Casey Owner", side, side)
        self.assertIn("Synthetic Co", prompt)
        self.assertNotIn("dossier name:", prompt)
        title_only = dataclasses.replace(side, facts=dataclasses.replace(detailed, title="Engineer", employers=()))
        self.assertIn("work: Engineer", judge.user_prompt("Casey Owner", title_only, title_only))

    def test_provider_request_validates_real_answer(self):
        import asyncio
        caller = AsyncMock()
        caller.call.return_value = dict(decision="different", confidence=0.9, reason="Two people", identity_evidence="distinct phones")
        self.assertEqual(asyncio.run(judge.judge(caller, "synthetic prompt")).same_person, 0)
        self.assertEqual(caller.call.call_args.kwargs["context"], "dedupe")


class DedupeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO owner VALUES ('owner', ?, 'owner-fp', '2026-01-01')", (json.dumps({"name": "Casey Owner"}),))

    def add(self, candidate, name="Jordan Bravo", parent=None, with_facts=True, owner=False):
        self.conn.execute("INSERT INTO candidates VALUES (?, ?, ?, '{}', '2026-01-01')", (candidate, name, int(owner)))
        self.conn.execute("INSERT INTO candidate_identifiers VALUES (?, 'email', ?, ?)", (candidate, candidate + "@example.com", candidate + "@example.com"))
        if with_facts:
            self.conn.execute("INSERT INTO facts VALUES (?, ?, ?, 'fake', 'low', '2026-01-01')", (candidate, json.dumps(facts(name).to_payload()), candidate + "fp"))
            bundle = CollectionBundle(candidate, name, (), (), (), (), (), (), 0, False)
            self.conn.execute("INSERT INTO bundles VALUES (?, ?, 'bundle-fp', '2026-01-01')", (candidate, json.dumps(bundle.to_payload())))
        if parent:
            queries.append_parent_rows(self.conn, [(candidate, parent, "human", None, "2026-01-01")])
        self.conn.commit()

    def node(self, limit=100):
        return Dedupe(self.conn, self.root, limit=limit)

    def save(self, a, b, verdict):
        queries.insert_pair_verdict(self.conn, (a, b, "signature", verdict, 0.9, "Synthetic identity", "2026-01-01"))
        self.conn.commit()

    def test_estimate_reports_bucket_funnel_and_tokens_without_writes(self):
        import tiktoken
        encoder = tiktoken.Encoding(name="synthetic-byte", pat_str=r"(?s).", mergeable_ranks={bytes([i]): i for i in range(256)}, special_tokens={})
        self.add("a")
        self.add("b")
        node = self.node()
        with patch("tiktoken.get_encoding", return_value=encoder):
            estimate = node.estimate()
        self.assertEqual((estimate["blocked_pairs"], estimate["gated_pairs"], estimate["pairs_for_sol"]), (1, 1, 1))
        self.assertEqual(estimate["output_tokens_assumed"], 1500)
        expected = len(encoder.encode(judge.SYSTEM_PROMPT + node.prompts(node.plan().todo)[0]))
        self.assertEqual(estimate["input_tokens"], expected)
        self.assertEqual(estimate["buckets"]["words"]["max_size"], 2)
        self.assertGreater(estimate["estimated_cost_usd"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pair_verdicts").fetchone()[0], 0)
        self.assertEqual(queries.current_parents(self.conn), {})
        self.assertFalse((self.root / "deep-context/v2-manifests/dedupe.json").exists())

    def test_cli_dry_run_then_real_completed_run(self):
        import tiktoken
        encoder = tiktoken.Encoding(name="synthetic-byte", pat_str=r"(?s).", mergeable_ranks={bytes([i]): i for i in range(256)}, special_tokens={})
        self.add("a")
        target = open_store(store_path(self.root))
        self.conn.backup(target)
        target.close()
        args = ["--data-root", str(self.root), "--limit", "0"]
        with patch("tiktoken.get_encoding", return_value=encoder), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args + ["--dry-run"]), 0)
        self.assertEqual(json.loads(output.getvalue())["pairs_for_sol"], 0)
        self.assertFalse((self.root / "deep-context/v2-manifests/dedupe.json").exists())
        with patch("packs.ingestion.primitives.deep_context_v2.dedupe.dedupe.OpenAIResponsesCaller", return_value=AsyncMock()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 0)
        self.assertIn("completed", output.getvalue())
        target = open_store(store_path(self.root))
        try:
            self.assertEqual(len(queries.current_parents(target)), 1)
        finally:
            target.close()

    def test_different_and_uncertain_are_singletons_not_merges(self):
        for candidate in "abc":
            self.add(candidate)
        self.save("a", "b", 0)
        self.save("a", "c", None)
        self.save("b", "c", None)
        with patch("packs.ingestion.primitives.deep_context_v2.dedupe.dedupe.OpenAIResponsesCaller", return_value=AsyncMock()):
            result = self.node().run()
        self.assertEqual(result.counts["components_merged"], 0)
        self.assertEqual(result.counts["singletons"], 3)
        self.assertEqual(len(set(queries.current_parents(self.conn).values())), 3)

    def test_internal_different_verdict_does_not_block_family_absorption(self):
        self.add("a", parent="p:existing")
        self.add("b", parent="p:existing")
        self.add("c")
        self.save("a", "b", 0)
        self.save("a", "c", 1)
        node = self.node()
        rows, counts = node.merge(node.current)
        self.assertEqual([row[:2] for row in rows], [("c", "p:existing")])
        self.assertEqual(counts["components_with_different"], 0)

    def test_verdict_reference_skips_pair_outside_component(self):
        for candidate in "ab":
            self.add(candidate, parent="p:separate")
        for candidate in "cd":
            self.add(candidate)
        self.save("a", "b", 1)
        self.save("c", "d", 1)
        node = self.node()
        rows, counts = node.merge(node.current)
        self.assertEqual({row[0] for row in rows}, {"c", "d"})
        self.assertTrue(all(row[3] == "pair_verdicts:c|d|signature" for row in rows))
        self.assertEqual(counts["components_merged"], 1)

    def test_empty_run_and_minted_id(self):
        client = AsyncMock()
        with patch("packs.ingestion.primitives.deep_context_v2.openai.AsyncOpenAI", return_value=client):
            result = self.node().run()
        client.responses.create.assert_not_awaited()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.counts["singletons"], 0)
        self.assertRegex(mint(), r"^p:[0-9a-f]{16}$")

    def test_plan_counts_joined_judged_and_limit(self):
        for candidate in "abcd":
            self.add(candidate, parent="p:existing" if candidate in "ab" else None)
        self.save("a", "c", None)
        plan = self.node(limit=1).plan()
        self.assertEqual((len(plan.blocking.pairs), plan.gated, plan.already_joined, plan.already_judged, len(plan.todo)), (6, 6, 1, 1, 1))
        self.assertEqual(len(plan.todo[0][2]), 64)

    def test_gate_rejects_same_initial_different_names(self):
        self.add("a", "Jordan Bravo")
        self.add("b", "Jamie Bravo")
        plan = self.node().plan()
        self.assertEqual(len(plan.blocking.pairs), 1)
        self.assertEqual(plan.gated, 0)

    def test_transitive_absorb_entire_family_to_linkedin(self):
        for candidate, parent in (("a", "p:old"), ("b", "p:old"), ("c", "li:synthetic"), ("d", None)):
            self.add(candidate, parent=parent)
        self.save("a", "c", 1)
        self.save("b", "d", 1)
        node = self.node()
        rows, counts = node.merge(node.current)
        self.assertEqual({row[0]: row[1] for row in rows}, {"a": "li:synthetic", "b": "li:synthetic", "d": "li:synthetic"})
        self.assertEqual(counts["components_merged"], 1)
        queries.append_parent_rows(self.conn, rows)
        next_node = self.node()
        self.assertEqual(next_node.merge(next_node.current)[0], [])

    def test_transitive_different_blocks_whole_component(self):
        for candidate in "abc":
            self.add(candidate)
        for a, b, decision in (("a", "b", 1), ("b", "c", 1), ("a", "c", 0)):
            self.save(a, b, decision)
        node = self.node()
        rows, counts = node.merge(node.current)
        self.assertEqual(rows, [])
        self.assertEqual(counts["components_with_different"], 1)

    def test_two_linkedins_refuse_merge(self):
        self.add("a", parent="li:first")
        self.add("b", parent="li:second")
        self.save("a", "b", 1)
        node = self.node()
        rows, counts = node.merge(node.current)
        self.assertEqual(rows, [])
        self.assertEqual(counts["components_two_linkedins"], 1)

    def test_lowest_existing_parent_wins(self):
        self.add("a", parent="p:z")
        self.add("b", parent="p:a")
        self.save("a", "b", 1)
        node = self.node()
        self.assertEqual(node.merge(node.current)[0][0][:2], ("a", "p:a"))

    def test_run_persists_verdict_merge_manifest_and_skips_rerun(self):
        self.add("a")
        self.add("b")
        self.add("owner", "Casey Owner", with_facts=False, owner=True)
        caller = AsyncMock()
        caller.__aenter__.return_value.call.return_value = dict(decision="same", confidence=0.9, reason="Same person", identity_evidence="personal phone")
        with patch("packs.ingestion.primitives.deep_context_v2.dedupe.dedupe.OpenAIResponsesCaller", return_value=caller):
            result = self.node().run()
            again = self.node().run()
        self.assertEqual(result.counts["judged"], 1)
        self.assertEqual(result.counts["sol_same_rows"], 2)
        self.assertEqual(result.counts["singletons"], 1)
        self.assertEqual(again.counts["judged"], 0)
        self.assertEqual(caller.__aenter__.return_value.call.await_count, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pair_verdicts").fetchone()[0], 1)
        parents = queries.current_parents(self.conn)
        self.assertEqual(parents["a"], parents["b"])
        self.assertNotEqual(parents["a"], parents["owner"])
        self.assertEqual(json.loads((self.root / "deep-context/v2-manifests/dedupe.json").read_text())["status"], "completed")

    def test_provider_failure_leaves_successful_verdict_no_parents(self):
        for candidate in "abc":
            self.add(candidate)
        caller = AsyncMock()
        caller.__aenter__.return_value.call.side_effect = [dict(decision="uncertain", confidence=0.2, reason="Not enough", identity_evidence=""), RuntimeError("secret prompt"), RuntimeError("secret prompt")]
        with patch("packs.ingestion.primitives.deep_context_v2.dedupe.dedupe.OpenAIResponsesCaller", return_value=caller), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "2 of 3 pairs failed"):
                self.node().run()
        self.assertNotIn("secret prompt", output.getvalue())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pair_verdicts").fetchone()[0], 1)
        self.assertEqual(queries.current_parents(self.conn), {})
        self.assertEqual(json.loads((self.root / "deep-context/v2-manifests/dedupe.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
