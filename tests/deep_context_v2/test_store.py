"""Real SQLite constraints, latest/human precedence, and node transactions."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_share
from packs.ingestion.primitives.deep_context_v2.db.owner import load_owner, owner_background_block, owner_from_payload, read_owner
from packs.ingestion.primitives.deep_context_v2.db.schema import SCHEMA_VERSION, TABLES, VIEWS
from packs.ingestion.primitives.deep_context_v2.db.store import StoreError, now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import MANIFEST_RELATIVE_DIR, Node

NOW = "2026-10-07T22:00:00Z"


class StoreFixture:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = store_path(self.root)
        self.conn = open_store(self.path)
        self.addCleanup(self.conn.close)
        queries.upsert_candidates(self.conn, [("a", "Jordan Bravo", 0, "{}", NOW), ("b", "Casey Delta", 0, "{}", NOW)])
        self.conn.executemany("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES (?, 'p:1','singleton',?)", [("a", NOW), ("b", NOW)])
        self.conn.commit()


class StoreTests(StoreFixture, unittest.TestCase):
    def test_schema_tables_views_and_reopen(self) -> None:
        objects = dict(self.conn.execute("SELECT name,type FROM sqlite_master"))
        self.assertTrue(all(objects[name] == "table" for name in TABLES))
        self.assertTrue(all(objects[name] == "view" for name in VIEWS))
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn = open_store(self.path, shared=True)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
        self.assertTrue(now_iso().endswith("Z"))

    def test_wrong_or_absent_version_is_refused(self) -> None:
        self.conn.execute("UPDATE meta SET value='999'")
        self.conn.commit()
        with self.assertRaises(StoreError):
            open_store(self.path)
        self.conn.execute("DELETE FROM meta")
        self.conn.commit()
        with self.assertRaises(StoreError):
            open_store(self.path)

    def test_constraint_rejects_invalid_boundary_rows(self) -> None:
        cases = [
            ("INSERT INTO candidates VALUES ('bad','',2,'{}',?)", (NOW,)),
            ("INSERT INTO candidates VALUES ('bad','',0,'not-json',?)", (NOW,)),
            ("INSERT INTO candidate_identifiers VALUES ('missing','email','a','a')", ()),
            ("INSERT INTO candidate_identifiers VALUES ('a','fax','a','a')", ()),
            ("INSERT INTO candidate_sources VALUES ('a','linkedin')", ()),
            ("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES ('a','p:2','unknown',?)", (NOW,)),
            ("INSERT INTO pair_verdicts VALUES ('b','a','f',1,.9,'',?)", (NOW,)),
            ("INSERT INTO pair_verdicts VALUES ('a','b','f',2,.9,'',?)", (NOW,)),
            ("INSERT INTO worth(candidate_id,worth,decided_by,reason,created_at) VALUES ('a','yes','machine','',?)", (NOW,)),
            ("INSERT INTO worth(candidate_id,worth,decided_by,reason,created_at) VALUES ('a','unknown','human','',?)", (NOW,)),
            ("INSERT INTO research VALUES ('h','p:1','complete',NULL,?)", (NOW,)),
            ("INSERT INTO research VALUES ('h','p:1','no_match','bad-json',?)", (NOW,)),
        ]
        for sql, params in cases:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, params)

    def linkedin(self, candidate: str, verdict: str, decider: str, *, member: str = "42", url: str = "https://www.linkedin.com/in/jordan-bravo", origin: str = "research") -> None:
        self.conn.execute("INSERT INTO candidate_linkedins(candidate_id,linkedin_url,member_id,origin,verdict,decided_by,judgment_fingerprint,reason,created_at) VALUES (?,?,?,?,?,?,?,'',?)", (candidate, url, member, origin, verdict, decider, "fp", NOW))

    def test_synthetic_is_human_only_and_origin_matches_key(self) -> None:
        for decider, url, origin in [("machine", "synthetic:h", "synthetic"), ("human", "https://www.linkedin.com/in/jordan-bravo", "synthetic"), ("human", "synthetic:h", "research")]:
            with self.subTest(decider=decider, url=url), self.assertRaises(sqlite3.IntegrityError):
                self.linkedin("a", "confirmed", decider, url=url, origin=origin)
        self.linkedin("a", "confirmed", "human", url="synthetic:h", member="synthetic:h", origin="synthetic")
        self.assertEqual(self.conn.execute("SELECT profile_key FROM current_profile").fetchone()[0], "synthetic:h")

    def test_parent_latest_wins_even_after_human_row(self) -> None:
        self.conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES ('a','li:42','human',?)", (NOW,))
        self.conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES ('a','p:2','singleton',?)", (NOW,))
        self.assertEqual(self.conn.execute("SELECT parent_id FROM current_parent WHERE candidate_id='a'").fetchone()[0], "p:2")

    def test_worth_human_beats_later_machine_and_latest_human_wins(self) -> None:
        for candidate, worth, decider in [("a", "no", "human"), ("b", "yes", "machine"), ("b", "maybe", "human")]:
            self.conn.execute("INSERT INTO worth(candidate_id,worth,decided_by,reason,input_fingerprint,created_at) VALUES (?,?,?,'','fp',?)", (candidate, worth, decider, NOW))
            expected = "maybe" if worth == "maybe" else "no"
            self.assertEqual(self.conn.execute("SELECT worth FROM current_worth").fetchone()[0], expected)

    def test_linkedin_human_beats_machine_and_profile_needs_matching_parent(self) -> None:
        self.linkedin("a", "wrong_person", "human")
        self.linkedin("a", "confirmed", "machine")
        self.assertEqual(self.conn.execute("SELECT verdict FROM current_linkedins").fetchone()[0], "wrong_person")
        self.linkedin("b", "confirmed", "machine")
        self.assertIsNone(self.conn.execute("SELECT profile_key FROM current_profile").fetchone()[0])
        self.conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES ('b','li:42','judge_confirmed',?)", (NOW,))
        self.assertEqual(self.conn.execute("SELECT profile_key FROM current_profile WHERE parent_id='li:42'").fetchone()[0], "https://www.linkedin.com/in/jordan-bravo")

    def test_upserts_preserve_unique_identifiers_sources_and_connections(self) -> None:
        queries.upsert_candidates(self.conn, [("a", "Jordan Updated", 1, '{"n":1}', NOW)])
        ids = [("a", "email", "jordan@example.com", "Jordan@example.com")]
        sources = [("a", "imessage")]
        for _ in range(2):
            queries.insert_candidate_identifiers(self.conn, ids)
            queries.insert_candidate_sources(self.conn, sources)
            queries.upsert_connections(self.conn, [("url", "Jordan", None, "Founder", "Example", NOW)])
            queries.upsert_bundle(self.conn, "b", '{"messages":[]}', "fp", NOW)
            queries.upsert_facts(self.conn, "b", "{}", "fp", "fake", "none", NOW)
        self.assertEqual(queries.display_names(self.conn)["a"], "Jordan Updated")
        self.assertEqual(queries.channels_present(self.conn), {"imessage"})
        self.assertEqual([p.person_id for p in queries.candidates_to_collect(self.conn, 1)], ["b"])
        self.assertEqual(queries.count_bundles(self.conn), 1)
        self.assertEqual(queries.bundles_with_facts_fingerprint(self.conn)[0].facts_fingerprint, "fp")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_identifiers").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM connections").fetchone()[0], 1)
        self.assertEqual(queries._batches([1, 2, 3], size=2), [[1, 2], [3]])
        self.assertEqual(queries._batches([]), [])

    def test_share_replacement_is_atomic_and_preserves_tags(self) -> None:
        tags = [("a", "friend", "note", NOW)]
        human = [("a", "jordan", "yes", "human choice", "", "human", NOW)]
        queries_share.decide_share(self.conn, tags, human)
        self.assertEqual(queries_share.current_tags(self.conn)["p:1"].tags, "friend")
        self.assertEqual(queries_share.current_share(self.conn)[0].source, "human")
        labels = [("b", "casey", "Casey", "yes", "", "{}", NOW)]
        machine = [("b", "casey", "no", "machine choice", "", "machine", NOW)]
        queries_share.replace_share(self.conn, labels, machine)
        self.assertEqual(set(queries_share.labels_by_candidate(self.conn)), {"b"})
        self.assertEqual(queries_share.current_share(self.conn)[0].share, "no")
        self.assertEqual(queries_share.current_tags(self.conn)["p:1"].note, "note")
        with self.assertRaises(sqlite3.IntegrityError):
            queries_share.replace_share(self.conn, [], [("missing", "x", "yes", "", "", "machine", NOW)])
        self.assertEqual(queries_share.current_share(self.conn)[0].share, "no")
        self.assertEqual(set(queries_share.labels_by_candidate(self.conn)), {"b"})

    def test_share_human_precedence_and_tag_tie_is_stable(self) -> None:
        queries_share.decide_share(self.conn, [("b", "second", "", NOW), ("a", "first", "", NOW)], [("a", "jordan", "yes", "", "", "human", NOW), ("b", "casey", "no", "", "", "machine", "2099")])
        self.assertEqual(queries_share.current_tags(self.conn)["p:1"].candidate_id, "a")
        self.assertEqual(queries_share.current_share(self.conn)[0].share, "yes")

    def test_owner_projection_normalizes_and_renders_dates(self) -> None:
        payload = {"name": " Jordan Bravo ", "emails": ["Jordan@Example.com", "j@example.com"], "phones": ["+1 (555) 010-0000"], "work": [{"company": "Example", "title": "Founder", "start": 2020, "end": 0}, {"company": "Other", "end": 2019}], "education": [{"school": "Example U", "note": "CS"}], "locations": ["Springfield"], "notes": "friend"}
        path = self.root / "owner.json"
        path.write_text(json.dumps(payload))
        owner = load_owner(self.conn, path)
        self.assertEqual(read_owner(self.conn), owner)
        self.assertEqual(owner.emails[0], "jordan@example.com")
        self.assertEqual(owner.phones, ("+15550100000",))
        rendered = owner_background_block(owner)
        self.assertIn("2020-present", rendered)
        self.assertIn("until 2019", rendered)
        self.assertIn("dates unknown", rendered)
        self.assertIn("(CS)", rendered)
        self.assertIn("example.com", rendered)
        self.assertIn("Notes: friend", rendered)
        self.assertEqual(owner_from_payload({}).emails, ())


class WriteNode(Node):
    name = "unit"
    reads = ()
    writes = ("bundles",)

    def execute(self) -> dict[str, int]:
        queries.upsert_bundle(self.conn, "a", "{}", "fp", NOW)
        return {"bundles": 1}


class NodeTests(StoreFixture, unittest.TestCase):
    def test_node_commits_and_records_manifest(self) -> None:
        manifest = WriteNode(self.conn, self.root).run()
        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.counts, {"bundles": 1})
        saved = json.loads((self.root / MANIFEST_RELATIVE_DIR / "unit.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(queries.count_bundles(self.conn), 1)

    def test_node_denies_undeclared_reads_and_rolls_back_partial_work(self) -> None:
        class BadNode(WriteNode):
            def execute(self) -> dict[str, int]:
                super().execute()
                self.conn.execute("SELECT * FROM connections").fetchall()
                return {}
        with self.assertRaises(sqlite3.DatabaseError):
            BadNode(self.conn, self.root).run()
        self.assertEqual(queries.count_bundles(self.conn), 0)
        saved = json.loads((self.root / MANIFEST_RELATIVE_DIR / "unit.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertIn("DatabaseError", saved["error"])
        self.conn.execute("SELECT * FROM connections").fetchall()  # Authorizer was removed.

    def test_node_denies_undeclared_writes(self) -> None:
        class BadNode(WriteNode):
            def execute(self) -> dict[str, int]:
                self.conn.execute("DELETE FROM candidates")
                return {}
        with self.assertRaises(sqlite3.DatabaseError):
            BadNode(self.conn, self.root).run()
        self.assertEqual(len(queries.display_names(self.conn)), 2)

    def test_missing_required_files_does_not_execute(self) -> None:
        class MissingNode(WriteNode):
            def required_files(self) -> tuple[Path, ...]:
                return (self.data_root / "missing.csv",)
        manifest = MissingNode(self.conn, self.root).run()
        self.assertEqual(manifest.status, "not_ready")
        self.assertIn("missing.csv", manifest.error)
        self.assertEqual(queries.count_bundles(self.conn), 0)

    def test_declared_views_and_metadata_are_readable(self) -> None:
        class ReadNode(WriteNode):
            reads = ("current_profile",)
            writes = ()
            def execute(self) -> dict[str, int]:
                self.conn.execute("SELECT * FROM meta").fetchall()
                return {"profiles": len(self.conn.execute("SELECT * FROM current_profile").fetchall())}
        self.assertEqual(ReadNode(self.conn, self.root).run().counts, {"profiles": 1})

    def test_node_subclass_must_declare_io(self) -> None:
        with self.assertRaisesRegex(TypeError, "name"):
            class MissingDeclaration(Node):
                def execute(self) -> dict[str, int]:
                    return {}


if __name__ == "__main__":
    unittest.main()
