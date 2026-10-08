"""Deterministic worth requests, model decisions and SQLite persistence."""
import asyncio
import contextlib
import io
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageEntry, MessageChannel, MessageDirection
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_from_payload
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts, ChannelCount, Connection
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, OwnedIdentifiers, EmployerFact
from packs.ingestion.primitives.deep_context_v2.worth import evidence, jev
from packs.ingestion.primitives.deep_context_v2.worth.pre_match import pre_match
from packs.ingestion.primitives.deep_context_v2.worth import reason as reason_module
from packs.ingestion.primitives.deep_context_v2.worth.worth import Worth, PRE_MATCHED_REASON, main


def facts(name="Jordan Bravo"):
    return SynthesizedFacts(name, (), (), "", "", "", "", "", "unknown", (), (), (), OwnedIdentifiers(("private@example.com",), (), ()), (), 0.5, False)


def member(candidate="a", name="Jordan Bravo", at="2026-01-01"):
    return MemberFacts(candidate, "p:family", json.dumps(facts(name).to_payload()), at)


def raw_answers():
    """All shipped model inputs, with neutral probabilities and a complete option vocabulary."""
    rows = {}
    for feature in jev.MODEL.features:
        question, _, option = feature.split(":", 1)[1].partition("=")
        if not option:
            rows[question] = {"type": "noul", "noul": 0.5}
        else:
            row = rows.setdefault(question, {"type": "score" if option.isdigit() else "choice", "probabilities": {}})
            row["probabilities"][option] = 0.5
    for row in rows.values():
        if "probabilities" in row:
            size = len(row["probabilities"])
            row["probabilities"] = dict.fromkeys(row["probabilities"], 1 / size)
    return rows


class EvidenceTests(unittest.TestCase):
    def test_names_unique_nonempty_and_reference_date(self):
        members = [member("a"), member("b", at="2026-03-04T00:00:00Z"), member("c")]
        self.assertEqual(evidence.family_names(members, {"a": "Jordan Bravo", "b": "Jordan Bravo", "c": ""}), ["Jordan Bravo"])
        self.assertEqual(evidence.reference_date(members), "2026-03-04")
        self.assertEqual(evidence.reference_date([]), "")

    def test_key_member_order_invariant_not_fact_fingerprint(self):
        a, b = member("a"), member("b")
        self.assertEqual(evidence.family_key([a, b]), evidence.family_key([b, a]))
        self.assertEqual(evidence.family_key([a]), evidence.family_key([dataclasses.replace(a, facts_json="{}", synthesized_at="later")]))
        self.assertNotEqual(evidence.family_key([a]), evidence.family_key([a, b]))

    def test_single_and_collapsed_facts(self):
        self.assertEqual(evidence.family_facts([member()]).canonical_name, "Jordan Bravo")
        self.assertEqual(evidence.family_facts([member("a"), member("b")]).canonical_name, "Jordan Bravo")

    def test_counts_include_other_direction_but_not_as_from_them(self):
        summary = evidence.channel_summary([member("a"), member("b")], {"a": ["imessage"], "b": ["gmail_msgvault"]}, {
            "a": [ChannelCount("a", "imessage", "from_me", 2, "2026-02-01", "2026-02-02"), ChannelCount("a", "imessage", "from_other", 3, None, None)],
            "b": [ChannelCount("b", "gmail", "from_them", 4, "2026-01-01", "2026-03-01")]}, {"a": 1, "b": 2})
        self.assertEqual(summary.sources, ("gmail_msgvault", "imessage"))
        self.assertEqual(summary.interaction_counts, {"imessage": 5, "gmail": 4})
        self.assertEqual((summary.from_me, summary.from_them, summary.group_count), (2, 4, 3))
        self.assertEqual((summary.first_message_at, summary.last_message_at), ("2026-01-01", "2026-03-01"))
        empty = evidence.channel_summary([member()], {"a": []}, {}, {"a": 0})
        self.assertIsNone(empty.first_message_at)

    def test_pre_match_requires_every_name_unique_connection_and_blank_matches_none(self):
        connections = [Connection("li:one", "Jordan Bravo", "CEO"), Connection("li:two", "Casey Delta", "")]
        self.assertEqual(pre_match(["Jordan Bravo", "Jordan Bravo"], connections), connections[:1])
        for names in ([], [""], ["Jordan Bravo", "Casey Delta"]):
            self.assertEqual(pre_match(names, connections), [])
        self.assertEqual(len(pre_match(["Jordan Bravo"], connections + connections[:1])), 2)


class JevTests(unittest.TestCase):
    def test_request_strips_owned_identifiers_pins_date_and_copies_policy(self):
        original = json.dumps(jev.WORTH_QUESTIONS, sort_keys=True)
        owner = owner_from_payload({"name": "Casey Owner"})
        email = jev.ChannelSummary(("gmail_msgvault",), {"gmail": 5}, None, None, 1, 4, 0)
        request = jev.build_request(facts(), email, owner, "2026-01-01")
        rendered = json.dumps(request)
        self.assertNotIn("private@example.com", rendered)
        self.assertIn("2026-01-01", rendered)
        self.assertIn("Casey Owner", rendered)
        self.assertIn("email", request["questions"]["worth"]["instructions"].lower())
        phone = dataclasses.replace(email, sources=("imessage",))
        self.assertIn("phone", jev.build_request(facts(), phone, owner, "2026-01-01")["questions"]["worth"]["instructions"].lower())
        mixed = dataclasses.replace(email, sources=("gmail_msgvault", "imessage"))
        self.assertIn("both", jev.build_request(facts(), mixed, owner, "2026-01-01")["questions"]["worth"]["instructions"].lower())
        self.assertEqual(json.dumps(jev.WORTH_QUESTIONS, sort_keys=True), original)

    def test_request_owner_history_and_current_employer(self):
        detailed = dataclasses.replace(facts(), employers=(EmployerFact("Past Co", "", "past"), EmployerFact("Current Co", "Engineer", "current")))
        owner = owner_from_payload({"name": "Casey Owner", "work": [{"company": "Owner Co", "title": "Engineer", "start": "2020", "end": ""}], "education": [{"school": "Synthetic School", "start": "2015", "end": "2019", "note": "degree"}]})
        summary = jev.ChannelSummary((), {}, None, None, 0, 0, 0)
        request = jev.build_request(detailed, summary, owner, "2026-01-01")
        for text in ("Current Co", "Owner Co", "Synthetic School"):
            self.assertIn(text, json.dumps(request))

    def test_unknown_probability_and_duplicate_supporting_phrases(self):
        self.assertIn("little indication", reason_module._phrase("relationship_kind", "unknown", 0.2))
        self.assertIn("some uncertainty", reason_module._phrase("relationship_kind", "unknown", 0.5))
        self.assertIn("a work connection", reason_module._phrase("relationship_kind", "business_contact", 0.8))
        self.assertIn("engineering work", reason_module._phrase("function", "engineering", 0.8))
        support = [("is_professional", "", 0.8), ("work_signal", "", 0.8), ("relationship_kind", "unknown", 0.5)]
        with patch.object(reason_module, "_supporting", return_value=support):
            text = reason_module.reason({}, "yes")
        self.assertEqual(text.count("work-related contact"), 1)
        self.assertIn("less clear", text)

    def test_answer_types_features_labels_ties_and_expected_score(self):
        answers = jev.parse_answers({"is_family": {"type": "noul", "noul": 0.8}, "worth": {"type": "choice", "probabilities": {"yes": 0.5, "no": 0.5}}, "warmth": {"type": "score", "probabilities": {"1": 0.3, "3": 0.7}}})
        self.assertEqual(jev.features(answers)["worth:worth=yes"], 0.5)
        self.assertEqual(jev.features(answers)["tag:is_family"], 0.8)
        self.assertEqual(jev.labels(answers), {"is_family": 0.8, "worth": "yes", "worth_p": 0.5, "warmth": 2.4})
        self.assertEqual(jev.best_index([1, 1, 0], -1), 0)
        self.assertEqual(jev.best_index([1, 1, 0], 0), 1)

    def test_model_scores_match_direct_arithmetic_and_reason(self):
        answers = jev.parse_answers(raw_answers())
        normalized, scores = jev.class_scores(answers)
        self.assertEqual(len(normalized), len(jev.MODEL.features))
        expected = [intercept + sum(value * weight for value, weight in zip(normalized, weights)) for intercept, weights in zip(jev.MODEL.intercept, jev.MODEL.coefficients)]
        for actual, direct in zip(scores, expected):
            self.assertAlmostEqual(actual, direct)
        decision = jev.predict(answers)
        self.assertEqual(decision, jev.MODEL.classes[scores.index(max(scores))])
        text = reason_module.reason(answers, decision)
        self.assertTrue(text)
        self.assertNotIn("worth=", text)

    def test_negative_model_decision_with_noise(self):
        raw = raw_answers()
        for name in ("noise", "transactional_only", "is_automated_sender", "is_stranger"):
            if name in raw:
                raw[name]["noul"] = 1.0
        for name in ("real_relationship", "professional_standing", "is_family", "is_close_friend"):
            if name in raw:
                raw[name]["noul"] = 0.0
        raw["worth"]["probabilities"] = {"yes": 0.0, "maybe": 0.0, "no": 1.0}
        self.assertEqual(jev.predict(jev.parse_answers(raw)), "no")

    def test_malformed_kind_and_missing_type_rejected(self):
        with self.assertRaises(ValueError):
            jev.parse_answers({"worth": {"type": "invalid"}})
        with self.assertRaises(KeyError):
            jev.parse_answers({"worth": {}})


    def test_missing_model_feature_fails_instead_of_predicting_default(self):
        with self.assertRaises(KeyError):
            jev.predict({})

    def test_answer_all_parses_provider_boundary(self):
        from types import SimpleNamespace
        fake = AsyncMock(return_value={"digest": SimpleNamespace(response={"answers": {"is_family": {"type": "noul", "noul": 0.8}}})})
        with patch("packs.ingestion.primitives.deep_context_v2.worth.jev.answer_requests", fake):
            result = asyncio.run(jev.answer_all({"digest": {}}, Path("unused")))
        self.assertEqual(result["digest"]["is_family"].noul, 0.8)

    def test_reason_certainty_unknown_and_grouping(self):
        self.assertEqual(reason_module._phrase("is_family", "", 0.8), "a family connection")
        self.assertIn("little indication", reason_module._phrase("is_family", "", 0.2))
        self.assertIn("uncertain", reason_module._phrase("is_family", "", 0.5))
        self.assertIn("unclear", reason_module._phrase("relationship_kind", "unknown", 0.8))
        self.assertIn("inner-circle", reason_module._phrase("warmth", "4", 1.0))
        self.assertEqual(reason_module._joined(["a", "b", "c"], " and "), "a, b and c")
        with patch.object(reason_module, "_supporting", return_value=[]):
            self.assertIn("no clear explanation", reason_module.reason({}, "yes"))
        with patch.object(reason_module, "_supporting", return_value=[("is_family", "", 0.8), ("is_close_friend", "", 0.5), ("noise", "", 0.2)]):
            text = reason_module.reason({}, "yes")
        self.assertIn("Looks like", text)
        self.assertIn("less clear", text)
        self.assertIn("little evidence", text)


class WorthStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO owner VALUES ('owner', ?, 'owner-fp', '2026-01-01')", (json.dumps({"name": "Casey Owner"}),))

    def add(self, candidate, name="Jordan Bravo", parent="p:family", with_facts=True):
        self.conn.execute("INSERT INTO candidates VALUES (?, ?, 0, '{}', '2026-01-01')", (candidate, name))
        self.conn.execute("INSERT INTO candidate_sources VALUES (?, 'imessage')", (candidate,))
        self.conn.execute("INSERT INTO candidate_parent (candidate_id,parent_id,reason,created_at) VALUES (?, ?, 'singleton', '2026-01-01')", (candidate, parent))
        if with_facts:
            self.conn.execute("INSERT INTO facts VALUES (?, ?, ?, 'fake', 'low', '2026-01-01')", (candidate, json.dumps(facts(name).to_payload()), candidate + "fp"))
            messages = (MessageEntry(MessageChannel.IMESSAGE, "", MessageDirection.FROM_ME, "", "synthetic message"), MessageEntry(MessageChannel.IMESSAGE, "2026-01-02", MessageDirection.FROM_THEM, "", "reply"))
            bundle = CollectionBundle(candidate, name, (), (), ("imessage",), ("synthetic group",), (), messages, 10, True)
            self.conn.execute("INSERT INTO bundles VALUES (?, ?, 'bundle-fp', '2026-01-01')", (candidate, json.dumps(bundle.to_payload())))
        self.conn.commit()

    def connection(self, name="Jordan Bravo", position="CEO", url="https://www.linkedin.com/in/synthetic-jordan"):
        self.conn.execute("INSERT INTO connections VALUES (?, ?, NULL, ?, 'Synthetic Co', '2026-01-01')", (url, name, position))
        self.conn.commit()

    def node(self, limit=100):
        return Worth(self.conn, self.root, limit=limit)

    def boundary(self):
        async def answer(requests, **kwargs):
            return {key: type("Response", (), {"response": {"answers": raw_answers()}})() for key in requests}
        return patch("packs.ingestion.primitives.deep_context_v2.worth.jev.answer_requests", new=AsyncMock(side_effect=answer))

    def test_estimate_free_pending_cached_and_judged_counts(self):
        import tiktoken
        from packs.search.primitives.llm_rerank_candidates.jev.client import cache_path
        encoder = tiktoken.Encoding(name="synthetic-byte", pat_str=r"(?s).", mergeable_ranks={bytes([i]): i for i in range(256)}, special_tokens={})
        self.add("a", parent="p:free")
        self.add("b", name="Casey Delta", parent="p:paid")
        self.connection()
        node = self.node()
        families = node.families()
        paid = next(family for family in families if family.match is None)
        with patch("tiktoken.get_encoding", return_value=encoder):
            estimate = node.estimate()
        self.assertEqual((estimate["families"], estimate["members"], estimate["pending"], estimate["pre_matched_yes"], estimate["notable_positions"], estimate["jev_calls"], estimate["jev_cached"]), (2, 2, 2, 1, 1, 1, 0))
        self.assertGreater(estimate["jev_input_tokens"], 0)
        cached = cache_path(node.cache_dir, paid.digest)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text("{}")  # estimate checks existence, not response content
        with patch("tiktoken.get_encoding", return_value=encoder):
            estimate = node.estimate()
        self.assertEqual((estimate["jev_cached"], estimate["jev_input_tokens"]), (1, 0))
        self.assertFalse((self.root / "deep-context/v2-manifests/worth.json").exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth").fetchone()[0], 0)
        with self.boundary():
            self.node().run()
        with patch("tiktoken.get_encoding", return_value=encoder):
            estimate = self.node().estimate()
        self.assertEqual((estimate["fresh"], estimate["pending"], estimate["jev_calls"]), (2, 0, 0))

    def test_cli_dry_run_then_real_completed_run(self):
        import tiktoken
        encoder = tiktoken.Encoding(name="synthetic-byte", pat_str=r"(?s).", mergeable_ranks={bytes([i]): i for i in range(256)}, special_tokens={})
        self.add("a")
        self.connection(position="Engineer")
        target = open_store(store_path(self.root))
        self.conn.backup(target)
        target.close()
        args = ["--data-root", str(self.root), "--limit", "1"]
        with patch("tiktoken.get_encoding", return_value=encoder), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args + ["--dry-run"]), 0)
        estimate = json.loads(output.getvalue())
        self.assertEqual((estimate["pre_matched_yes"], estimate["notable_positions"]), (1, 0))
        self.assertFalse((self.root / "deep-context/v2-manifests/worth.json").exists())
        with self.boundary(), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 0)
        self.assertIn("completed", output.getvalue())
        target = open_store(store_path(self.root))
        try:
            self.assertEqual(target.execute("SELECT worth FROM worth").fetchone()[0], "yes")
        finally:
            target.close()

    def test_empty_store_run(self):
        with self.boundary() as external:
            result = self.node().run()
        self.assertEqual(result.counts, {"families": 0, "pre_matched_yes": 0, "jev_requests": 0, "worth_rows": 0})
        self.assertEqual(external.call_args.args[0], {})

    def test_prematched_notable_yes_rows_and_rerun_no_requests(self):
        self.add("a")
        self.add("b", "Jordan Bravo")
        self.add("missing", with_facts=False)
        self.connection()
        with self.boundary() as external:
            result = self.node().run()
            again = self.node().run()
        self.assertEqual(result.counts, {"families": 1, "pre_matched_yes": 1, "jev_requests": 0, "worth_rows": 2})
        self.assertEqual(again.counts["worth_rows"], 0)
        rows = self.conn.execute("SELECT * FROM worth").fetchall()
        self.assertTrue(all(row["worth"] == "yes" and row["reason"] == "Notable position: CEO" for row in rows))
        self.assertEqual(external.call_args.args[0], {})
        self.assertEqual(json.loads((self.root / "deep-context/v2-manifests/worth.json").read_text())["status"], "completed")

    def test_ordinary_position_free_yes_and_vice_president_not_notable(self):
        self.add("a")
        self.connection(position="Vice President")
        with self.boundary():
            self.node().run()
        self.assertEqual(self.conn.execute("SELECT reason FROM worth").fetchone()[0], PRE_MATCHED_REASON)

    def test_ambiguous_connection_uses_model_one_request_for_family(self):
        self.add("a")
        self.add("b")
        self.connection()
        self.connection(url="https://www.linkedin.com/in/another-synthetic")
        with self.boundary() as external:
            result = self.node().run()
        self.assertEqual(result.counts["jev_requests"], 1)
        self.assertEqual(result.counts["worth_rows"], 2)
        self.assertEqual(len(external.call_args.args[0]), 1)
        rows = self.conn.execute("SELECT worth, labels_json, reason FROM worth").fetchall()
        self.assertEqual(tuple(rows[0]), tuple(rows[1]))
        self.assertTrue(json.loads(rows[0]["labels_json"]))

    def test_new_member_makes_new_key_new_facts_do_not(self):
        self.add("a")
        with self.boundary():
            self.node().run()
        old = self.node().families()[0]
        self.conn.execute("UPDATE facts SET input_fingerprint='changed'")
        self.conn.commit()
        self.assertEqual(self.node().pending(self.node().families()), [])
        self.add("b")
        new = self.node().families()[0]
        self.assertNotEqual(new.fingerprint, old.fingerprint)
        self.assertFalse(new.judged)

    def test_collected_counts_not_messages_available_and_limit(self):
        self.add("a", parent="p:first")
        self.add("b", name="Casey Delta", parent="p:second")
        node = self.node(limit=1)
        families = node.families()
        self.assertEqual(len(families), 2)
        self.assertEqual(len(node.pending(families)), 1)
        self.assertIn('"imessage": 2', json.dumps(families[0].request))
        with self.boundary():
            self.assertEqual(node.run().counts["worth_rows"], 1)

    def test_negative_provider_decision_persists_real_model_reason(self):
        self.add("a")
        raw = raw_answers()
        raw["worth"]["probabilities"] = {"yes": 0.0, "maybe": 0.0, "no": 1.0}
        for name in ("noise", "transactional_only", "is_automated_sender", "is_stranger"):
            if name in raw:
                raw[name]["noul"] = 1.0
        for name in ("real_relationship", "professional_standing", "is_family", "is_close_friend"):
            if name in raw:
                raw[name]["noul"] = 0.0
        from types import SimpleNamespace
        async def answer(requests, **kwargs):
            return {key: SimpleNamespace(response={"answers": raw}) for key in requests}
        with patch("packs.ingestion.primitives.deep_context_v2.worth.jev.answer_requests", new=AsyncMock(side_effect=answer)):
            self.node().run()
        row = self.conn.execute("SELECT worth, reason FROM worth").fetchone()
        self.assertEqual(row["worth"], "no")
        self.assertTrue(row["reason"])

    def test_failed_provider_rolls_back_worth_and_records_failed_manifest(self):
        self.add("a")
        with patch("packs.ingestion.primitives.deep_context_v2.worth.jev.answer_requests", new=AsyncMock(side_effect=RuntimeError("synthetic provider failure"))):
            with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
                self.node().run()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth").fetchone()[0], 0)
        self.assertEqual(json.loads((self.root / "deep-context/v2-manifests/worth.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
