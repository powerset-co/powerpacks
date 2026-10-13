"""Signal refresh reuses saved evidence and preserves decisions on failure; synthetic data only."""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from unittest import mock

from test_share_v2 import StoreTests
from test_logbook_people import Handler
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.worth import jev
from packs.ingestion.primitives.deep_context_v2.worth.worth import DEFAULT_LIMIT, Worth
from packs.ingestion.primitives.share.share_list import Share
from packs.ingestion.primitives.share.web.signals import PeopleSignals, RefreshSignals
from packs.ingestion.primitives.share.web.server import share_routes
from packs.search.primitives.llm_rerank_candidates.jev import client


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
            self.conn.execute(
                "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
                "VALUES (?, 'yes', 'machine', 'old', NULL, ?, '2026-10-07')",
                (family.members[0], family.fingerprint))
        self.conn.commit()
        Share(self.conn, self.root).run()
        self.signals = PeopleSignals(self.root / "store.sqlite", self.root)

    def _cache(self, family) -> Path:
        answers = {}
        for name, question in family.request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.1}
            else:
                options = list(question["criteria"]) if question["type"] == "choice" else [str(i) for i in range(len(question["criteria"]))]
                answers[name] = {"type": question["type"], "probabilities": {option: float(i == 0) for i, option in enumerate(options)}}
        raw = json.dumps({"model": client.MODEL, "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}})
        saved = client._cache_record(raw, family.digest, family.request,
                                     request_version=jev.REQUEST_VERSION, question_version=jev.REQUEST_VERSION)
        path = client.cache_path(self.root / "deep-context", family.digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved))
        return path

    def _snapshot(self):
        return [tuple(row) for table in ("worth", "person_labels", "share", "person_tags")
                for row in self.conn.execute(f"SELECT * FROM {table}")]

    def test_refresh_rejudges_only_selected_families_without_removing_old_rows(self) -> None:
        self.assertEqual(Worth(self.conn, self.root, limit=DEFAULT_LIMIT).estimate()["pending"], 0)
        refresh = Worth(self.conn, self.root, limit=DEFAULT_LIMIT, parent_ids=("p:a",))
        self.assertEqual([family.key for family in refresh.pending(refresh.families())], ["p:a"])
        before = self._snapshot()
        with mock.patch.object(jev, "answer_all", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                refresh.run()
        self.assertEqual(self._snapshot(), before)

    def test_paid_refresh_without_confirmation_never_starts(self) -> None:
        plan = self.signals.check(("p:a",))
        self.assertEqual((plan.people, plan.cached, plan.new_requests), (1, 0, 1))
        self.assertGreater(plan.estimated_cost_usd, 0)
        with mock.patch.object(jev, "answer_all") as calls:
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
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic"}), mock.patch.object(client, "_request") as paid:
            self.signals._run(node)
        paid.assert_not_called()
        self.assertEqual(self.signals.status()["status"], "failed")
        self.assertEqual(self._snapshot(), before)

    def test_cached_refresh_updates_labels_and_keeps_human_worth_and_tags(self) -> None:
        self._cache(self.families[0])
        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) "
                          "VALUES ('c1', 'no', 'human', 'keep', '2026-10-08')")
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, note, updated_at) "
                          "VALUES ('c1', 'private', 'keep note', '2026-10-08')")
        self.conn.commit()
        with mock.patch.object(client, "_request", side_effect=AssertionError("no paid calls")):
            self.signals.start(("p:a",), None)
            self.signals._thread.join(10)
        self.assertEqual(self.signals.status()["status"], "completed", self.signals.status())
        self.assertEqual(self.conn.execute("SELECT worth, decided_by FROM current_worth WHERE parent_id = 'p:a'").fetchone()[:], ("no", "human"))
        self.assertEqual(self.conn.execute("SELECT tags, note FROM person_tags WHERE candidate_id = 'c1'").fetchone()[:], ("private", "keep note"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth WHERE candidate_id = 'c2'").fetchone()[0], 1)
        labels = json.loads(self.conn.execute("SELECT labels_json FROM person_labels WHERE candidate_id = 'c1'").fetchone()[0])
        self.assertIn("relationship_kind", labels)
        self.assertEqual(self.conn.execute("SELECT reason FROM share WHERE candidate_id = 'c1'").fetchone()[0], "human_private")

    def test_cache_disappearing_after_paid_confirmation_does_not_expand_paid_work(self) -> None:
        path = self._cache(self.families[0])
        plan = self.signals.check(("p:a", "p:b"))
        self.assertEqual((plan.cached, plan.new_requests), (1, 1))
        with mock.patch("packs.ingestion.primitives.share.web.signals.threading.Thread") as thread:
            self.signals.start(("p:a", "p:b"), plan.checked)
            node = thread.call_args.kwargs["args"][0]
        path.rename(path.with_suffix(".bkup"))
        with mock.patch.object(client, "_request") as paid:
            self.signals._run(node)
        paid.assert_not_called()
        self.assertEqual(self.signals.status()["status"], "failed")

    def test_cache_only_client_cannot_call_provider_with_a_key_present(self) -> None:
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic"}), mock.patch.object(client, "_request") as paid:
            with self.assertRaisesRegex(RuntimeError, "requires TYPESAFE_API_KEY"):
                Worth(self.conn, self.root, limit=DEFAULT_LIMIT, parent_ids=("p:a",)).execute(cache_only=True)
        paid.assert_not_called()

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
        with mock.patch.object(client, "_request", side_effect=AssertionError("no paid calls")):
            handler = Handler()
            self.assertTrue(routes.get(handler, urllib.parse.urlparse("/api/people/signals")))
            self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "idle")
            handler = Handler(body=json.dumps({"people": ["p:a"]}).encode())
            self.assertTrue(routes.post(handler, urllib.parse.urlparse("/api/people/signals/check")))
            plan = json.loads(handler.wfile.getvalue())
            self.assertEqual((plan["people"], plan["new_requests"]), (1, 1))
            handler = Handler(body=json.dumps({"people": ["p:a"]}).encode())
            self.assertTrue(routes.post(handler, urllib.parse.urlparse("/api/people/signals")))
            self.assertEqual(handler.response_status, 409)
            self.assertIn("confirm", json.loads(handler.wfile.getvalue())["error"])

    def test_routes_reject_unknown_selection_and_other_origins(self) -> None:
        routes = share_routes(self.conn, self.root, upload_dir=self.root / "upload")
        for people in ([], [7], ["missing"]):
            handler = Handler(body=json.dumps({"people": people}).encode())
            routes.post(handler, urllib.parse.urlparse("/api/people/signals/check"))
            self.assertEqual(handler.response_status, 409)
        handler = Handler(origin="http://attacker.example", body=b'{"people":["p:a"]}')
        routes.post(handler, urllib.parse.urlparse("/api/people/signals"))
        self.assertEqual(handler.response_status, 403)
        self.assertIsNone(routes.signals._thread)
