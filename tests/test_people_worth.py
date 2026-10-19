"""People worth, labels and tags on synthetic v2 stores."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.lookup import matches
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.ingestion.primitives.share.store import TagStore

NOW = "2026-10-07T20:00:00Z"
LATER = "2026-10-07T21:00:00Z"


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        queries.upsert_candidates(self.conn, [("c1", "Jordan Bravo", 0, "{}", NOW), ("c2", "Casey Delta", 0, "{}", NOW)])
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _parent(self, parent_id: str, *candidates: str) -> None:
        self.conn.executemany(
            "INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) VALUES (?, ?, 'human', NULL, ?)",
            [(candidate, parent_id, LATER) for candidate in candidates])
        self.conn.commit()

    def _facts(self, candidate_id: str, name: str) -> None:
        facts = SynthesizedFacts(canonical_name=name, aliases=(), employers=(), title="", school="", field_of_study="",
                                 location="", relationship_to_owner="", relationship_category="", topics=(),
                                 notable_events=(), identifiers=(), owned_identifiers=OwnedIdentifiers((), (), ()),
                                 shared_context=(), confidence=0.9, is_owner=False)
        self.conn.execute(
            "INSERT INTO facts (candidate_id, facts_json, input_fingerprint, model, reasoning_effort, synthesized_at) "
            "VALUES (?, ?, 'f', 'm', 'low', ?)", (candidate_id, json.dumps(facts.to_payload()), NOW))
        self.conn.commit()


class FamilyTagsTests(StoreTests):
    def test_a_merged_family_keeps_every_members_human_word(self) -> None:
        # Two people the human tagged separately: one labelled family (older), one later cleared.
        self._parent("p:a", "c1")
        self._parent("p:b", "c2")
        self.conn.executemany("INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES (?, ?, ?, ?)",
                              [("c1", "is_family", "landlord", NOW), ("c2", "", "", LATER)])
        self.conn.commit()
        # Dedupe merges them: the family's tags are the union, so the label stands.
        self._parent("li:42", "c1", "c2")
        held = TagStore(self.conn).load()
        self.assertEqual(set(held), {"li:42"})
        self.assertEqual(held["li:42"].tags, frozenset({"is_family"}))
        self.assertEqual(queries_share.current_tags(self.conn)["li:42"].tags, "is_family")

    def test_tags_from_two_members_add_up(self) -> None:
        self._parent("li:42", "c1", "c2")
        self.conn.executemany("INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES (?, ?, ?, ?)",
                              [("c1", "is_family", "", NOW), ("c2", "is_recruiter", "note", LATER)])
        self.conn.commit()
        held = queries_share.current_tags(self.conn)["li:42"]
        self.assertEqual(held.tags, "is_family|is_recruiter")
        self.assertEqual((held.note, held.updated_at), ("note", LATER))


class LookupTests(StoreTests):
    def test_a_typed_name_matches_one_written_name_not_words_pooled_across_the_family(self) -> None:
        self._parent("li:42", "c1", "c2")
        self._facts("c1", "Jordan Bravo")
        self._facts("c2", "Casey Delta")
        self.assertEqual([match.parent_id for match in matches(self.conn, name="Jordan Bravo")], ["li:42"])
        self.assertEqual([match.parent_id for match in matches(self.conn, name="casey")], ["li:42"])
        self.assertEqual(matches(self.conn, name="Jordan Delta"), [])


class PeopleWorthTests(StoreTests):
    def setUp(self) -> None:
        super().setUp()
        from packs.ingestion.primitives.share.person_labels import PersonLabels
        from packs.ingestion.primitives.share.web.model import SharePeople
        from packs.ingestion.primitives.share.web.server import ShareRoutes
        from unittest.mock import Mock

        self.root = Path(self.tmp.name)
        self._parent("p:a", "c1", "c2")
        self._facts("c1", "Jordan Bravo")
        self.conn.executemany("INSERT INTO candidate_sources VALUES (?, 'gmail_msgvault')", [("c1",), ("c2",)])
        self.conn.executemany("INSERT INTO candidate_identifiers VALUES (?, 'email', ?, ?)",
                              [(candidate, f"{candidate}@example.com", f"{candidate}@example.com")
                               for candidate in ("c1", "c2")])
        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, input_fingerprint, created_at) "
                          "VALUES ('c1', 'maybe', 'machine', 'uncertain', 'v1', ?)", (NOW,))
        self.conn.commit()
        PersonLabels(self.conn, self.root).run()
        queries_share.write_tags(self.conn, [("c1", "is_recruiter", "keep note", NOW)])
        self.people = SharePeople(self.conn, self.root)
        self.routes = ShareRoutes(self.conn, self.people, self.people.load, Mock())

    def post(self, path: str, payload: object):
        from test_logbook_people import Handler
        from urllib.parse import urlparse
        handler = Handler(body=json.dumps(payload).encode())
        self.assertTrue(self.routes.post(handler, urlparse(path)))
        return handler.response_status, json.loads(handler.wfile.getvalue())

    def test_worth_write_returns_human_worth_for_the_whole_family_and_keeps_tags(self) -> None:
        from dataclasses import asdict
        initial, = self.people.load()
        # The machine's "maybe" reaches the page as its own call; only the human writes yes or no.
        self.assertEqual((initial.worth, initial.worth_source, initial.reason), ("maybe", "machine", "worth_maybe"))
        self.assertNotIn("share", asdict(initial))
        self.assertNotIn("share_source", asdict(initial))
        labels = [tuple(row) for row in self.conn.execute("SELECT * FROM person_labels")]
        for choice in ("yes", "no"):
            code, payload = self.post("/api/people/worth", {"ids": ["p:a"], "worth": choice})
            self.assertEqual(code, 200)
            updated, = payload["rows"]
            self.assertEqual((updated["worth"], updated["worth_source"], updated["reason"]), (choice, "human", "human_worth"))
            self.assertEqual(updated["tags"], ["is_recruiter"])
            self.assertNotIn("share", updated)
            human = self.conn.execute("SELECT candidate_id, worth, reason, labels_json, input_fingerprint FROM worth "
                                      "WHERE decided_by = 'human' ORDER BY seq DESC LIMIT 2").fetchall()
            self.assertEqual({tuple(row) for row in human}, {(c, choice, "human", None, None) for c in ("c1", "c2")})
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM person_labels")], labels)
        self.assertEqual(queries_share.current_tags(self.conn)["p:a"].note, "keep note")

    def test_tags_keep_their_route_and_cannot_write_retired_decisions(self) -> None:
        code, payload = self.post("/api/people/tags", {"people": [{"parent_id": "p:a", "tags": ["is_family"]}]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["rows"][0]["tags"], ["is_family"])
        self.assertEqual(payload["rows"][0]["worth_source"], "machine")
        for tag in ("share", "private"):
            self.assertEqual(self.post("/api/people/tags", {"people": [{"parent_id": "p:a", "tags": [tag]}]})[0], 400)

    def test_invalid_worth_requests_do_not_write(self) -> None:
        count = self.conn.execute("SELECT COUNT(*) FROM worth").fetchone()[0]
        for request in ({"ids": [], "worth": "yes"}, {"ids": ["missing"], "worth": "yes"},
                        {"ids": ["p:a"], "worth": "maybe"}, {"ids": [1], "worth": "no"}, []):
            self.assertEqual(self.post("/api/people/worth", request)[0], 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worth").fetchone()[0], count)

    def test_human_relationship_survives_label_refresh_and_tag_edits(self) -> None:
        from packs.ingestion.primitives.share.person_labels import PersonLabels

        before = [tuple(row) for row in self.conn.execute("SELECT * FROM person_labels")]
        code, payload = self.post("/api/people/relationship", {"parent_id": "p:a", "relationship_kind": "close_friend"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["rows"][0]["relationship_kind"], "close_friend")
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM person_labels")], before)
        self.assertEqual(self.people.detail("p:a").relationship_kind_source, "human")
        self.assertNotIn("relationship_kind", self.people.detail("p:a").choice_p)
        self.assertEqual([tuple(row) for row in self.conn.execute(
            "SELECT candidate_id, relationship_kind FROM person_tags ORDER BY candidate_id")],
            [("c1", "close_friend"), ("c2", "close_friend")])
        self.post("/api/people/tags", {"people": [{"parent_id": "p:a", "tags": ["is_family"]}]})
        PersonLabels(self.conn, self.root).run()
        self.assertEqual(self.people.load()[0].relationship_kind, "close_friend")
        self.assertEqual(queries_share.current_tags(self.conn)["p:a"].note, "keep note")

    def test_relationship_controls_effective_flag_and_upload_labels(self) -> None:
        from packs.ingestion.primitives.share.store import share_rows

        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, input_fingerprint, created_at) "
                          "VALUES ('c1', 'yes', 'machine', 'old', 'v2', ?)", (LATER,))
        self.conn.execute("UPDATE person_labels SET flag = 'family', labels_json = ?",
                          (json.dumps({"relationship_kind": "family", "relationship_kind_p": 0.9, "is_family": 0.9}),))
        self.conn.commit()
        self.assertEqual(self.people.detail("p:a").relationship_kind_source, "machine")
        self.assertEqual(share_rows(self.root / "store.sqlite"), ())
        for kind, flag in (("colleague", ""), ("family", "family"), ("romantic_partner", "romantic_partner")):
            self.assertEqual(self.post("/api/people/relationship", {"parent_id": "p:a", "relationship_kind": kind})[0], 200)
            self.assertEqual(self.people.load()[0].flag, flag)
            uploaded = share_rows(self.root / "store.sqlite")
            self.assertEqual(len(uploaded), int(not flag))
            if uploaded:
                self.assertEqual(json.loads(uploaded[0].labels_json)["relationship_kind"], kind)
        self.conn.execute("UPDATE person_labels SET labels_json = ?", (json.dumps({"is_minor": 0.9}),))
        self.conn.commit()
        self.post("/api/people/relationship", {"parent_id": "p:a", "relationship_kind": "colleague"})
        self.assertEqual(self.people.load()[0].flag, "minor")
        self.assertEqual(share_rows(self.root / "store.sqlite"), ())

    def test_relationship_keeps_the_latest_family_note(self) -> None:
        queries_share.write_tags(self.conn, [("c2", "is_family", "latest note", LATER)])
        self.post("/api/people/relationship", {"parent_id": "p:a", "relationship_kind": "friend"})
        held = queries_share.current_tags(self.conn)["p:a"]
        self.assertEqual((set(held.tags.split("|")), held.note), ({"is_recruiter", "is_family"}, "latest note"))

    def test_relationship_validation_does_not_write(self) -> None:
        for request in ({"parent_id": "p:a", "relationship_kind": "invalid"},
                        {"parent_id": "missing", "relationship_kind": "family"},
                        {"parent_id": "p:a"}, [], {"parent_id": 1, "relationship_kind": "family"}):
            self.assertEqual(self.post("/api/people/relationship", request)[0], 400)

    def test_unjudged_worth_and_source_are_empty(self) -> None:
        self.conn.execute("DELETE FROM worth")
        self.conn.commit()
        row, = self.people.load()
        self.assertEqual((row.worth, row.worth_source, row.reason), ("", "", "unjudged"))

    def test_changed_profile_keeps_in_progress_and_blocks_writes_until_labels_refresh(self) -> None:
        from packs.ingestion.primitives.deep_context_v2.db import queries_review
        from packs.ingestion.primitives.share.person_labels import PersonLabels
        queries_review.insert_linkedin(self.conn, "c1", "https://www.linkedin.com/in/jordan-bravo",
                                       "4242", "human_override", "confirmed", "f1", NOW)
        self._parent("li:4242", "c1", "c2")
        self.conn.commit()
        self.assertTrue(self.people.load()[0].in_progress)
        self.assertEqual(self.post("/api/people/worth", {"ids": ["li:4242"], "worth": "yes"})[0], 409)
        PersonLabels(self.conn, self.root).run()
        self.assertFalse(self.people.load()[0].in_progress)
        self.assertEqual(self.post("/api/people/worth", {"ids": ["li:4242"], "worth": "yes"})[0], 200)

    def test_real_http_rows_and_worth_write_use_the_same_contract(self) -> None:
        import threading
        import urllib.request
        from http.server import HTTPServer
        from packs.ingestion.primitives.share.web.server import make_handler

        server = HTTPServer(("127.0.0.1", 0), make_handler(self.routes))
        self.addCleanup(server.server_close)
        # Serve each request on the store's owning thread.
        url = f"http://127.0.0.1:{server.server_port}/api/people/"
        answers = []
        def request() -> None:
            body = json.dumps({"ids": ["p:a"], "worth": "yes"}).encode()
            with urllib.request.urlopen(urllib.request.Request(url + "worth", data=body), timeout=5) as response:
                answers.append(json.loads(response.read()))
            with urllib.request.urlopen(url + "rows", timeout=5) as response:
                answers.append(json.loads(response.read()))
            relationship = json.dumps({"parent_id": "p:a", "relationship_kind": "colleague"}).encode()
            with urllib.request.urlopen(urllib.request.Request(url + "relationship", data=relationship), timeout=5) as response:
                answers.append(json.loads(response.read()))
            with urllib.request.urlopen(url + "person?id=p%3Aa", timeout=5) as response:
                answers.append(json.loads(response.read()))
        client = threading.Thread(target=request)
        client.start()
        for _ in range(4):
            server.handle_request()
        client.join(5)
        updated = answers[0]["rows"][0]
        payload = answers[1]
        row = dict(zip(payload["columns"], payload["rows"][0]))
        self.assertEqual(row, updated)
        self.assertEqual((row["worth"], row["worth_source"]), ("yes", "human"))
        self.assertNotIn("share", payload["columns"])
        self.assertEqual(answers[2]["rows"][0]["relationship_kind"], "colleague")
        self.assertEqual(answers[3]["relationship_kind_source"], "human")
        self.assertNotIn("relationship_kind", answers[3]["choice_p"])

    def _family_rows(self, candidates: tuple[str, ...], parent_id: str) -> dict[str, int]:
        """table -> rows naming one of the candidates or the parent, for every table keyed by either."""
        found: dict[str, int] = {}
        for (table,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'sqlite_sequence'"):
            columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            keys = [column for column in ("candidate_id", "candidate_a", "candidate_b") if column in columns]
            clauses = [f"{column} IN ({', '.join('?' * len(candidates))})" for column in keys]
            params: list[str] = [value for _ in keys for value in candidates]
            if "parent_id" in columns:
                clauses.append("parent_id = ?")
                params.append(parent_id)
            if clauses:
                found[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {' OR '.join(clauses)}", params).fetchone()[0]
        return found

    def _seed_every_table(self, candidates: tuple[str, str], parent_id: str) -> None:
        first, second = candidates
        self.conn.executemany("INSERT INTO candidate_names VALUES (?, ?)", [(c, f"name {c}") for c in candidates])
        self.conn.executemany("INSERT INTO bundles VALUES (?, '{}', 'f', ?)", [(c, NOW) for c in candidates])
        self.conn.execute("INSERT INTO pair_verdicts VALUES (?, ?, 's', 1, 0.9, 'same', ?)", (first, second, NOW))
        self.conn.execute("INSERT INTO candidate_linkedins (candidate_id, linkedin_url, member_id, origin, verdict, decided_by, "
                          "judgment_fingerprint, confidence, reason, created_at) VALUES "
                          "(?, 'https://www.linkedin.com/in/example', '7', 'research', 'needs_review', 'machine', 'j', 0.5, '', ?)",
                          (first, NOW))
        self.conn.execute("INSERT INTO research VALUES (?, ?, 'failed', NULL, ?)", (f"h-{parent_id}", parent_id, NOW))
        self.conn.execute("INSERT INTO review_queue VALUES (?, 'detach', '', '', ?)", (parent_id, NOW))
        self.conn.commit()

    def test_delete_removes_every_row_of_one_family_and_none_of_the_other(self) -> None:
        from packs.ingestion.primitives.share.person_labels import PersonLabels
        queries.upsert_candidates(self.conn, [("c3", "Riley Echo", 0, "{}", NOW), ("c4", "Riley E", 0, "{}", NOW)])
        self._parent("p:b", "c3", "c4")
        self._facts("c3", "Riley Echo")
        self.conn.executemany("INSERT INTO candidate_sources VALUES (?, 'imessage')", [("c3",), ("c4",)])
        self.conn.executemany("INSERT INTO candidate_identifiers VALUES (?, 'phone', ?, ?)",
                              [("c3", "+15550100", "+15550100"), ("c4", "+15550101", "+15550101")])
        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, input_fingerprint, created_at) "
                          "VALUES ('c3', 'yes', 'machine', 'close', 'v1', ?)", (NOW,))
        self.conn.commit()
        PersonLabels(self.conn, self.root).run()
        queries_share.write_tags(self.conn, [("c3", "is_family", "", NOW)])
        self._seed_every_table(("c1", "c2"), "p:a")
        self._seed_every_table(("c3", "c4"), "p:b")
        self.conn.execute("INSERT INTO pair_verdicts VALUES ('c1', 'c3', 's', 0, 0.9, 'different', ?)", (NOW,))
        self.conn.commit()
        self.assertEqual({row.parent_id for row in self.people.load()}, {"p:a", "p:b"})
        kept = self._family_rows(("c3", "c4"), "p:b")
        deleted = self._family_rows(("c1", "c2"), "p:a")
        keyed = {table for table, count in deleted.items() if count}
        self.assertTrue({"candidates", "candidate_names", "candidate_identifiers", "candidate_sources", "bundles", "facts",
                         "candidate_parent", "pair_verdicts", "worth", "candidate_linkedins", "research", "person_tags",
                         "person_labels", "review_queue"} <= keyed)

        self.assertEqual(self.post("/api/people/delete", {"ids": ["p:a"]}), (200, {"deleted": 1}))

        self.assertEqual(set(self._family_rows(("c1", "c2"), "p:a").values()), {0})
        kept["pair_verdicts"] -= 1  # the one verdict that paired c1 with c3
        self.assertEqual(self._family_rows(("c3", "c4"), "p:b"), kept)
        self.assertEqual(set(self.people.export_rows()), {"p:b"})
        self.assertIsNone(self.people.detail("p:a"))
        self.assertEqual([row.parent_id for row in self.people.load()], ["p:b"])

    def test_delete_of_an_unknown_id_is_a_conflict_and_deletes_nothing(self) -> None:
        before = self._family_rows(("c1", "c2"), "p:a")
        self.assertEqual(self.post("/api/people/delete", {"ids": ["p:a", "p:missing"]})[0], 409)
        for request in ({"ids": []}, {"ids": [1]}, []):
            self.assertEqual(self.post("/api/people/delete", request)[0], 400)
        self.assertEqual(self._family_rows(("c1", "c2"), "p:a"), before)


class WorthMigrationTests(StoreTests):
    def test_schema_five_adds_relationship_without_changing_existing_rows(self) -> None:
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, note, updated_at) "
                          "VALUES ('c1', 'is_family', 'keep note', ?)", (NOW,))
        self.conn.execute("ALTER TABLE person_tags DROP COLUMN relationship_kind")
        self.conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
        self.conn.commit()
        self.conn.close()
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        self.assertEqual(tuple(self.conn.execute("SELECT candidate_id, tags, note, updated_at, relationship_kind "
                                                "FROM person_tags").fetchone()),
                         ("c1", "is_family", "keep note", NOW, None))

    def test_schema_four_drops_derived_share_and_only_the_retired_tags(self) -> None:
        from packs.ingestion.primitives.deep_context_v2.db.schema import SCHEMA_VERSION
        self.conn.execute("CREATE TABLE share (candidate_id TEXT PRIMARY KEY, share TEXT)")
        self.conn.execute("CREATE VIEW current_share AS SELECT * FROM share")
        self.conn.execute("INSERT INTO share VALUES ('c1', 'yes')")
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES ('c1', 'is_family|private|share|is_recruiter', 'keep note', ?)", (NOW,))
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES ('c2', 'private|share', '', ?)", (LATER,))
        self.conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        self.conn.commit()
        self.conn.close()
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], str(SCHEMA_VERSION))
        self.assertEqual(self.conn.execute("SELECT name FROM sqlite_master WHERE name IN ('share', 'current_share')").fetchall(), [])
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT tags, note, updated_at FROM person_tags ORDER BY candidate_id")],
                         [("is_family|is_recruiter", "keep note", NOW), ("", "", LATER)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 2)
        self.conn.close()
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM person_tags").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
