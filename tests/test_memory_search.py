"""The one search: owner labels as layers, people ranked with evidence, warmth as the tie-breaker."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from test_memory import STORIES, FixtureProvider, source_db
from packs.memory.__main__ import main
from packs.memory.generate import Generate
from packs.memory.models import Claim, Kind
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store

LABELS = {
    "c1": {"relationship_kind": "romantic_partner", "is_close_friend": 0.99, "is_family": 0.02, "warmth": 3.5},
    "c2": {"relationship_kind": "colleague", "is_coworker_past": 0.8, "is_close_friend": 0.1, "warmth": 1.0},
    "c3": {"relationship_kind": "family", "is_family": 1.0, "warmth": 2.0},
    "c4": {"relationship_kind": "stranger", "is_recruiter": 0.4, "warmth": 0.5},
}


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.adapter = V2(self.base / "v2")
        source_db(self.adapter.database, STORIES)
        with sqlite3.connect(self.adapter.database) as conn:
            for member, labels in LABELS.items():
                conn.execute("INSERT INTO person_labels VALUES(?,?,?,'yes','',?,'now')",
                             (member, "", member, json.dumps(labels)))
            conn.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('c4','mentor','Human note','now')")
        self.store = Store(self.base / "derived.sqlite", create=True)
        self.addCleanup(self.store.close)
        self.provider = FixtureProvider()
        self.store.import_source(self.adapter)
        Generate(self.store, self.provider).run(limit=100)

    def labels(self, parent: str) -> set[str]:
        return {row[0] for row in self.store.conn.execute(
            "SELECT topic FROM active_claims WHERE parent LIKE ? AND kind='tag' AND scope='owner' AND source_key LIKE 'labels:%'",
            ("%#" + parent,))}

    def test_labels_become_owner_tags_and_unknown_kinds_are_skipped(self) -> None:
        self.assertEqual(self.labels("p:1"), {"romantic partner", "close friend"})
        self.assertEqual(self.labels("p:2"), {"colleague", "past coworker"})
        self.assertEqual(self.labels("p:3"), {"family"})
        self.assertEqual(self.labels("p:4"), set())
        # Only message dossiers are generated from; names, tags and labels are projected only.
        self.assertTrue(all(row[0].startswith("facts:") for row in self.store.conn.execute(
            "SELECT key FROM sources WHERE id IN (" + ",".join(str(source.id) for source in self.store.sources()) + ")")))

    def test_layer_words_list_everyone_holding_the_label(self) -> None:
        answer = Query(self.store, self.provider).people("wife")
        self.assertEqual(answer.layers, ("romantic partner",))
        self.assertFalse(answer.filtered)
        self.assertEqual([person.name for person in answer.people], ["Jordan Bravo"])
        # The native id the graph and People page use, not the corpus-prefixed one.
        self.assertEqual(answer.people[0].parent, "p:1")
        self.assertEqual(answer.people[0].warmth, 3.5)
        self.assertIn("romantic partner", [hit.claim.text for hit in answer.people[0].evidence])
        family = Query(self.store, self.provider).people("my family")
        self.assertEqual([person.name for person in family.people], ["Morgan Echo"])
        self.assertEqual(Query(self.store, self.provider).people("", layer="past coworker").people[0].name, "Casey Delta")

    def test_other_words_filter_the_layer_and_rank_by_evidence(self) -> None:
        answer = Query(self.store, self.provider).people("my coworkers with IAM")
        self.assertTrue(answer.filtered)
        # Only labels someone holds are layers: nobody in the fixture is a current coworker.
        self.assertEqual(answer.layers, ("past coworker",))
        self.assertEqual([person.name for person in answer.people], ["Casey Delta"])
        self.assertIn("IAM", answer.people[0].evidence[0].claim.text)

    def test_layer_words_in_other_groups_are_plain_words_without_boost(self) -> None:
        provider = OpenAIProvider("", "")
        self.addCleanup(provider.close)
        query = Query(self.store, provider)
        for text in ("IAM family", "IAM coworkers", "royal family", "the hunt family", "family office"):
            with self.subTest(text=text):
                answer = query.people(text)
                self.assertFalse(answer.filtered)
                self.assertEqual(answer.layers, ())
                for person in answer.people:
                    self.assertAlmostEqual(person.score, sum(hit.score for hit in person.evidence) * (1 + person.warmth / 10))
                    self.assertTrue(all(hit.lexical_rank is not None for hit in person.evidence))
        for text in ("family", "who is family", "my family", "who is in our family"):
            self.assertEqual(query.people(text).layers, ("family",))

    def test_royal_family_and_family_office_do_not_list_owner_family(self) -> None:
        source = self.store.conn.execute("SELECT id,hash FROM sources WHERE key='facts:c1'").fetchone()
        self.store.add_claim(source['id'], source['hash'], None,
                             Claim("Jordan belongs to the royal family and runs a family office.", Kind.STATEMENT,
                                   "family office", "", "", "", "", ""), human=False)
        provider = OpenAIProvider("", "")
        self.addCleanup(provider.close)
        query = Query(self.store, provider)
        for text in ("who is in the royal family", "who runs a family office"):
            with self.subTest(text=text):
                answer = query.people(text)
                self.assertEqual(answer.layers, ())
                self.assertFalse(answer.filtered)
                self.assertIn('p:1', [person.parent for person in answer.people])

    def test_possessive_layer_allows_up_to_two_intervening_words(self) -> None:
        provider = OpenAIProvider("", "")
        self.addCleanup(provider.close)
        query = Query(self.store, provider)
        for text in ("my IAM coworkers", "my past IAM coworkers", "our past coworkers with IAM"):
            with self.subTest(text=text):
                answer = query.people(text)
                self.assertEqual(answer.layers, ('past coworker',))
                self.assertTrue(answer.filtered)
                self.assertEqual([person.parent for person in answer.people], ['p:2'])
        self.assertEqual(query.people("my former past IAM coworkers").layers, ())

    def test_lp_layer_selects_claim_holders_and_supplies_lp_evidence(self) -> None:
        query = Query(self.store, self.provider)
        for text in ("lp", "LPs", "limited partner", "limited partners", "who are my LPs", "our limited partners"):
            with self.subTest(text=text):
                answer = query.people(text)
                self.assertEqual(answer.layers, ("lp",))
                self.assertEqual({person.parent for person in answer.people}, {'p:2', 'p:3'})
                for person in answer.people:
                    self.assertTrue(any(hit.claim.kind == Kind.LP for hit in person.evidence))
        filtered = query.people('which of my LPs work on IAM')
        self.assertTrue(filtered.filtered)
        self.assertEqual([person.parent for person in filtered.people], ['p:2'])

    def test_ranking_dedupes_merged_duplicates_and_warmth_breaks_ties(self) -> None:
        answer = Query(self.store, self.provider).people("Bravo owner")
        jordan = next(person for person in answer.people if person.name == "Jordan Bravo")
        # c1 and c1b saved the same sentence; it scores once and appears once.
        self.assertEqual([hit.claim.text for hit in jordan.evidence].count("Jordan Bravo is a close friend of the owner."), 1)
        self.assertEqual(answer.people[0].name, "Jordan Bravo")
        self.assertGreater(answer.people[0].score, answer.people[-1].score)

    def test_vector_shortlist_dedupes_before_ranking_and_keeps_lowest_id(self) -> None:
        query = Query(self.store, self.provider)
        before = query.people("permission architecture")
        source_db(self.adapter.database, [
            {"member": f"duplicate-{index}", "parent": "p:1", "name": "Jordan Bravo",
             "claims": [STORIES[0]["claims"][1]]} for index in range(101)
        ])
        self.store.import_source(self.adapter)
        Generate(self.store, self.provider).run(limit=200)
        expected = [(person.parent, person.score, [hit.claim_id for hit in person.evidence]) for person in before.people]
        for reverse in (0, 1):
            with self.subTest(reverse=reverse):
                self.store.conn.execute(f"PRAGMA reverse_unordered_selects={reverse}")
                after = query.people("permission architecture")
                self.assertEqual(after.vector_candidates, before.vector_candidates)
                self.assertEqual(
                    [(person.parent, person.score, [hit.claim_id for hit in person.evidence]) for person in after.people],
                    expected)

    def test_nothing_matching_is_an_empty_answer(self) -> None:
        lexical = OpenAIProvider("", "")
        self.addCleanup(lexical.close)
        answer = Query(self.store, lexical).people("zebra")
        self.assertEqual(answer.people, ())
        self.assertEqual(answer.total, 0)

    def test_import_retires_claims_the_projection_no_longer_makes(self) -> None:
        source_id = self.store.conn.execute("SELECT id FROM sources WHERE key='facts:c3'").fetchone()[0]
        source_hash = self.store.conn.execute("SELECT hash FROM sources WHERE id=?", (source_id,)).fetchone()[0]
        stale = self.store.add_claim(source_id, source_hash, None, Claim("Morgan Echo once had a headline.", Kind.STATEMENT, "headline", "", "", "", "", ""), human=False)
        self.assertEqual(self.store.conn.execute("SELECT active FROM claims WHERE id=?", (stale,)).fetchone()[0], 1)
        self.store.import_source(self.adapter)
        self.assertEqual(self.store.conn.execute("SELECT active FROM claims WHERE id=?", (stale,)).fetchone()[0], 0)
        self.assertEqual(self.labels("p:3"), {"family"})

    def test_cli_search_returns_people(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.base / "derived.sqlite"), "search", "family"]), 0)
        answer = json.loads(output.getvalue())
        self.assertEqual([person["name"] for person in answer["people"]], ["Morgan Echo"])
        self.assertEqual(answer["layers"], ["family"])
