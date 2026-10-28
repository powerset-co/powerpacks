"""Deterministic worth requests, model decisions and SQLite persistence."""
import contextlib
import io
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from packs.ingestion.primitives.deep_context_v2 import decisions

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageEntry, MessageChannel, MessageDirection
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_from_payload
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts, ChannelCount, Connection
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, OwnedIdentifiers, EmployerFact
from packs.ingestion.primitives.deep_context_v2.worth import evidence, luna
from packs.ingestion.primitives.deep_context_v2.worth.pre_match import pre_match
from packs.ingestion.primitives.deep_context_v2.worth import reason as reason_module
from packs.ingestion.primitives.deep_context_v2.worth.worth import Worth, PRE_MATCHED_REASON, main


def facts(name="Jordan Bravo"):
    return SynthesizedFacts(name, (), (), "", "", "", "", "", "unknown", (), (), (), OwnedIdentifiers(("private@example.com",), (), ()), (), 0.5, False)


def member(candidate="a", name="Jordan Bravo", at="2026-01-01"):
    return MemberFacts(candidate, "p:family", json.dumps(facts(name).to_payload()), at)


def raw_answers(decision="yes"):
    """Current Decisions response: choice probabilities plus worth's explanatory predicates."""
    return [{"type": "choice", "name": "worth", "choice": decision, "confidence": 0.9,
             "probabilities": [{"value": value, "probability": float(value == decision)}
                               for value in ("yes", "maybe", "no")]},
            {"type": "predicate", "name": "real_relationship", "probability": 0.8 if decision == "yes" else 0.1},
            {"type": "predicate", "name": "noise", "probability": 0.8 if decision == "no" else 0.1},
            {"type": "score", "name": "warmth", "score": 2.44}]


def bundle():
    return CollectionBundle("a", "Jordan Bravo", (), (), ("imessage",), (), (),
                            (MessageEntry(MessageChannel.IMESSAGE, "2026-01-01", MessageDirection.FROM_THEM,
                                          "", "Synthetic reply"),), 1, False)


class EvidenceTests(unittest.TestCase):
    def test_names_unique_nonempty_and_reference_date(self):
        members = [member("a"), member("b", at="2026-03-04T00:00:00Z"), member("c")]
        self.assertEqual(evidence.family_names(members, {"a": "Jordan Bravo", "b": "Jordan Bravo", "c": ""}), ["Jordan Bravo"])
        self.assertEqual(evidence.reference_date(members), "2026-03-04")
        self.assertEqual(evidence.reference_date([]), "")

    def test_version_key_tracks_question_version(self):
        original = evidence.version_key()
        self.assertEqual(original, evidence.version_key())
        with patch.object(luna, "REQUEST_VERSION", "synthetic-next-version"):
            self.assertNotEqual(original, evidence.version_key())

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


class LunaTests(unittest.TestCase):
    def test_request_uses_raw_messages_pins_date_and_copies_policy(self):
        original = json.dumps(luna.WORTH_QUESTIONS, sort_keys=True)
        owner = owner_from_payload({"name": "Casey Owner"})
        email = luna.ChannelSummary(("gmail_msgvault",), {"gmail": 5}, None, None, 1, 4, 0)
        request = luna.build_request(facts(), [bundle()], email, owner, "2026-01-01")
        state = json.loads(request["input"])
        self.assertNotIn("private@example.com", json.dumps(request))
        self.assertEqual(state["reference_date"], "2026-01-01")
        self.assertEqual(state["owner"]["name"], "Casey Owner")
        self.assertIn("Synthetic reply", state["messages"])
        for sources, expected in ((("gmail_msgvault",), "email"), (("imessage",), "phone"),
                                  (("gmail_msgvault", "imessage"), "both")):
            summary = dataclasses.replace(email, sources=sources)
            asked = luna.build_request(facts(), [bundle()], summary, owner, "2026-01-01")
            worth = next(q for q in asked["questions"] if q["name"] == "worth")
            self.assertIn(expected, worth["instructions"].lower())
        self.assertEqual(json.dumps(luna.WORTH_QUESTIONS, sort_keys=True), original)

    def test_request_owner_history_and_current_employer(self):
        detailed = dataclasses.replace(facts(), employers=(EmployerFact("Past Co", "", "past"),
                                                          EmployerFact("Current Co", "Engineer", "current")))
        owner = owner_from_payload({"name": "Casey Owner", "work": [{"company": "Owner Co", "title": "Engineer", "start": "2020", "end": ""}], "education": [{"school": "Synthetic School", "start": "2015", "end": "2019", "note": "degree"}]})
        summary = luna.ChannelSummary((), {}, None, None, 0, 0, 0)
        state = json.loads(luna.build_request(detailed, [bundle()], summary, owner, "2026-01-01")["input"])
        self.assertEqual(state["profile"]["company"], "Current Co")
        for text in ("Owner Co", "Synthetic School"):
            self.assertIn(text, json.dumps(state["owner"]))

    def test_answer_labels_use_choice_and_expected_score(self):
        answers = raw_answers() + [{"type": "refusal", "name": "is_minor"}]
        self.assertEqual(luna.labels(answers), {"worth": "yes", "worth_p": 1.0,
                                               "real_relationship": 0.8, "noise": 0.1, "warmth": 2.44})
        self.assertFalse(luna.refused(answers))
        self.assertTrue(luna.refused([{"type": "refusal", "name": "worth"}]))
        self.assertTrue(luna.refused([{"type": "refusal", "name": "warmth"}]))

    def test_reason_uses_worth_signals_and_absence(self):
        text = reason_module.reason(luna.labels(raw_answers()), "yes")
        self.assertIn("direct correspondence", text)
        self.assertIn("little evidence", text)
        self.assertIn("unsolicited outreach", reason_module.reason(luna.labels(raw_answers("no")), "no"))
        self.assertIn("no clear explanation", reason_module.reason({}, "yes"))
        self.assertEqual(reason_module._joined(["a", "b", "c"], " and "), "a, b and c")


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

    @contextlib.contextmanager
    def boundary(self, decision="yes", error=False):
        real = decisions.AsyncOpenAI
        sent = []
        def handler(request):
            if error:
                raise RuntimeError("synthetic provider failure")
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"answers": raw_answers(decision)})
        def client(**options):
            return real(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with patch.object(decisions, "AsyncOpenAI", client), patch.dict("os.environ", {
            "OPENAI_API_KEY": "synthetic", "POWERPACKS_USAGE_LOG": str(self.root / "usage.jsonl")
        }):
            yield sent

    def test_estimate_pending_cached_and_reasked_counts(self):
        self.add("a", parent="p:free")
        self.add("b", name="Casey Delta", parent="p:paid")
        self.connection()
        node = self.node()
        estimate = node.estimate()
        self.assertEqual((estimate["families"], estimate["members"], estimate["pending"],
                          estimate["pre_matched_yes"], estimate["notable_positions"], estimate["requests"],
                          estimate["cached"]), (2, 2, 2, 1, 1, 2, 0))
        self.assertGreater(estimate["input_tokens"], 0)
        self.assertFalse((self.root / "deep-context/v2-manifests/worth.json").exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth").fetchone()[0], 0)
        with self.boundary() as sent:
            node.run()
        self.assertEqual(len(sent), 2)
        cached = self.node().estimate()
        self.assertEqual((cached["pending"], cached["cached"], cached["input_tokens"]), (2, 2, 0))
        with self.boundary() as sent:
            again = self.node().run()
        self.assertEqual(sent, [])
        self.assertEqual(again.counts["worth_rows"], 2)

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
        self.assertEqual(result.counts, {"families": 0, "pre_matched_yes": 0, "refused": 0, "requests": 0, "worth_rows": 0})
        self.assertEqual(external, [])

    def test_prematched_notable_yes_rows_and_rerun_no_requests(self):
        self.add("a")
        self.add("b", "Jordan Bravo")
        self.add("missing", with_facts=False)
        self.connection()
        with self.boundary() as external:
            result = self.node().run()
            again = self.node().run()
        self.assertEqual(result.counts, {"families": 1, "pre_matched_yes": 1, "refused": 0, "requests": 1, "worth_rows": 2})
        self.assertEqual(again.counts["worth_rows"], 2)
        rows = self.conn.execute("SELECT * FROM worth").fetchall()
        self.assertTrue(all(row["worth"] == "yes" and row["reason"] == "Notable position: CEO" for row in rows))
        self.assertEqual(len(external), 1)
        self.assertEqual(json.loads((self.root / "deep-context/v2-manifests/worth.json").read_text())["status"], "completed")

    def test_ordinary_position_prematched_yes_and_vice_president_not_notable(self):
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
        self.assertEqual(result.counts["requests"], 1)
        self.assertEqual(result.counts["worth_rows"], 2)
        self.assertEqual(len(external), 1)
        rows = self.conn.execute("SELECT worth, labels_json, reason FROM worth").fetchall()
        self.assertEqual(tuple(rows[0]), tuple(rows[1]))
        self.assertTrue(json.loads(rows[0]["labels_json"]))

    def test_new_member_and_changed_facts_change_exact_request_digest(self):
        self.add("a")
        with self.boundary():
            self.node().run()
        before = self.node()
        original = before.estimate()
        self.assertEqual(original["cached"], 1)
        old = before.request_digests["p:family"]
        self.conn.execute("UPDATE facts SET facts_json=?", (json.dumps(dataclasses.replace(facts(), title="Engineer").to_payload()),))
        self.conn.commit()
        changed = self.node()
        self.assertEqual(changed.estimate()["cached"], 0)
        self.assertNotEqual(changed.request_digests["p:family"], old)
        self.add("b")
        expanded = self.node()
        self.assertEqual(expanded.estimate()["members"], 2)
        self.assertNotEqual(expanded.request_digests["p:family"], changed.request_digests["p:family"])

    def test_collected_counts_not_messages_available_and_limit(self):
        self.add("a", parent="p:first")
        self.add("b", name="Casey Delta", parent="p:second")
        node = self.node(limit=1)
        families = node.families()
        self.assertEqual(len(families), 2)
        self.assertEqual(len(node.pending(families)), 1)
        self.assertEqual(json.loads(node._request(families[0])["input"])["channels"]["interaction_counts"], {"imessage": 2})
        with self.boundary():
            self.assertEqual(node.run().counts["worth_rows"], 1)

    def test_negative_provider_decision_persists_real_reason(self):
        self.add("a")
        with self.boundary("no") as sent:
            self.node().run()
        self.assertEqual(len(sent), 1)
        row = self.conn.execute("SELECT worth, reason FROM worth").fetchone()
        self.assertEqual(row["worth"], "no")
        self.assertIn("unsolicited outreach", row["reason"])

    def test_failed_provider_rolls_back_worth_and_records_failed_manifest(self):
        from openai import APIConnectionError
        self.add("a")
        with self.boundary(error=True):
            with self.assertRaises(APIConnectionError):
                self.node().run()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth").fetchone()[0], 0)
        self.assertEqual(json.loads((self.root / "deep-context/v2-manifests/worth.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
