"""Signal refresh reuses saved evidence and preserves decisions on failure; synthetic data only."""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from test_people_worth import StoreTests
from test_logbook_people import Handler
from packs.ingestion.primitives.deep_context_v2 import decisions
from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, Person
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.worth import luna
from packs.ingestion.primitives.deep_context_v2.worth.worth import DECISIONS_CACHE_DIR, DEFAULT_LIMIT, Worth
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.share.person_labels import PersonLabels
from packs.ingestion.primitives.share.web.signals import PeopleSignals, RefreshSignals, SignalsPlan
from packs.ingestion.primitives.share.web.server import share_routes

NO_CALLS = mock.patch.object(decisions, "AsyncOpenAI", side_effect=AssertionError("no paid calls"))


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
            chosen = values[0]
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
        for candidate_id in ("c1", "c2"):
            bundle = CollectionBundle.of(Person(candidate_id, "", (), (), ()), messages=[], groups=[],
                                         thread_participants=(), available=0)
            queries.upsert_bundle(self.conn, candidate_id, json.dumps(bundle.to_payload()), "test", "2026-10-07")
        self._parent("p:a", "c1")
        self._parent("p:b", "c2")
        self._facts("c1", "Jordan Bravo")
        self._facts("c2", "Casey Delta")
        self.worth = Worth(self.conn, self.root, limit=DEFAULT_LIMIT)
        self.families = self.worth.families()
        for family in self.families:
            self._judged(family.members[0], family.fingerprint)
        self.conn.commit()
        Realize(self.conn, self.root).run()
        PersonLabels(self.conn, self.root).run()
        self.signals = PeopleSignals(self.root / "store.sqlite", self.root)

    def _judged(self, candidate_id: str, fingerprint: str) -> None:
        self.conn.execute(
            "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
            "VALUES (?, 'yes', 'machine', 'old', NULL, ?, '2026-10-07')", (candidate_id, fingerprint))

    def _cache(self, family) -> Path:
        """The family's request answered on disk."""
        cache = self.root / DECISIONS_CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        request = self.worth._request(family)
        first = answers_for(request)
        path = decisions.cache_path(cache, decisions.request_digest(luna.REQUEST_VERSION, request))
        path.write_text(json.dumps({"answers": first, "usage": None}))
        return path

    def _snapshot(self):
        return [tuple(row) for table in ("worth", "person_labels", "person_tags")
                for row in self.conn.execute(f"SELECT * FROM {table}")]

    def _finished(self):
        self.assertIsNotNone(self.signals._thread)
        self.signals._thread.join(5)
        self.assertFalse(self.signals._thread.is_alive())
        return self.signals.status()

    def _check(self, parent_ids):
        self.signals.check(parent_ids)
        status = self._finished()
        self.assertEqual(status["status"], "idle", status)
        return SignalsPlan(**status["plan"])

    def test_refresh_rejudges_only_selected_families_without_removing_old_rows(self) -> None:
        self.assertEqual(Worth(self.conn, self.root, limit=DEFAULT_LIMIT).estimate()["pending"], 2)
        refresh = Worth(self.conn, self.root, limit=DEFAULT_LIMIT, parent_ids=("p:a",))
        self.assertEqual([family.key for family in refresh.pending(refresh.families())], ["p:a"])
        before = self._snapshot()
        with mock.patch("packs.ingestion.primitives.deep_context_v2.worth.worth.decide_all",
                        side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                refresh.run()
        self.assertEqual(self._snapshot(), before)

    def test_check_returns_while_reading_families_and_publishes_progress_without_calls(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        request = Worth._request

        def slow_request(worth, family):
            if family.key == "p:b":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release the family read")
            return request(worth, family)

        before = self._snapshot()
        with NO_CALLS, mock.patch.object(Worth, "_request", slow_request):
            worker = threading.Thread(target=self.signals.check, args=((),))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                worker.join(0.1)
                self.assertFalse(worker.is_alive(), "check blocked the request thread")
                status = self.signals.status()
                self.assertEqual((status["status"], status["message"], status["done"], status["total"]),
                                 ("running", "Grouping each person's contacts: 1 of 2", 1, 2))
                print(status["message"], {"done": status["done"], "total": status["total"]})
                self.assertIsNone(status["plan"])
                with self.assertRaisesRegex(ValueError, "already"):
                    self.signals.start((), None)
            finally:
                release.set()
                worker.join(5)
                if self.signals._thread is not None:
                    self.signals._thread.join(5)
        status = self.signals.status()
        self.assertEqual(status["status"], "idle", status)
        self.assertEqual(status["plan"]["people"], 2)
        self.assertEqual(self._snapshot(), before)

    def test_refresh_returns_before_planning_finishes(self) -> None:
        self._cache(self.families[0])
        entered = threading.Event()
        release = threading.Event()
        plan = RefreshSignals.plan

        def slow_plan(node):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release planning")
            return plan(node)

        with NO_CALLS, mock.patch.object(RefreshSignals, "plan", slow_plan):
            caller = threading.Thread(target=self.signals.start, args=(("p:a",), None))
            caller.start()
            try:
                self.assertTrue(entered.wait(5))
                caller.join(0.1)
                self.assertFalse(caller.is_alive(), "refresh blocked the request thread")
                self.assertEqual(self.signals.status()["status"], "running")
                self.assertEqual(self.signals.status()["message"], "Grouping each person's contacts")
            finally:
                release.set()
                caller.join(5)
                status = self._finished()
        self.assertEqual(status["status"], "completed", status)

    def test_check_starts_without_a_count_and_keeps_it_when_no_families_are_pending(self) -> None:
        estimate = Worth.estimate

        def empty_estimate(node):
            self.assertEqual(self.signals.status()["message"], "Grouping each person's contacts")
            result = estimate(node)
            self.assertEqual(self.signals.status()["message"], "Grouping each person's contacts")
            return result

        with NO_CALLS, mock.patch.object(Worth, "_build_families", return_value=[]), \
                mock.patch.object(Worth, "estimate", empty_estimate):
            self.assertEqual(self._check(()).people, 0)

    def test_refresh_updates_people_without_a_count(self) -> None:
        self._cache(self.families[0])

        def updating_people():
            self.assertEqual(self.signals.status()["message"], "Updating People")
            self.assertEqual((self.signals.status()["done"], self.signals.status()["total"]), (0, 0))

        with NO_CALLS, mock.patch.object(PersonLabels, "execute", side_effect=updating_people) as labels:
            self.signals.start(("p:a",), None)
            status = self._finished()
        self.assertEqual(status["status"], "completed", status)
        labels.assert_called_once()

    def test_check_failure_is_reported_without_changing_signals_or_calling_providers(self) -> None:
        before = self._snapshot()
        with NO_CALLS, mock.patch.object(Worth, "_request", side_effect=RuntimeError("saved bundle unreadable")):
            with self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR") as logs:
                self.assertEqual(self.signals.check(())["status"], "running")
                status = self._finished()
        self.assertEqual((status["status"], status["error"]), ("failed", "saved bundle unreadable"))
        self.assertIn("Traceback (most recent call last):", logs.output[0])
        self.assertEqual(self._snapshot(), before)

    def test_a_refresh_asks_everyone_or_the_selection(self) -> None:
        # Every family is asked every time; only the unsaved answers cost anything.
        self._cache(self.families[1])
        plan = self._check(())
        self.assertEqual((plan.people, plan.cached, plan.new_requests), (2, 1, 1))
        self.assertEqual(self._check(("p:a",)).people, 1)

    def test_a_family_luna_refuses_keeps_its_old_signals_and_labels_still_run(self) -> None:
        # p:a's latest machine row holds JEV's labels; Luna refuses it again, and answers p:b. You said
        # both are worth yes, so the human decisions hold. The label export still completes.
        self.conn.execute(
            "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
            "VALUES ('c1', 'yes', 'machine', 'old', ?, 'an-older-version-key', '2026-10-08')",
            (json.dumps({"relationship_kind": "friend", "relationship_kind_p": 0.8, "is_family": 0.1}),))
        self.conn.executemany("INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) "
                              "VALUES (?, 'yes', 'human', 'mine', '2026-10-09')", [("c1",), ("c2",)])
        self.conn.commit()
        path = self._cache(self.families[1])
        refusal = [{"type": "refusal", "name": "relationship_kind"}]
        digest = decisions.request_digest(luna.REQUEST_VERSION, self.worth._request(self.families[0]))
        answers = {digest: refusal, path.stem: json.loads(path.read_text())["answers"]}
        node = RefreshSignals(self.conn, self.root, ("p:a", "p:b"))
        node.cache_only = False
        with mock.patch("packs.ingestion.primitives.deep_context_v2.worth.worth.decide_all", return_value=answers):
            manifest = node.run()
        self.assertEqual((manifest.status, manifest.counts["refused"]), ("completed", 1))
        flags = dict(self.conn.execute("SELECT candidate_id, flag FROM person_labels").fetchall())
        self.assertEqual(flags, {"c1": "", "c2": "family"})

    def test_selected_and_all_refresh_keep_human_relationship(self) -> None:
        from packs.ingestion.primitives.share.web.model import SharePeople
        from packs.ingestion.primitives.share.web.server import decide_relationship

        people = SharePeople(self.conn, self.root)
        decide_relationship(self.conn, people, "p:a", "colleague")
        for family in self.families:
            self._cache(family)
        with NO_CALLS:
            for selection in (("p:a",), ()):
                self.assertEqual(RefreshSignals(self.conn, self.root, selection).run().status, "completed")
                row = next(row for row in people.load() if row.parent_id == "p:a")
                self.assertEqual((row.relationship_kind, row.flag), ("colleague", ""))
                self.assertEqual(people.detail("p:a").relationship_kind_source, "human")
                self.assertNotIn("relationship_kind", people.detail("p:a").choice_p)
        saved = json.loads(self.conn.execute("SELECT labels_json FROM person_labels WHERE candidate_id = 'c1'").fetchone()[0])
        self.assertEqual(saved["relationship_kind"], "family")

    def test_a_refresh_keeps_lunas_worth_without_a_profile_or_messages(self) -> None:
        self._cache(self.families[0])
        with NO_CALLS:
            manifest = RefreshSignals(self.conn, self.root, ("p:a",)).run()
        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.counts["settle_worth_rows"], 0)
        self.assertEqual(self.conn.execute("SELECT worth FROM current_worth WHERE parent_id = 'p:a'").fetchone()[0],
                         "yes")

    def test_paid_refresh_without_confirmation_never_starts(self) -> None:
        plan = self._check(("p:a",))
        self.assertEqual((plan.people, plan.cached, plan.new_requests), (1, 0, 1))
        self.assertGreater(plan.estimated_cost_usd, 0)
        self.assertEqual(plan.estimated_seconds, 1)
        with mock.patch("packs.ingestion.primitives.deep_context_v2.worth.worth.decide_all") as calls:
            with self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR"):
                self.signals.start(("p:a",), None)
                status = self._finished()
        calls.assert_not_called()
        self.assertEqual(status["status"], "failed")
        self.assertIn("confirm", status["error"])

    def test_confirmation_rejects_changed_evidence_and_cache_coverage(self) -> None:
        plan = self._check(("p:a",))
        self._cache(self.families[0])
        with NO_CALLS, self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR"):
            self.signals.start(("p:a",), plan.checked)
            status = self._finished()
        self.assertEqual(status["status"], "failed")
        self.assertIn("changed", status["error"])
        plan = self._check(("p:a",))
        queries.upsert_owner(self.conn, '{"name":"Changed Owner"}', "changed", "2026-10-08")
        self.conn.commit()
        with NO_CALLS, self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR"):
            self.signals.start(("p:a",), plan.checked)
            status = self._finished()
        self.assertEqual(status["status"], "failed")
        self.assertIn("changed", status["error"])

    def test_cache_disappearing_after_free_start_cannot_enable_paid_calls(self) -> None:
        path = self._cache(self.families[0])
        before = self._snapshot()
        run = RefreshSignals.run

        def remove_answer(node):
            path.rename(path.with_suffix(".bkup"))
            return run(node)

        with mock.patch.object(RefreshSignals, "run", remove_answer):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}), NO_CALLS:
                with self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR") as logs:
                    self.signals.start(("p:a",), None)
                    self._finished()
        self.assertIn("Traceback (most recent call last):", logs.output[0])
        self.assertIn(self.signals.status()["error"], logs.output[0])
        self.assertEqual(self.signals.status()["status"], "failed")
        self.assertEqual(self._snapshot(), before)

    def test_cached_refresh_updates_labels_and_keeps_human_worth_and_tags(self) -> None:
        self._cache(self.families[0])
        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) "
                          "VALUES ('c1', 'no', 'human', 'keep', '2026-10-08')")
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, note, updated_at) "
                          "VALUES ('c1', 'is_recruiter', 'keep note', '2026-10-08')")
        self.conn.commit()
        with NO_CALLS:
            self.signals.start(("p:a",), None)
            self.signals._thread.join(10)
        self.assertEqual(self.signals.status()["status"], "completed", self.signals.status())
        self.assertEqual(self.conn.execute("SELECT worth, decided_by FROM current_worth WHERE parent_id = 'p:a'").fetchone()[:], ("no", "human"))
        self.assertEqual(self.conn.execute("SELECT tags, note FROM person_tags WHERE candidate_id = 'c1'").fetchone()[:], ("is_recruiter", "keep note"))
        # p:b was not asked: no worth row of its carries Luna's labels.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth WHERE candidate_id = 'c2' "
                                           "AND labels_json LIKE '%\"version\"%'").fetchone()[0], 0)
        labels = json.loads(self.conn.execute("SELECT labels_json FROM person_labels WHERE candidate_id = 'c1'").fetchone()[0])
        self.assertEqual(labels["relationship_kind"], "family")
        self.assertNotIn("share_call", labels)

    def test_cache_disappearing_after_paid_confirmation_does_not_expand_paid_work(self) -> None:
        path = self._cache(self.families[0])
        plan = self._check(("p:a", "p:b"))
        self.assertEqual((plan.cached, plan.new_requests), (1, 1))
        run = RefreshSignals.run

        def remove_answer(node):
            path.rename(path.with_suffix(".bkup"))
            return run(node)

        with mock.patch.object(RefreshSignals, "run", remove_answer), NO_CALLS:
            with self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR"):
                self.signals.start(("p:a", "p:b"), plan.checked)
                self._finished()
        self.assertEqual(self.signals.status()["status"], "failed")

    def test_cache_only_client_cannot_call_provider_with_a_key_present(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}), NO_CALLS:
            with self.assertRaisesRegex(ValueError, "not saved yet"):
                Worth(self.conn, self.root, limit=DEFAULT_LIMIT, parent_ids=("p:a",)).execute(cache_only=True)

    def test_labels_failure_rolls_back_new_machine_worth_and_preserves_cache(self) -> None:
        path = self._cache(self.families[0])
        before = self._snapshot()
        node = RefreshSignals(self.conn, self.root, ("p:a",))
        with mock.patch.object(PersonLabels, "execute", side_effect=RuntimeError("labels failed")):
            with self.assertRaisesRegex(RuntimeError, "labels failed"):
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
            self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "running")
            routes.signals._thread.join(5)
            plan = routes.signals.status()["plan"]
            self.assertEqual((plan["people"], plan["new_requests"]), (1, 1))
            handler = Handler(body=json.dumps({"people": ["p:a"]}).encode())
            with self.assertLogs("packs.ingestion.primitives.share.web.signals", level="ERROR"):
                self.assertTrue(routes.post(handler, urllib.parse.urlparse("/api/people/signals")))
                self.assertEqual(handler.response_status, 200)
                self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "running")
                routes.signals._thread.join(5)
            self.assertEqual(routes.signals.status()["status"], "failed")
            self.assertIn("confirm", routes.signals.status()["error"])

    def test_routes_refresh_everyone_with_no_one_selected(self) -> None:
        routes = share_routes(self.conn, self.root, upload_dir=self.root / "upload")
        handler = Handler(body=json.dumps({"people": []}).encode())
        routes.post(handler, urllib.parse.urlparse("/api/people/signals/check"))
        self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "running")
        routes.signals._thread.join(5)
        self.assertEqual(routes.signals.status()["plan"]["people"], 2)

    def test_routes_reject_a_malformed_selection_and_other_origins(self) -> None:
        routes = share_routes(self.conn, self.root, upload_dir=self.root / "upload")
        handler = Handler(body=json.dumps({"people": [7]}).encode())
        routes.post(handler, urllib.parse.urlparse("/api/people/signals/check"))
        self.assertEqual(handler.response_status, 409)
        handler = Handler(origin="http://attacker.example", body=b'{"people":["p:a"]}')
        routes.post(handler, urllib.parse.urlparse("/api/people/signals"))
        self.assertEqual(handler.response_status, 403)
        self.assertIsNone(routes.signals._thread)
