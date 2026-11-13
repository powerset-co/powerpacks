"""GET /api/people/page's SQL: the page's tab, facets, search, hides and sort over stored People rows (the
client's filter and sort before 2026-11-03, web/src/lib/people)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.web.model import PeopleQuery, SharePeople

# name, worth, relationship_kind, recency_days, interactions, title/company/location, linkedin_only
FAMILIES = (
    ("p:casey", "Casey Delta", "no", "close_friend", 800, 12, "Engineer", "Initech", "Austin", 0),
    ("p:jordan", "Jordan Bravo", "no", "family", 30, 90, "Founder", "Acme", "Boston", 0),
    ("p:riley", "Riley Echo", "no", "family", None, 3, "Designer", "Globex", "Denver", 0),
    ("p:morgan", "Morgan Fox", "yes", "family", 100, 40, "Analyst", "Hooli", "Springfield", 0),
)


class PeoplePageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        for parent, name, worth, kind, days, count, title, company, location, only in FAMILIES:
            self.conn.execute(
                "INSERT INTO families (parent_id, public_identifier, linkedin_url, name, headline, title, company, "
                "location, profile_picture_url, source_channels, interaction_counts, last_interaction, derived, "
                "worth, relationship_kind, recency_days, interactions, linkedin_only, channels, reason) "
                "VALUES (?, '', '', ?, '', ?, ?, ?, '', 'gmail_msgvault', '{}', '', 1, ?, ?, ?, ?, ?, ?, 'unjudged')",
                (parent, name, title, company, location, worth, kind, days, count, only, json.dumps(["gmail"])))
        self.conn.commit()
        self.people = SharePeople(self.conn, self.root)

    def names(self, logbooks: frozenset[str] = frozenset(), **query: list[str]) -> list[str]:
        return [row.name for row in self.people.page(PeopleQuery.parse(query), logbooks).rows]

    def test_the_tab_and_the_sort(self) -> None:
        self.assertEqual(self.names(tab=["no"]), ["Casey Delta", "Jordan Bravo", "Riley Echo"])
        self.assertEqual(self.names(sort=["messages"]), ["Jordan Bravo", "Morgan Fox", "Casey Delta", "Riley Echo"])
        self.assertEqual(self.names(sort=["last"], dir=["desc"])[0], "Riley Echo")  # never written: last
        self.assertEqual(PeopleQuery.parse({}).sort, "warmth")  # no sort: warmest first

    def test_linkedin_only_people_hide_only_while_anyone_else_shows(self) -> None:
        self.conn.execute("UPDATE families SET linkedin_only = 1 WHERE parent_id = 'p:casey'")
        self.assertEqual(self.names(tab=["no"]), ["Jordan Bravo", "Riley Echo"])
        self.assertEqual(self.names(tab=["no"], show=["linkedin_only"]), ["Casey Delta", "Jordan Bravo", "Riley Echo"])
        self.conn.execute("UPDATE families SET linkedin_only = 1")
        self.assertEqual(self.names(tab=["no"]), ["Casey Delta", "Jordan Bravo", "Riley Echo"])

    def test_an_undecided_linkedin_only_person_counts_and_lists_as_keep(self) -> None:
        # Casey: LinkedIn-only, nobody decided. Jordan: LinkedIn-only but skipped. Riley: undecided, with messages.
        self.conn.execute("UPDATE families SET linkedin_only = 1, worth = '' WHERE parent_id = 'p:casey'")
        self.conn.execute("UPDATE families SET linkedin_only = 1 WHERE parent_id = 'p:jordan'")
        self.conn.execute("UPDATE families SET worth = '' WHERE parent_id = 'p:riley'")
        page = self.people.page(PeopleQuery.parse({"tab": ["yes"], "show": ["linkedin_only"]}), frozenset())
        self.assertEqual(page.totals, {"yes": 2, "no": 1, "unjudged": 1})
        self.assertEqual([row.name for row in page.rows], ["Casey Delta", "Morgan Fox"])
        self.assertEqual(self.names(tab=["unjudged"], show=["linkedin_only"]), ["Riley Echo"])
        self.assertEqual(self.names(tab=["no"], show=["linkedin_only"]), ["Jordan Bravo"])
        self.assertEqual(self.names(show=["linkedin_only"], **{"filter.worth": ["yes"]}), ["Casey Delta", "Morgan Fox"])

    def test_every_held_facet_and_the_counts(self) -> None:
        held = {"filter.relationship_kind": ["family"], "filter.last": ["< 1 year"]}
        self.assertEqual(self.names(tab=["no"], **held), ["Jordan Bravo"])
        self.assertEqual(self.names(**{"filter.last": ["Never"]}), ["Riley Echo"])
        page = self.people.page(PeopleQuery.parse({"tab": ["no"], "q": ["jordan"]}), frozenset())
        # Relationship counts are over the tab, before the search and facets; totals over everyone.
        self.assertEqual((page.matching, page.kinds, page.totals), (1, {"family": 2, "close_friend": 1},
                                                                    {"no": 3, "yes": 1}))

    def test_the_saved_logbooks(self) -> None:
        saved = frozenset({"p:casey"})
        self.assertEqual(self.names(saved, tab=["no"], **{"filter.logbook": ["Has logbook"]}), ["Casey Delta"])
        self.assertEqual(self.names(saved, tab=["no"], **{"filter.logbook": ["No logbook"]}),
                         ["Jordan Bravo", "Riley Echo"])

    def test_text_matches_name_title_company_and_location(self) -> None:
        self.assertEqual(self.names(q=[" ACME "]), ["Jordan Bravo"])
        self.assertEqual(self.names(tab=["yes"], q=["springfield"]), ["Morgan Fox"])

    def test_a_slice_and_every_id(self) -> None:
        page = self.people.page(PeopleQuery.parse({"offset": ["1"], "limit": ["2"]}), frozenset())
        self.assertEqual(([row.name for row in page.rows], page.matching), (["Jordan Bravo", "Morgan Fox"], 4))
        ids = self.people.page(PeopleQuery.parse({"ids": ["1"], "tab": ["no"]}), frozenset()).ids
        self.assertEqual(ids, ("p:casey", "p:jordan", "p:riley"))


if __name__ == "__main__":
    unittest.main()
