"""Review decisions and request parsing without a browser or HTTP server."""
from __future__ import annotations

import io
import json
import unittest
import urllib.parse
from dataclasses import replace
from http import HTTPStatus
from unittest.mock import Mock, patch

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_review
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles
from packs.ingestion.primitives.deep_context_v2.enrich.research import handle
from packs.ingestion.primitives.deep_context_v2.review import decisions, payloads, queue, server
from packs.ingestion.primitives.deep_context_v2.review.api import Refusal, ReviewApi
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import EmployerFact, NotableEvent, OwnedIdentifiers, SharedContextFact, SynthesizedFacts
from tests.deep_context_v2.test_store import NOW, StoreFixture

URL = "https://www.linkedin.com/in/jordan-bravo"


def facts() -> SynthesizedFacts:
    return SynthesizedFacts("Jordan Bravo", (), (EmployerFact("Example Labs", "Engineer", "current"),), "Engineer", "Example U", "CS", "Springfield", "colleague", "work", ("<script>topic</script>",), (NotableEvent("2026", "started work"),), (), OwnedIdentifiers((), (), ()), (SharedContextFact("school", "Example U", "class"),), .8, False)


class Handler:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = 0
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key] = value

    def end_headers(self) -> None:
        pass

    def json(self) -> dict[str, object]:
        return json.loads(self.wfile.getvalue())


class ReviewTests(StoreFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        value = json.dumps(facts().to_payload())
        for key in ("a", "b"):
            queries.upsert_facts(self.conn, key, value, "fp", "fake", "none", NOW)
            queries.upsert_bundle(self.conn, key, '{"messages":[{"channel":"gmail"},{"channel":"imessage"}]}', "fp", NOW)
            queries.insert_candidate_identifiers(self.conn, [(key, "email", "jordan@example.com", "Jordan@example.com")])
            queries.insert_candidate_sources(self.conn, [(key, "gmail_msgvault")])
        self.conn.execute("INSERT INTO worth(candidate_id,worth,decided_by,reason,labels_json,input_fingerprint,created_at) VALUES ('a','yes','machine','','{\"is_founder\":0.9}','fp',?)", (NOW,))
        self.pending("a")
        self.conn.commit()
        self.api = ReviewApi(self.conn, self.root)

    def pending(self, key: str, *, member: str = "42", url: str = URL, verdict: str = "needs_review", decider: str = "machine") -> None:
        self.conn.execute("INSERT INTO candidate_linkedins(candidate_id,linkedin_url,member_id,origin,verdict,decided_by,judgment_fingerprint,confidence,reason,created_at) VALUES (?,?,?,'research',?,?,'fp',.7,'name only',?)", (key, url, member, verdict, decider, NOW))

    def test_card_rolls_up_real_queries_and_deduplicates_pending(self) -> None:
        self.pending("b")
        card = queue.load_card(self.conn, self.root, "p:1")
        self.assertEqual([m.candidate_id for m in card.members], ["a", "b"])
        self.assertEqual(card.messages, {"gmail": 2, "imessage": 2})
        self.assertEqual(len(card.pending), 1)
        self.assertEqual(card.pending[0].confidence, .7)
        self.assertEqual(card.pending[0].reason, "name only")
        self.assertEqual(queue.review_list(self.conn), ["p:1"])
        self.assertEqual(queries_review.labels_json(self.conn, []), None)
        self.assertEqual(queries_review.sources(self.conn, []), [])
        self.assertEqual(queries_review.identifiers(self.conn, []), [])
        self.assertEqual(queries_review.current_verdicts(self.conn, []), [])
        self.assertEqual(queries_review.message_counts(self.conn, []), {})

    def test_yes_moves_every_member_and_human_verdict_survives_machine(self) -> None:
        card = queue.load_card(self.conn, self.root, "p:1")
        self.assertEqual(decisions.yes(self.conn, card, "42"), URL)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT parent_id FROM current_parent")}, {"li:42"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM current_linkedins WHERE verdict='confirmed' AND decided_by='human'").fetchone()[0], 2)
        self.pending("a", verdict="wrong_person")
        self.assertEqual(self.conn.execute("SELECT verdict FROM current_linkedins WHERE candidate_id='a'").fetchone()[0], "confirmed")
        self.assertEqual(queue.review_list(self.conn), [])

    def test_yes_unknown_key_writes_nothing(self) -> None:
        card = queue.load_card(self.conn, self.root, "p:1")
        before = self.conn.total_changes
        with self.assertRaises(decisions.DecisionError):
            decisions.yes(self.conn, card, "missing")
        self.assertEqual(self.conn.total_changes, before)

    def test_skip_rejects_all_members_and_sets_human_worth_no(self) -> None:
        decisions.skip(self.conn, queue.load_card(self.conn, self.root, "p:1"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM current_linkedins WHERE verdict='wrong_person'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT worth,decided_by FROM current_worth").fetchone()[:], ("no", "human"))
        self.assertEqual(queue.review_list(self.conn), [])

    def test_skip_without_pending_only_writes_worth(self) -> None:
        card = replace(queue.load_card(self.conn, self.root, "p:1"), pending=())
        decisions.skip(self.conn, card)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT worth FROM current_worth").fetchone()[0], "no")

    def test_decision_rolls_back_if_second_member_write_fails(self) -> None:
        card = queue.load_card(self.conn, self.root, "p:1")
        insert = queries_review.insert_parent
        calls = 0
        def fail_second(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic failure")
            insert(*args, **kwargs)
        with patch.object(queries_review, "insert_parent", side_effect=fail_second), self.assertRaises(RuntimeError):
            decisions.yes(self.conn, card, "42")
        self.assertEqual({r[0] for r in self.conn.execute("SELECT parent_id FROM current_parent")}, {"p:1"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 1)

    def test_synthetic_yes_preserves_parent_and_exports_profile_key(self) -> None:
        research_handle = handle(facts())
        result = {"content": {"real_name": "Jordan Bravo"}, "basis": []}
        self.conn.execute("DELETE FROM candidate_linkedins")
        self.conn.execute("INSERT INTO research VALUES (?, 'p:1','no_match',?,?)", (research_handle, json.dumps(result), NOW))
        self.conn.commit()
        self.assertTrue(queue.has_synthetic_card(self.conn, "p:1"))
        self.assertEqual(queue.review_list(self.conn), ["p:1"])
        card = queue.load_card(self.conn, self.root, "p:1")
        key = "synthetic:" + research_handle
        decisions.yes(self.conn, card, key)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT parent_id FROM current_parent")}, {"p:1"})
        self.assertEqual(self.conn.execute("SELECT profile_key FROM current_profile").fetchone()[0], key)
        self.assertEqual(queue.review_list(self.conn), [])

    def test_stale_research_or_no_research_does_not_queue_a_card(self) -> None:
        self.conn.execute("DELETE FROM candidate_linkedins")
        self.assertEqual(queue.review_list(self.conn), [])
        self.conn.execute("INSERT INTO research VALUES ('old','p:1','no_match','{}',?)", (NOW,))
        self.assertEqual(queue.review_list(self.conn), [])
        self.assertFalse(queue.has_synthetic_card(self.conn, "p:1"))

    def test_retarget_invalid_or_missing_profile_writes_nothing(self) -> None:
        card = queue.load_card(self.conn, self.root, "p:1")
        before = self.conn.total_changes
        with patch("packs.ingestion.primitives.deep_context_v2.review.decisions.load_profiles", return_value=Profiles({}, 1, 0)) as fetch:
            with self.assertRaises(decisions.DecisionError):
                decisions.retarget(self.conn, self.root, card, "not a URL")
            fetch.assert_not_called()
            with self.assertRaises(decisions.DecisionError):
                decisions.retarget(self.conn, self.root, card, URL)
        self.assertEqual(self.conn.total_changes, before)

    def test_retarget_confirmed_profile_moves_whole_family(self) -> None:
        profile = Profile(URL, "jordan-bravo", "99", "Jordan Bravo", "Founder", "Springfield", (), (), NOW)
        with patch("packs.ingestion.primitives.deep_context_v2.review.decisions.load_profiles", return_value=Profiles({URL: profile}, 0, 1)):
            self.assertEqual(decisions.retarget(self.conn, self.root, queue.load_card(self.conn, self.root, "p:1"), URL), URL)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT parent_id FROM current_parent")}, {"li:99"})
        self.assertEqual({r[0] for r in self.conn.execute("SELECT origin FROM current_linkedins WHERE member_id='99'")}, {"human_override"})

    def test_payload_labels_names_contacts_and_escaped_dossier(self) -> None:
        card = queue.load_card(self.conn, self.root, "p:1")
        person = payloads.person(card)
        self.assertEqual(person.name, "Jordan Bravo")
        self.assertEqual(person.contacts, "Jordan@example.com")
        self.assertEqual(person.sources, ("gmail",))
        self.assertEqual(person.labels, ("Founder",))
        self.assertEqual(payloads.person_name(replace(card, members=())), "Jordan Bravo")
        self.assertEqual(payloads._labels(None), ())
        self.assertEqual(payloads._labels('{"is_professional":0.9,"work_signal":0.95,"is_founder":0.84,"relationship_kind":"friend","relationship_kind_p":0.9}'), ("Work-related", "Friend"))
        fragment = payloads.dossier(card)
        self.assertNotIn("<script>", fragment)
        self.assertIn("&lt;script&gt;", fragment)
        self.assertIn("Engineer @ Example Labs", fragment)
        self.assertIn("gmail: 2", fragment)
        self.assertEqual(payloads.page(3).progress.linkedin_pending, 3)

    def test_payload_pending_profile_missing_and_research_variants(self) -> None:
        card = queue.load_card(self.conn, self.root, "p:1")
        self.assertEqual(payloads.candidates(card)[0].url, URL)
        profile = Profile(URL, "jordan-bravo", "42", "Jordan Bravo", "Founder", "Springfield", ("Founder @ Example",), ("Example U",), NOW)
        pending = replace(card.pending[0], profile=profile)
        self.assertEqual(payloads.candidates(replace(card, pending=(pending,)))[0].headline, "Founder")
        result = {"content": {"real_name": "Jordan Bravo", "summary": "Founder", "work_experience": [{"company_name": "Example", "title": "Founder"}], "education": [{"degree": "BS", "school_name": "Example U"}], "location_city": "Springfield", "location_country": "US"}, "basis": [{"field": "other"}, {"field": "real_name", "confidence": "high", "reasoning": "known name"}]}
        synthetic = replace(pending, key="synthetic:h", research=result)
        rendered = payloads.candidates(replace(card, pending=(synthetic,)))[0]
        self.assertTrue(rendered.synthetic)
        self.assertEqual(rendered.confidence, .9)
        self.assertEqual(rendered.reason, "known name")
        self.assertEqual(rendered.education, ("BS, Example U",))
        self.assertEqual(payloads.candidates(replace(card, pending=())), ())

    def test_api_card_exclusion_wrapping_and_next_after_save(self) -> None:
        self.assertEqual(self.api.linkedin_card({"exclude": [" p:1 , "]}).finished.synthesize_pending, False)
        card = self.api.linkedin_card({"index": ["999"], "debug": ["1"]})
        self.assertEqual(card.queue.index, 0)
        self.assertEqual(card.queue.total, 1)
        result = self.api.decide({"parent_slug": ["p:1"], "decision": ["keep"], "pub": ["42"]})
        self.assertTrue(result.ok)
        self.assertEqual(result.next.pending, 0)
        with self.assertRaises(Refusal) as caught:
            self.api.decide({"parent_slug": ["p:1"], "decision": ["detach"]})
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_api_unknown_and_invalid_decisions_are_bad_requests(self) -> None:
        for form in [{"decision": ["unknown"]}, {"decision": ["keep"], "pub": ["missing"]}, {"decision": ["fix"], "new_url": ["bad"]}]:
            with self.subTest(form=form), self.assertRaises(Refusal) as caught:
                self.api.decide({"parent_slug": ["p:1"], **form})
            self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(queue.review_list(self.conn), ["p:1"])

    def test_get_routes_send_shape_and_security_headers(self) -> None:
        for path in ("/api/review/page", "/api/review/linkedin-card", "/api/dossier?slug=p:1", "/missing"):
            handler = Handler()
            self.api.get(handler, urllib.parse.urlparse(path))
            self.assertEqual(handler.response_headers["Cache-Control"], "no-store")
            self.assertEqual(handler.response_headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(int(handler.response_headers["Content-Length"]), len(handler.wfile.getvalue()))
            self.assertEqual(handler.status, 404 if path == "/missing" else 200)

    def test_post_routes_cross_origin_and_refusals(self) -> None:
        handler = Handler(headers={"Origin": "https://example.com"})
        self.api.post(handler, urllib.parse.urlparse("/api/review/decide"))
        self.assertEqual(handler.status, 403)
        for path, status in [("/retarget", 501), ("/missing", 404)]:
            handler = Handler(headers={"Origin": "http://localhost:8777"})
            self.api.post(handler, urllib.parse.urlparse(path))
            self.assertEqual(handler.status, status)
        handler = Handler(b"parent_slug=p%3A1&decision=detach")
        self.api.post(handler, urllib.parse.urlparse("/api/review/decide"))
        self.assertTrue(handler.json()["ok"])
        self.assertEqual(handler.status, 200)


    def test_query_plans_and_count_only_check_are_reachable(self) -> None:
        self.assertEqual(queries_review.first_family(self.conn), "p:1")
        result = server.check(self.conn, self.root)
        self.assertEqual(result["queue"]["families"], 1)
        self.assertEqual(result["card"]["members"], 2)
        self.assertEqual(result["card"]["messages"], 4)
        self.assertTrue(result["card"]["plan_members"])

    def test_handler_dispatch_uses_in_process_doubles_not_a_server(self) -> None:
        api = Mock()
        app = Mock()
        app.get.return_value = False
        with patch.object(server, "AppRoutes", return_value=app):
            cls = server.make_handler(api)
        handler = object.__new__(cls)
        handler.path = "/api/review/page"
        handler.do_GET()
        api.get.assert_called_once()
        app.get.return_value = True
        handler.do_GET()
        self.assertEqual(api.get.call_count, 1)
        handler.do_POST()
        api.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
