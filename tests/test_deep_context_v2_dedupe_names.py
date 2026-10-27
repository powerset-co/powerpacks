"""Name recall sends candidates to the judge without deciding identity."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, Person
from packs.ingestion.primitives.deep_context_v2.db import queries, queries_dedupe
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import Dedupe
from packs.ingestion.primitives.deep_context_v2.names import names_for_matching, written_names_can_match
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts

NOW = "2026-10-20T00:00:00Z"


class DedupeNameRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        queries.upsert_owner(self.conn, json.dumps({"name": "Synthetic Owner"}), "owner", NOW)

    def add(self, candidate: str, written: str, canonical: str) -> None:
        queries.upsert_candidates(self.conn, [(candidate, written, 0, "{}", NOW)])
        self.conn.execute(
            "INSERT INTO candidate_identifiers VALUES (?, 'email', ?, ?)",
            (candidate, f"{candidate}@example.com", f"{candidate}@example.com"))
        facts = SynthesizedFacts(canonical, (), (), "", "", "", "", "friend", "personal",
                                 (), (), (), OwnedIdentifiers((), (), ()), (), 0.9, False)
        queries.upsert_facts(self.conn, candidate, json.dumps(facts.to_payload()), candidate, "synthetic", "low", NOW)
        bundle = CollectionBundle.of(Person(candidate, written, (), (), ()), messages=[], groups=[],
                                     thread_participants=(), available=0)
        self.conn.execute("INSERT INTO bundles VALUES (?, ?, ?, ?)",
                          (candidate, json.dumps(bundle.to_payload()), candidate, NOW))

    def node(self) -> Dedupe:
        return Dedupe(self.conn, self.root, limit=100)

    def test_dirty_written_names_share_canonical_name_and_keep_written_names_in_prompt(self) -> None:
        for candidate, written in (("a", "Jordan Bravo Photography"), ("b", "jordanbravophoto"),
                                   ("c", "Jordan Bravo")):
            self.add(candidate, written, "Jordan Bravo")
        node = self.node()
        self.assertEqual([(a, b) for a, b, _ in node.plan().todo], [("a", "b"), ("a", "c"), ("b", "c")])
        prompt = node.prompts(node.plan().todo[:1])[0]
        self.assertIn("Jordan Bravo Photography", prompt)
        self.assertIn("jordanbravophoto", prompt)
        self.assertIn("dossier name: Jordan Bravo", prompt)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_parent").fetchone()[0], 0)

    def test_written_name_remains_an_alternative_to_a_different_canonical_name(self) -> None:
        self.add("a", "Jordan Bravo root of trouble", "Jordan Bravo")
        self.add("b", "Jordan Bravo", "Jordan Bravo")
        self.add("c", "Jordan Bravo root of trouble", "Casey Delta")
        self.assertEqual([(a, b) for a, b, _ in self.node().plan().todo],
                         [("a", "b"), ("a", "c")])

    def test_missing_middle_name_and_surname_particles_or_punctuation_reach_judge(self) -> None:
        for first, second in (("Jordan Quincy Al-Bravo", "Jordan Albravo"),
                              ("Jordan Quincy Al Bravo", "Jordan Albravo"),
                              ("Jordan Van Der Bravo", "Jordan Vanderbravo"),
                              ("Jordan O'Bravo", "Jordan Obravo")):
            with self.subTest(first=first, second=second):
                self.assertTrue(any(written_names_can_match(a, b) for a in names_for_matching(first, "")
                                    for b in names_for_matching(second, "")))
        self.add("a", "Jordan Quincy Al-Bravo", "")
        self.add("b", "Jordan Albravo", "")
        self.assertEqual([(a, b) for a, b, _ in self.node().plan().todo], [("a", "b")])

    def test_original_surname_forms_remain_available(self) -> None:
        for index, written in enumerate(("Jordan van der Bravo", "Jordan Al-Bravo", "Jordan Smith-Bravo")):
            with self.subTest(written=written):
                a, b = f"a{index}", f"b{index}"
                self.add(a, written, "")
                self.add(b, "Jordan Bravo", "")
                self.assertIn((a, b), [(a, b) for a, b, _ in self.node().plan().todo])

    def test_same_name_still_requires_judge_and_saved_different_verdict_is_reused(self) -> None:
        self.add("a", "Jordan Bravo", "Jordan Bravo")
        self.add("b", "Jordan Bravo", "Jordan Bravo")
        first = self.node()
        self.assertEqual(len(first.plan().todo), 1)
        queries_dedupe.insert_pair_verdict(self.conn, ("a", "b", first.signature("a", "b"), 0, 1.0,
                                                       "Synthetic distinct identities", NOW))
        rerun = self.node()
        self.assertEqual(rerun.plan().todo, [])
        self.assertEqual(rerun.plan().already_judged, 1)
        with patch.object(rerun, "_judge_all", return_value={}) as judge:
            rerun.execute()
        self.assertEqual(judge.call_args.args[0], [])
        self.assertNotEqual(queries_dedupe.current_parents(self.conn)["a"],
                            queries_dedupe.current_parents(self.conn)["b"])

    def test_deleted_family_is_not_considered(self) -> None:
        self.add("a", "Jordan Bravo Photography", "Jordan Bravo")
        self.add("b", "Jordan Bravo", "Jordan Bravo")
        queries_dedupe.append_parent_rows(self.conn, [("a", "p:deleted", "singleton", None, NOW)])
        self.conn.execute("INSERT INTO deleted_parents VALUES (?, ?)", ("p:deleted", NOW))
        self.assertEqual(self.node().plan().todo, [])

    def test_cached_same_verdict_merges_once_and_preserves_human_tags(self) -> None:
        self.add("a", "Jordan Bravo Photography", "Jordan Bravo")
        self.add("b", "jordanbravophoto", "Jordan Bravo")
        self.conn.execute("INSERT INTO person_tags (candidate_id, tags, relationship_kind, note, updated_at) VALUES (?, ?, ?, ?, ?)",
                          ("a", "friend", "friend", "human note", NOW))
        first = self.node()
        queries_dedupe.insert_pair_verdict(self.conn, ("a", "b", first.signature("a", "b"), 1, 1.0,
                                                       "Synthetic saved identity evidence", NOW))
        with patch.object(Dedupe, "_judge_all", return_value={}) as judge:
            self.node().execute()
            rows = self.conn.execute("SELECT count(*) FROM candidate_parent").fetchone()[0]
            self.node().execute()
        self.assertTrue(all(call.args[0] == [] for call in judge.call_args_list))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_parent").fetchone()[0], rows)
        self.assertEqual(queries_dedupe.current_parents(self.conn)["a"],
                         queries_dedupe.current_parents(self.conn)["b"])
        self.assertEqual(tuple(self.conn.execute(
            "SELECT candidate_id, tags, relationship_kind, note, updated_at FROM person_tags").fetchone()),
                         ("a", "friend", "friend", "human note", NOW))


if __name__ == "__main__":
    unittest.main()
