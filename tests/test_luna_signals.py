"""Luna's signals: the Decisions client, the labels and reason from its answers, the share call and
the share rules. Synthetic data; OpenAI is a mock transport."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from test_share_v2 import StoreTests
from packs.ingestion.primitives.deep_context_v2 import decisions
from packs.ingestion.primitives.deep_context_v2.collect.bundle import (
    CollectionBundle, MessageChannel, MessageDirection, MessageEntry, Person, ThreadParticipants,
)
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.synthesize.prompt import render_batch
from packs.ingestion.primitives.deep_context_v2.worth import evidence, luna
from packs.ingestion.primitives.deep_context_v2.worth.worth import (
    DEFAULT_LIMIT, LABELS_TOKENS_PER_QUESTION, SHARE_REQUEST_TOKENS, Worth,
)
from packs.ingestion.primitives.deep_context_v2.worth.reason import reason
from packs.ingestion.primitives.share.labels import flag, labels_from_saved, share_decision
from packs.ingestion.primitives.share.models import HumanTags, LabelRow
from packs.ingestion.primitives.share.questions import (
    CHOICE_LABELS, EVIDENCE_POLICY, NOUL_LABELS, SCORE_LABELS, label_questions,
)

ANSWERS: list[dict[str, Any]] = [
    {"type": "predicate", "name": "is_recruiter", "probability": 0.91},
    {"type": "choice", "name": "relationship_kind", "choice": "colleague", "confidence": 0.8,
     "probabilities": [{"value": "colleague", "probability": 0.82}, {"value": "friend", "probability": 0.18}]},
    {"type": "score", "name": "warmth", "score": 2.44, "confidence": 0.6,
     "probabilities": [{"value": 2, "label": "2", "probability": 0.56}, {"value": 3, "label": "3", "probability": 0.44}]},
]


def no_progress(_done: int, _total: int) -> None:
    pass


def answers_for(request: dict[str, Any]) -> list[dict[str, Any]]:
    """A synthetic Decisions response to `request`: every predicate low, every choice its first value."""
    answers: list[dict[str, Any]] = []
    for question in request["questions"]:
        name: str = question["name"]
        if question["type"] == "predicate":
            answers.append({"type": "predicate", "name": name, "probability": 0.1})
        elif question["type"] == "score":
            answers.append({"type": "score", "name": name, "score": 1.0, "confidence": 0.9, "probabilities": [
                {"value": level, "label": item["label"], "probability": float(level == 1)}
                for level, item in enumerate(question["levels"])]})
        else:
            values = [item["value"] for item in question["choices"]]
            answers.append({"type": "choice", "name": name, "choice": values[0], "confidence": 0.9, "probabilities": [
                {"value": value, "probability": 0.9 if value == values[0] else 0.05} for value in values]})
    return answers


def bundle(candidate_id: str, messages: list[MessageEntry]) -> CollectionBundle:
    return CollectionBundle.of(
        Person(candidate_id, "Jordan Bravo", (f"{candidate_id}@example.com",), (), ("gmail",)),
        messages=messages, groups=[], thread_participants=(), available=len(messages),
    )


class RequestTests(unittest.TestCase):
    def test_synthesis_default_render_is_byte_identical(self) -> None:
        messages = [
            MessageEntry.of(MessageChannel.GMAIL, "2026-10-14T12:00:00Z", from_me=True,
                            subject="Hello", text="Can we talk?"),
            MessageEntry.of(MessageChannel.IMESSAGE, "2026-10-15", from_me=False, text="Yes!"),
            MessageEntry(MessageChannel.IMESSAGE_GROUP, "", MessageDirection.FROM_OTHER, "", "See you there."),
        ]
        person = CollectionBundle.of(
            Person("c1", "Jordan Bravo", ("jordan@example.com",), ("+15550100",), ("gmail", "imessage")),
            messages=messages, groups=["Project"],
            thread_participants=(ThreadParticipants("Hello", ("owner@example.com", "jordan@example.com")),),
            available=3,
        )
        self.assertEqual(render_batch(person, messages),
                         "CONTACT: Jordan Bravo\n"
                         "Known emails: jordan@example.com\n"
                         "Known phones: +15550100\n"
                         "Channels: gmail, imessage\n"
                         "Shared group chats (names only): Project\n\n"
                         "EMAIL THREADS & WHO WAS ON THEM (from/to/cc — shared colleagues, teams, and my own address if I'm a participant):\n"
                         "- Hello — owner@example.com, jordan@example.com\n\n"
                         "MESSAGES (most relevant, chronological):\n"
                         "[gmail 2026-10-14 ME] Hello: Can we talk?\n"
                         "[imessage 2026-10-15 THEM]: Yes!\n"
                         "[imessage_group  OTHER-IN-GROUP]: See you there.")

    def test_questions_use_messages_and_define_warmth_as_reachability_today(self) -> None:
        questions = label_questions() + luna.WORTH_QUESTIONS
        text = json.dumps(questions) + EVIDENCE_POLICY
        self.assertNotIn("facts", text)
        self.assertIn("messages", text)
        warmth = next(question for question in questions if question["name"] == "warmth")
        self.assertEqual(warmth["instructions"],
                         "How readily could the owner message this person today and ask for something real "
                         "(join a company, help with a search, an intro)? Judge from how close the relationship "
                         "has been AND how recently they were in touch, as of reference_date. Two people equally "
                         "close in the past differ when one was in touch last month and the other ten years ago.")
        self.assertEqual(warmth["levels"], [
            {"label": "0", "description": "No relationship, or automated contact only."},
            {"label": "1", "description": "Distant: would need a reintroduction."},
            {"label": "2", "description": "Friendly: would answer, but it has been a while."},
            {"label": "3", "description": "Close: would answer promptly."},
            {"label": "4", "description": "Inner circle: in regular touch."},
        ])


class DecisionsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "decisions"
        self.sent: list[dict[str, Any]] = []
        real = decisions.AsyncOpenAI

        def handler(request: httpx.Request) -> httpx.Response:
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"answers": ANSWERS})

        def client(**options: Any) -> Any:
            return real(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        self.patches = [mock.patch.object(decisions, "AsyncOpenAI", client),
                        mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic",
                                                     "POWERPACKS_USAGE_LOG": str(Path(self.tmp.name) / "usage.jsonl")})]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def decide(self, requests: dict[str, dict[str, Any]], *, cache_only: bool = False) -> dict[str, Any]:
        return asyncio.run(decisions.decide_all(requests, self.cache, cache_only=cache_only, stage="test",
                                                on_progress=no_progress))

    def test_an_answer_is_paid_once_then_read_from_disk(self) -> None:
        request = {"model": decisions.MODEL, "input": "facts", "questions": []}
        digest = decisions.request_digest("v1", request)
        self.assertEqual(self.decide({digest: request}), {digest: ANSWERS})
        self.assertEqual(self.decide({digest: request}), {digest: ANSWERS})
        self.assertEqual(len(self.sent), 1)
        usage = [json.loads(line) for line in (Path(self.tmp.name) / "usage.jsonl").read_text().splitlines()]
        self.assertEqual((len(usage), usage[0]["model"], usage[0]["cost_basis"]), (1, "gpt-6-luna", "tiktoken_estimate"))

    def test_a_version_bump_asks_again(self) -> None:
        request = {"model": decisions.MODEL, "input": "facts", "questions": []}
        self.assertNotEqual(decisions.request_digest("v1", request), decisions.request_digest("v2", request))

    def test_cache_only_never_calls_openai(self) -> None:
        request = {"model": decisions.MODEL, "input": "facts", "questions": []}
        with self.assertRaisesRegex(ValueError, "not saved yet"):
            self.decide({decisions.request_digest("v1", request): request}, cache_only=True)
        self.assertEqual(self.sent, [])


class WorthRunTests(StoreTests):
    """Worth end to end on a synthetic store, with OpenAI answering through a mock transport."""

    def test_worth_asks_both_requests_and_writes_luna_labels(self) -> None:
        queries.upsert_owner(self.conn, '{"name":"Test Owner"}', "test", "2026-10-07")
        self.conn.execute("INSERT INTO candidate_sources (candidate_id, source) VALUES ('c1', 'gmail_msgvault')")
        self.conn.execute("INSERT INTO bundles (candidate_id, payload_json, content_fingerprint, collected_at) "
                          "VALUES ('c1', ?, 'test', '2026-10-07')", (json.dumps(bundle("c1", []).to_payload()),))
        self._parent("p:a", "c1")
        self._facts("c1", "Jordan Bravo")
        root = Path(self.tmp.name)
        sent: list[dict[str, Any]] = []
        real = decisions.AsyncOpenAI

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={"answers": answers_for(body)})

        def client(**options: Any) -> Any:
            return real(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        phases: list[str] = []
        node = Worth(self.conn, root, limit=DEFAULT_LIMIT)
        node.on_progress = lambda phase, done, total: phases.append(phase)
        with mock.patch.object(decisions, "AsyncOpenAI", client), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic",
                                             "POWERPACKS_USAGE_LOG": str(root / "usage.jsonl")}):
            manifest = node.run()
        self.assertEqual((manifest.status, manifest.counts["requests"], len(sent)), ("completed", 2, 2))
        self.assertEqual(set(phases), {"Reading signals", "Deciding who to share"})
        labels = json.loads(self.conn.execute("SELECT labels_json FROM worth WHERE candidate_id = 'c1' "
                                              "ORDER BY seq DESC").fetchone()[0])
        self.assertEqual((labels["worth"], labels["share_call"], labels["version"]),
                         ("yes", "share", luna.REQUEST_VERSION))

    def test_family_request_merges_member_messages_newest_first_and_prices_them(self) -> None:
        queries.upsert_owner(self.conn, '{"name":"Test Owner"}', "test", "2026-10-07")
        self._parent("p:a", "c1", "c2")
        self._facts("c1", "Jordan Bravo")
        self._facts("c2", "Jordan Bravo")
        messages = [
            MessageEntry.of(MessageChannel.GMAIL, "2026-10-01", from_me=True, text="Oldest"),
            MessageEntry.of(MessageChannel.GMAIL, "2026-10-15", from_me=False, text="Newest"),
            MessageEntry.of(MessageChannel.GMAIL, "2026-10-10", from_me=True, text="Middle"),
            MessageEntry(MessageChannel.IMESSAGE_GROUP, "2026-09-30", MessageDirection.FROM_OTHER, "", "Group"),
        ]
        for candidate_id, entries in [("c1", messages[:2]), ("c2", messages[2:])]:
            self.conn.execute("INSERT INTO candidate_sources (candidate_id, source) VALUES (?, 'gmail_msgvault')",
                              (candidate_id,))
            queries.upsert_bundle(self.conn, candidate_id, json.dumps(bundle(candidate_id, entries).to_payload()),
                                  "test", "2026-10-15")
        node = Worth(self.conn, Path(self.tmp.name), limit=DEFAULT_LIMIT)
        request = node.families()[0].request
        state = json.loads(request["input"])
        self.assertNotIn("facts", state)
        self.assertEqual(state["profile"]["name"], "Jordan Bravo")
        self.assertEqual(state["messages"].count("CONTACT:"), 1)
        self.assertIn("Known emails: c1@example.com, c2@example.com", state["messages"])
        self.assertEqual([line for line in state["messages"].splitlines() if line.startswith("[")], [
            "[gmail 2026-10-15 from Jordan Bravo to Test Owner (owner)]: Newest",
            "[gmail 2026-10-10 from Test Owner (owner) to Jordan Bravo]: Middle",
            "[gmail 2026-10-01 from Test Owner (owner) to Jordan Bravo]: Oldest",
            "[imessage_group 2026-09-30 from another group participant to Test Owner (owner) and Jordan Bravo]: Group",
        ])
        self.assertEqual((state["channels"]["from_me"], state["channels"]["from_them"]), (2, 1))
        self.assertEqual(state["owner"]["name"], "Test Owner")
        self.assertEqual(state["reference_date"], "2026-10-07")
        self.assertEqual(state["evidence_policy"], EVIDENCE_POLICY)
        with mock.patch.object(decisions, "AsyncOpenAI", side_effect=AssertionError("No API calls")):
            estimate = node.estimate()
        self.assertEqual(estimate["families"], 1)
        self.assertEqual(estimate["input_tokens"], decisions.input_tokens(request)
                         + LABELS_TOKENS_PER_QUESTION * len(request["questions"]) + SHARE_REQUEST_TOKENS)

        # The synthesis cap applies once across the family, after merging the members' messages.
        for candidate_id, days in [("c1", range(1, 22, 2)), ("c2", range(2, 23, 2))]:
            entries = [MessageEntry.of(MessageChannel.GMAIL, f"2026-09-{day:02}", from_me=True,
                                       text=f"Day {day:02} " + "x" * 8993) for day in days]
            queries.upsert_bundle(self.conn, candidate_id, json.dumps(bundle(candidate_id, entries).to_payload()),
                                  "test", "2026-10-15")
        request = Worth(self.conn, Path(self.tmp.name), limit=DEFAULT_LIMIT).families()[0].request
        state = json.loads(request["input"])
        lines = [line for line in state["messages"].splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 20)
        self.assertIn("Day 22 ", lines[0])
        self.assertIn("Day 03 ", lines[-1])
        self.assertEqual(state["channels"]["from_me"], 22)


class MergeTests(StoreTests):
    def test_a_merge_asks_nothing(self) -> None:
        # Both people were judged under today's questions; dedupe then puts c2 under p:a.
        root = Path(self.tmp.name)
        queries.upsert_owner(self.conn, '{"name":"Test Owner"}', "test", "2026-10-07")
        self.conn.executemany("INSERT INTO candidate_sources (candidate_id, source) VALUES (?, 'gmail_msgvault')",
                              [("c1",), ("c2",)])
        self.conn.executemany("INSERT INTO bundles (candidate_id, payload_json, content_fingerprint, collected_at) "
                              "VALUES (?, ?, 'test', '2026-10-07')",
                              [(candidate_id, json.dumps(bundle(candidate_id, []).to_payload()))
                               for candidate_id in ("c1", "c2")])
        self._parent("p:a", "c1")
        self._parent("p:b", "c2")
        self._facts("c1", "Jordan Bravo")
        self._facts("c2", "Casey Delta")
        self.conn.executemany(
            "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
            "VALUES (?, 'yes', 'machine', 'judged', NULL, ?, '2026-10-13')",
            [("c1", evidence.version_key()), ("c2", evidence.version_key())])
        self._parent("p:a", "c2")
        families = Worth(self.conn, root, limit=DEFAULT_LIMIT).families()
        self.assertEqual([(family.key, family.members, family.judged) for family in families],
                         [("p:a", ("c1", "c2"), True)])


class LabelsTests(unittest.TestCase):
    def test_answers_become_one_value_per_question(self) -> None:
        self.assertEqual(luna.labels(ANSWERS), {"is_recruiter": 0.91, "relationship_kind": "colleague",
                                                "relationship_kind_p": 0.82, "warmth": 2.44})

    def test_a_refused_choice_leaves_the_family_unlabelled_and_a_refused_yes_no_only_its_label(self) -> None:
        self.assertTrue(luna.refused([*ANSWERS, {"type": "refusal", "name": "relationship_kind"}]))
        declined = [*ANSWERS, {"type": "refusal", "name": "is_minor"}]
        self.assertFalse(luna.refused(declined))
        self.assertNotIn("is_minor", luna.labels(declined))

    def test_the_share_request_reads_each_label_with_its_confidence(self) -> None:
        request = luna.share_request(luna.labels(ANSWERS))
        self.assertEqual(request["input"], "is_recruiter: 0.91\nrelationship_kind: colleague (0.82)\nwarmth: 2.4 of 4")
        self.assertEqual([question["name"] for question in request["questions"]], ["share_call"])

    def test_the_reason_names_what_points_to_the_verdict(self) -> None:
        labels = {"real_relationship": 0.9, "work_signal": 0.7, "professional_standing": 0.5, "noise": 0.1,
                  "transactional_only": 0.5, "evidence_incomplete": 0.2}
        self.assertEqual(reason(labels, "yes"), "Looks like direct correspondence and work-related contact. "
                                                "There's little evidence of unsolicited outreach or broadcasts.")
        self.assertEqual(reason(labels, "maybe"), "The signals give no clear explanation to single out.")


def saved(**overrides: Any) -> dict[str, Any]:
    """A complete saved label set: everything low, a colleague, shared at 0.9."""
    cells: dict[str, Any] = {name: 0.1 for name in NOUL_LABELS}
    for name in CHOICE_LABELS:
        cells[name], cells[f"{name}_p"] = "unknown", 0.5
    for name in SCORE_LABELS:
        cells[name] = 2.0
    cells.update({"relationship_kind": "colleague", "share_call": "share", "share_call_p": 0.9})
    cells.update(overrides)
    return cells


def row(labels: dict[str, Any], *, worth: str = "yes", is_owner: bool = False) -> LabelRow:
    parsed = labels_from_saved(labels)
    return LabelRow(person_id="p:1", public_identifier="", is_owner=is_owner, worth=worth, flag=flag(parsed),
                    probabilities=parsed.probabilities, share_call=parsed.share_call, share_p=parsed.share_p)


def decided(labels: dict[str, Any], tags: frozenset[str] = frozenset(), **options: Any) -> tuple[str, str]:
    held = HumanTags(person_id="p:1", tags=tags, note="", updated_at="2026-10-13") if tags else None
    decision = share_decision(row(labels, **options), held, updated_at="2026-10-13")
    return decision.share, decision.reason


class ShareRuleTests(unittest.TestCase):
    def test_you_come_first(self) -> None:
        self.assertEqual(decided(saved(), is_owner=True), ("no", "owner"))
        self.assertEqual(decided(saved(share_call="keep_private"), frozenset({"share"})), ("yes", "human_share"))
        self.assertEqual(decided(saved(), frozenset({"private"})), ("no", "human_private"))

    def test_a_declined_yes_no_question_raises_no_flag(self) -> None:
        cells = saved()
        del cells["is_minor"]
        self.assertEqual(decided(cells), ("yes", "would_vouch"))

    def test_worth_then_the_flags_keep_people_home(self) -> None:
        self.assertEqual(decided(saved(), worth="no"), ("no", "worth_no"))
        self.assertEqual(decided(saved(), worth="maybe"), ("no", "worth_maybe"))
        self.assertEqual(decided(saved(relationship_kind="family")), ("no", "family"))
        self.assertEqual(decided(saved(is_family=0.5)), ("no", "family"))
        self.assertEqual(decided(saved(relationship_kind="romantic_partner")), ("no", "romantic_partner"))
        self.assertEqual(decided(saved(is_minor=0.7)), ("no", "minor"))

    def test_lunas_call_decides_the_rest_at_any_confidence(self) -> None:
        self.assertEqual(decided(saved()), ("yes", "would_vouch"))
        self.assertEqual(decided(saved(share_call="keep_private", share_call_p=0.8)), ("no", "not_professional"))
        # A coin-flip call still decides: nobody is asked to confirm (a wrong share takes a Private tag).
        self.assertEqual(decided(saved(share_call_p=0.52)), ("yes", "would_vouch"))
        self.assertEqual(decided(saved(share_call="keep_private", share_call_p=0.52)), ("no", "not_professional"))


if __name__ == "__main__":
    unittest.main()
