"""Signal refresh reuses saved evidence and preserves decisions on failure; synthetic data only."""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from test_share_v2 import StoreTests
from test_logbook_people import Handler
from packs.ingestion.primitives.deep_context_v2 import decisions
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.worth import luna
from packs.ingestion.primitives.deep_context_v2.worth.worth import DECISIONS_CACHE_DIR, DEFAULT_LIMIT, Worth
from packs.ingestion.primitives.share.share_list import Share
from packs.ingestion.primitives.share.web.signals import PeopleSignals, RefreshSignals
from packs.ingestion.primitives.share.web.server import share_routes

NO_CALLS = mock.patch.object(decisions, "AsyncOpenAI", side_effect=AssertionError("no paid calls"))


def answers_for(request: dict[str, Any], share_call: str = "share") -> list[dict[str, Any]]:
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
            chosen = share_call if name == "share_call" else values[0]
            answers.append({"type": "choice", "name": name, "choice": chosen, "confidence": 0.9, "probabilities": [
                {"value": value, "probability": 0.9 if value == chosen else 0.05} for value in values]})
    return answers


class SignalsRefreshTests(StoreTests):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.tmp.name)
        queries.upsert_owner(self.conn, '{"name":"Test Owner"}', "test", "2026-10-07")
        self.conn.executemany("INSERT INTO candidate_sources (candidate_id, source) VALUES (?, 'gmail_msgvault')", [("c1",), ("c2",)])
        self.conn.executemany("INSERT INTO candidate_identifiers (candidate_id, kind, normalized_value, display_value) "
                             "VALUES (?, 'email', ?, ?)", [("c1", "one@example.test", "one@example.test"),
                                                           ("c2", "two@example.test", "two@example.test")])
        self.conn.executemany("INSERT INTO bundles (candidate_id, payload_json, content_fingerprint, collected_at) "
                             "VALUES (?, '{\"messages\":[],\"groups\":[]}', 'test', '2026-10-07')", [("c1",), ("c2",)])
        self._parent("p:a", "c1")
        self._parent("p:b", "c2")
        self._facts("c1", "Jordan Bravo")
        self._facts("c2", "Casey Delta")
        self.families = Worth(self.conn, self.root, limit=DEFAULT_LIMIT).families()
        for family in self.families:
            self._judged(family.members[0], family.fingerprint)
        self.conn.commit()
        Share(self.conn, self.root).run()
        self.signals = PeopleSignals(self.root / "store.sqlite", self.root)

    def _judged(self, candidate_id: str, fingerprint: str) -> None:
        self.conn.execute(
            "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
            "VALUES (?, 'yes', 'machine', 'old', NULL, ?, '2026-10-07')", (candidate_id, fingerprint))

    def _cache(self, family, share_call: str = "share") -> Path:
        """Both of the family's requests answered on disk; returns request 1's file."""
        cache = self.root / DECISIONS_CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        first = answers_for(family.request)
        path = decisions.cache_path(cache, family.digest)
        path.write_text(json.dumps({"answers": first, "usage": None}))
        share = luna.share_request(luna.labels(first))
        second = decisions.cache_path(cache, decisions.request_digest(luna.REQUEST_VERSION, share))
        second.write_text(json.dumps({"answers": answers_for(share, share_call), "usage": None}))
        return path

    def _snapshot(self):
        return [tuple(row) for table in ("worth", "person_labels", "share", "person_tags")
                for row in self.conn.execute(f"SELECT * FROM {table}")]

    def test_refresh_rejudges_only_selected_families_without_removing_old_rows(self) -> None:
        self.assertEqual(Worth(self.conn, self.root, limit=DEFAULT_LIMIT).estimate()["pending"], 0)
        refresh = Worth(self.conn, self.root, limit=DEFAULT_LIMIT, parent_ids=("p:a",))
        self.assertEqual([family.key for family in refresh.pending(refresh.families())], ["p:a"])
        before = self._snapshot()
        with mock.patch("packs.ingestion.primitives.deep_context_v2.worth.worth.decide_all",
                        side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                refresh.run()
        self.assertEqual(self._snapshot(), before)

    def test_a_refresh_of_a_few_people_brings_older_signals_up_to_date(self) -> None:
        # p:b was judged under older questions: any refresh asks it again, so share never mixes versions.
        self._judged("c2", "an-older-version-key")
        self.conn.commit()
        self.assertEqual(self.signals.check(()).people, 1)
        self.assertEqual(self.signals.check(("p:a",)).people, 2)

    def test_paid_refresh_without_confirmation_never_starts(self) -> None:
        plan = self.signals.check(("p:a",))
        self.assertEqual((plan.people, plan.cached, plan.new_requests), (1, 0, 2))
        self.assertGreater(plan.estimated_cost_usd, 0)
        self.assertEqual(plan.estimated_seconds, 1)
        with mock.patch("packs.ingestion.primitives.deep_context_v2.worth.worth.decide_all") as calls:
            with self.assertRaisesRegex(ValueError, "confirm"):
                self.signals.start(("p:a",), None)
        calls.assert_not_called()
        self.assertIsNone(self.signals._thread)

    def test_confirmation_rejects_changed_evidence_and_cache_coverage(self) -> None:
        plan = self.signals.check(("p:a",))
        self._cache(self.families[0])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.signals.start(("p:a",), plan.checked)
        plan = self.signals.check(("p:a",))
        queries.upsert_owner(self.conn, '{"name":"Changed Owner"}', "changed", "2026-10-08")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.signals.start(("p:a",), plan.checked)

    def test_cache_disappearing_after_free_start_cannot_enable_paid_calls(self) -> None:
        path = self._cache(self.families[0])
        before = self._snapshot()
        with mock.patch("packs.ingestion.primitives.share.web.signals.threading.Thread") as thread:
            self.signals.start(("p:a",), None)
            node = thread.call_args.kwargs["args"][0]
        path.rename(path.with_suffix(".bkup"))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}), NO_CALLS:
            self.signals._run(node)
        self.assertEqual(self.signals.status()["status"], "failed")
        self.assertEqual(self._snapshot(), before)

    def test_cached_refresh_updates_labels_and_keeps_human_worth_and_tags(self) -> None:
        self._cache(self.families[0])
        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) "
                          "VALUES ('c1', 'no', 'human', 'keep', '2026-10-08')")
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, note, updated_at) "
                          "VALUES ('c1', 'private', 'keep note', '2026-10-08')")
        self.conn.commit()
        with NO_CALLS:
            self.signals.start(("p:a",), None)
            self.signals._thread.join(10)
        self.assertEqual(self.signals.status()["status"], "completed", self.signals.status())
        self.assertEqual(self.conn.execute("SELECT worth, decided_by FROM current_worth WHERE parent_id = 'p:a'").fetchone()[:], ("no", "human"))
        self.assertEqual(self.conn.execute("SELECT tags, note FROM person_tags WHERE candidate_id = 'c1'").fetchone()[:], ("private", "keep note"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth WHERE candidate_id = 'c2'").fetchone()[0], 1)
        labels = json.loads(self.conn.execute("SELECT labels_json FROM person_labels WHERE candidate_id = 'c1'").fetchone()[0])
        self.assertEqual((labels["relationship_kind"], labels["share_call"]), ("family", "share"))
        self.assertEqual(self.conn.execute("SELECT reason FROM share WHERE candidate_id = 'c1'").fetchone()[0], "human_private")

    def test_cache_disappearing_after_paid_confirmation_does_not_expand_paid_work(self) -> None:
        path = self._cache(self.families[0])
        plan = self.signals.check(("p:a", "p:b"))
        self.assertEqual((plan.cached, plan.new_requests), (2, 2))
        with mock.patch("packs.ingestion.primitives.share.web.signals.threading.Thread") as thread:
            self.signals.start(("p:a", "p:b"), plan.checked)
            node = thread.call_args.kwargs["args"][0]
        path.rename(path.with_suffix(".bkup"))
        with NO_CALLS:
            self.signals._run(node)
        self.assertEqual(self.signals.status()["status"], "failed")

    def test_cache_only_client_cannot_call_provider_with_a_key_present(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}), NO_CALLS:
            with self.assertRaisesRegex(ValueError, "not saved yet"):
                Worth(self.conn, self.root, limit=DEFAULT_LIMIT, parent_ids=("p:a",)).execute(cache_only=True)

    def test_share_failure_rolls_back_new_machine_worth_and_preserves_cache(self) -> None:
        path = self._cache(self.families[0])
        before = self._snapshot()
        node = RefreshSignals(self.conn, self.root, ("p:a",))
        with mock.patch.object(Share, "execute", side_effect=RuntimeError("share failed")):
            with self.assertRaisesRegex(RuntimeError, "share failed"):
                node.run()
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(path.exists())

    def test_routes_check_saved_evidence_without_provider_calls_and_gate_paid_start(self) -> None:
        routes = share_routes(self.conn, self.root, upload_dir=self.root / "upload")
        with NO_CALLS:
            handler = Handler()
            self.assertTrue(routes.get(handler, urllib.parse.urlparse("/api/people/signals")))
            self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "idle")
            handler = Handler(body=json.dumps({"people": ["p:a"]}).encode())
            self.assertTrue(routes.post(handler, urllib.parse.urlparse("/api/people/signals/check")))
            plan = json.loads(handler.wfile.getvalue())
            self.assertEqual((plan["people"], plan["new_requests"]), (1, 2))
            handler = Handler(body=json.dumps({"people": ["p:a"]}).encode())
            self.assertTrue(routes.post(handler, urllib.parse.urlparse("/api/people/signals")))
            self.assertEqual(handler.response_status, 409)
            self.assertIn("confirm", json.loads(handler.wfile.getvalue())["error"])

    def test_routes_refresh_whoever_needs_it_with_no_one_selected(self) -> None:
        routes = share_routes(self.conn, self.root, upload_dir=self.root / "upload")
        handler = Handler(body=json.dumps({"people": []}).encode())
        routes.post(handler, urllib.parse.urlparse("/api/people/signals/check"))
        self.assertEqual(json.loads(handler.wfile.getvalue())["people"], 0)

    def test_routes_reject_unknown_selection_and_other_origins(self) -> None:
        routes = share_routes(self.conn, self.root, upload_dir=self.root / "upload")
        for people in ([7], ["missing"]):
            handler = Handler(body=json.dumps({"people": people}).encode())
            routes.post(handler, urllib.parse.urlparse("/api/people/signals/check"))
            self.assertEqual(handler.response_status, 409)
        handler = Handler(origin="http://attacker.example", body=b'{"people":["p:a"]}')
        routes.post(handler, urllib.parse.urlparse("/api/people/signals"))
        self.assertEqual(handler.response_status, 403)
        self.assertIsNone(routes.signals._thread)
