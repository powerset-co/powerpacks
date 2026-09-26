"""The People page: the row model, the one atomic decision write, and the routes."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from http.server import ThreadingHTTPServer
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from deep_context_sqlite_test_helpers import seed_identity
from packs.ingestion.primitives.deep_context.db.models import PersonTagRow, ShareDecisionRow
from packs.ingestion.primitives.deep_context.db.share_views import person_labels, person_tags, share_decisions
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import label_row_from_export, share_decision
from packs.ingestion.primitives.share.models import HumanTags
from packs.ingestion.primitives.share.questions import build_questions
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.store import TagStore
from packs.ingestion.primitives.share.web.model import PEOPLE_COLUMNS, SharePeople, people_payload
from packs.ingestion.primitives.share.web.server import (
    decide_tags,
    make_handler,
    parse_tag_request,
    share_routes,
)
from packs.shared.csv_io import CsvIO

PEOPLE_HEADER = [
    "id", "public_identifier", "linkedin_url", "full_name", "headline", "current_title", "current_company",
    "city", "country", "profile_picture_url", "source_channels", "interaction_counts", "last_interaction",
    "superseded_person_ids",
]
SUPERSEDED = "candidate:email:casey@example.com"


def _saved_labels(**cells: object) -> dict:
    saved: dict = {}
    for name, question in build_questions().items():
        if question["type"] == "noul":
            saved[name] = cells.get(name, 0.0)
        elif question["type"] == "score":
            saved[name] = cells.get(name, 0)
        else:
            saved[name] = cells.get(name, "unknown")
            saved[f"{name}_p"] = 1.0
    return saved


def _decisions(db: Db) -> dict[str, tuple]:
    return {row.person_id: (row.share, row.reason, row.labels, row.source) for row in share_decisions(db)}


class ShareWebFixture(unittest.TestCase):
    """Roster: Jordan (worth yes), Casey (worth yes, family flag, an old id that merged
    away), Riley (worth maybe, LinkedIn-only), Morgan (0.5999 is_personal: below the
    active bar only when unrounded)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.people_csv = self.root / "people.csv"
        CsvIO.write_dict_rows(self.people_csv, PEOPLE_HEADER, [
            {"id": "person-a", "linkedin_url": "https://www.linkedin.com/in/jordan-bravo", "full_name": "Jordan Bravo",
             "current_title": "Staff Engineer", "current_company": "Example Corp", "city": "Oakland",
             "country": "United States", "profile_picture_url": "https://img.example.com/jordan.jpg",
             "source_channels": json.dumps(["gmail_msgvault", "linkedin_csv"]),
             "interaction_counts": json.dumps({"gmail": 12}), "last_interaction": "2026-06-01T00:00:00Z"},
            {"id": "person-b", "public_identifier": "casey-delta", "full_name": "Casey Delta",
             "source_channels": json.dumps(["imessage"]), "interaction_counts": json.dumps({"imessage": 400}),
             "last_interaction": "2026-09-01T00:00:00Z", "superseded_person_ids": json.dumps([SUPERSEDED])},
            {"id": "person-c", "public_identifier": "riley-echo", "full_name": "Riley Echo",
             "source_channels": json.dumps(["linkedin_csv"])},
            {"id": "person-d", "public_identifier": "morgan-fox", "full_name": "Morgan Fox",
             "source_channels": json.dumps(["gmail_msgvault"])},
            {"id": "person-e", "full_name": "Casey D (work)", "source_channels": json.dumps(["gmail_msgvault"]),
             "interaction_counts": json.dumps({"gmail": 5}), "last_interaction": "2025-01-01T00:00:00Z"},
        ])
        for parent_id, person_id, row_key, name, slug, worth, labels in (
            ("parent-aaaa", "person-a", "jordan-bravo-aaaa", "Jordan Bravo", "jordan-bravo", "yes",
             _saved_labels(is_professional=0.9, is_coworker_past=0.7, relationship_kind="colleague", warmth=2)),
            # Casey's work email is a second person under the same parent; the parent's facts are seeded last.
            ("parent-bbbb", "person-e", "casey-d-bbbb", "Casey D (work)", "casey-d", "yes",
             _saved_labels(is_family=0.95, is_personal=0.8, relationship_kind="family", warmth=4)),
            ("parent-bbbb", "person-b", "casey-delta-bbbb", "Casey Delta", "casey-delta", "yes",
             _saved_labels(is_family=0.95, is_personal=0.8, relationship_kind="family", warmth=4)),
            ("parent-cccc", "person-c", "riley-echo-cccc", "Riley Echo", "riley-echo", "maybe", None),
            ("parent-dddd", "person-d", "morgan-fox-dddd", "Morgan Fox", "morgan-fox", "yes",
             _saved_labels(is_personal=0.5999, is_professional=0.61)),
        ):
            seed_identity(self.db, parent_id=parent_id, person_id=person_id, row_key=row_key, name=name,
                          machine_worth=worth, public_identifier=slug, labels=labels)
        self.evidence = ShareEvidence(self.db, people_csv=self.people_csv)
        self._share()
        self.people = SharePeople(self.db, people_csv=self.people_csv)

    def _share(self) -> dict:
        return ShareList(db=self.db, out_dir=self.root / "share", evidence=self.evidence).run().to_payload()


class RowModelTests(ShareWebFixture):
    def test_rows_join_roster_labels_and_decision(self) -> None:
        rows = {row.parent_id: row for row in self.people.load()}
        self.assertEqual(sorted(rows), ["parent-aaaa", "parent-bbbb", "parent-cccc", "parent-dddd"])
        jordan = rows["parent-aaaa"]
        self.assertEqual((jordan.title, jordan.company, jordan.location),
                         ("Staff Engineer", "Example Corp", "Oakland, United States"))
        self.assertEqual(jordan.channels, ("gmail", "linkedin"))
        self.assertEqual((jordan.interactions, jordan.has_avatar), (12, True))
        self.assertEqual((jordan.share, jordan.reason, jordan.worth, jordan.worth_source),
                         ("yes", "worth_yes", "yes", "machine"))
        self.assertEqual(jordan.labels, ("is_professional", "is_coworker_past"))
        self.assertEqual((jordan.relationship_kind, jordan.warmth), ("colleague", 2))
        # One row per parent: Casey's two roster people share it, its sources, counts and decision.
        casey = rows["parent-bbbb"]
        self.assertEqual((casey.share, casey.reason, casey.flag), ("confirm", "family", "family"))
        self.assertEqual((casey.name, casey.public_identifier, casey.channels, casey.interactions, casey.last_interaction),
                         ("Casey Delta", "casey-delta", ("imessage", "gmail"), 405, "2026-09-01T00:00:00Z"))
        riley = rows["parent-cccc"]
        self.assertEqual((riley.share, riley.reason, riley.linkedin_only, riley.labels), ("no", "worth_maybe", True, ()))
        self.assertEqual(rows["parent-dddd"].labels, ("is_professional",))

    def test_payload_is_columnar_with_counts(self) -> None:
        payload = people_payload(self.people.load())
        self.assertEqual(payload["counts"], {"total": 4, "upload": 2, "confirm": 1, "private": 1})
        self.assertEqual(payload["columns"], list(PEOPLE_COLUMNS))
        self.assertEqual(len(payload["rows"][0]), len(PEOPLE_COLUMNS))

    def test_detail_carries_the_drawer_only_cells(self) -> None:
        detail = self.people.detail("parent-bbbb")
        self.assertEqual(detail.probabilities["is_family"], 0.95)
        self.assertEqual(detail.choice_p["relationship_kind"], 1.0)
        self.assertEqual(detail.worth_reason, "fixture")
        self.assertEqual(detail.events, ())
        self.assertIsNone(self.people.detail("nobody"))

    def test_detail_orders_the_timeline_and_reads_the_facts(self) -> None:
        facts = {"notable_events": [
            {"date": "2026-03", "summary": "Caught up over coffee."},
            {"date": "2024-10-30", "summary": "Introduced by a mutual friend."},
            {"date": "2025-01-02", "summary": ""},
        ], "shared_context": [{"overlap": "school", "detail": "Example University"}],
            "aliases": ["Casey Delta", "CD"], "location": "Oakland"}
        with self.db.transaction() as conn:
            conn.execute("update facts set facts_json = json_patch(facts_json, ?) where parent_id = 'parent-bbbb'",
                         (json.dumps(facts),))
        detail = SharePeople(self.db, people_csv=self.people_csv).detail("parent-bbbb")
        self.assertEqual([(event.date, event.summary) for event in detail.events],
                         [("2026-03", "Caught up over coffee."), ("2024-10-30", "Introduced by a mutual friend.")])
        self.assertEqual(detail.shared_context, ("school: Example University",))
        self.assertEqual(detail.aliases, ("Casey D (work)", "CD"))
        self.assertEqual(detail.location, "Oakland")

    def test_an_empty_share_table_yields_no_rows(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM share")
        self.assertEqual(self.people.load(), ())


class DecisionWriteTests(ShareWebFixture):
    def test_the_export_re_decides_every_row_the_node_wrote(self) -> None:
        tags = TagStore(self.db).load()
        for label in person_labels(self.db):
            expected = _decisions(self.db)[label.person_id]
            row = share_decision(label_row_from_export(label), tags.get(label.person_id), updated_at="x")
            self.assertEqual((row.share, row.reason, row.labels, row.source), expected, label.person_id)

    def test_tags_and_share_rows_are_written_together(self) -> None:
        decided = decide_tags(self.db, self.people, {"parent-bbbb": frozenset({"share"}), "parent-cccc": frozenset({"share"})})
        self.assertEqual({parent: [(row.person_id, row.share, row.reason, row.source) for row in rows]
                          for parent, rows in decided.items()},
                         {"parent-bbbb": [("person-b", "yes", "human_share", "human"), ("person-e", "yes", "human_share", "human")],
                          "parent-cccc": [("person-c", "yes", "human_share", "human")]})
        table = _decisions(self.db)
        self.assertEqual(table["person-a"][1], "worth_yes")
        self.assertEqual(table["person-b"][2], "is_family|is_personal|family")
        self.assertEqual({row.person_id: row.tags for row in person_tags(self.db)},
                         {"person-b": "share", "person-c": "share", "person-e": "share"})

    def test_ui_decisions_match_a_fresh_share_run(self) -> None:
        decide_tags(self.db, self.people, {"parent-aaaa": frozenset({"private"}), "parent-bbbb": frozenset({"share"}),
                                           "parent-dddd": frozenset()})
        before = _decisions(self.db)
        self._share()
        self.assertEqual(before, _decisions(self.db))
        self.assertEqual(before["person-a"][:2], ("no", "human_private"))

    def test_an_absolute_set_replaces_opposing_tags_and_empty_returns_to_worth(self) -> None:
        decide_tags(self.db, self.people, {"parent-bbbb": frozenset({"private"})})
        rows = decide_tags(self.db, self.people, {"parent-bbbb": frozenset({"share"})})["parent-bbbb"]
        self.assertEqual((rows[0].share, rows[0].reason), ("yes", "human_share"))
        rows = decide_tags(self.db, self.people, {"parent-bbbb": frozenset()})["parent-bbbb"]
        self.assertEqual((rows[0].share, rows[0].reason, rows[0].source), ("confirm", "family", "machine"))

    def test_an_inherited_tag_moves_to_the_roster_id_with_its_note(self) -> None:
        TagStore(self.db).apply(SUPERSEDED, add={"private"}, remove=set(), note="old id")
        self._share()
        self.assertEqual(_decisions(self.db)["person-b"][:2], ("no", "human_private"))
        decide_tags(self.db, self.people, {"parent-bbbb": frozenset({"private", "is_family"})})
        rows = {row.person_id: row for row in person_tags(self.db)}
        self.assertEqual((rows["person-b"].tags, rows["person-b"].note), ("is_family|private", "old id"))
        self.assertEqual((rows["person-e"].tags, rows["person-e"].note), ("is_family|private", None))
        self._share()
        self.assertEqual(_decisions(self.db)["person-b"][:2], ("no", "human_private"))

    def test_a_failed_share_write_rolls_back_the_tags(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.decide_share(
                (PersonTagRow(person_id="person-a", tags="share", updated_at="x"),),
                (ShareDecisionRow(person_id="person-a", share="bogus", reason="x", updated_at="x"),),
            )
        self.assertEqual(person_tags(self.db), ())
        self.assertEqual(_decisions(self.db)["person-a"][:2], ("yes", "worth_yes"))

    def test_requests_are_absolute_sets_over_the_vocabulary(self) -> None:
        known = {"parent-aaaa"}
        self.assertEqual(parse_tag_request(json.dumps({"people": [{"parent_id": "parent-aaaa", "tags": ["share"]}]}).encode(), known),
                         {"parent-aaaa": frozenset({"share"})})
        for body, message in (
            ({"people": [{"parent_id": "ghost", "tags": ["share"]}]}, "not on the share list"),
            ({"people": [{"parent_id": "parent-aaaa", "tags": ["warmth"]}]}, "unknown tags"),
            ({"people": []}, "non-empty"),
            ({"people": [{"parent_id": "parent-aaaa"}]}, "list of tags"),
        ):
            with self.assertRaises(ValueError) as caught:
                parse_tag_request(json.dumps(body).encode(), known)
            self.assertIn(message, str(caught.exception))


class RoutesTests(ShareWebFixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(share_routes(self.db, self.people_csv)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path) as response:
            return json.loads(response.read())

    def _post(self, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + "/api/people/tags", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_people_payload_detail_and_health_are_served(self) -> None:
        payload = self._get("/api/people/rows")
        self.assertEqual(payload["counts"]["total"], 4)
        self.assertEqual(self._get("/api/people/person?id=parent-aaaa")["linkedin_url"],
                         "https://www.linkedin.com/in/jordan-bravo")
        self.assertEqual(self._get("/healthz")["people"], 4)
        with urllib.request.urlopen(self.base + "/people/assets/results.css") as response:
            self.assertEqual(response.headers["Content-Type"], "text/css; charset=utf-8")

    def test_tags_post_writes_and_the_next_payload_reflects_it(self) -> None:
        status, body = self._post({"people": [{"parent_id": "parent-bbbb", "tags": ["share"]}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"], [{"parent_id": "parent-bbbb", "share": "yes", "reason": "human_share",
                                         "share_source": "human", "tags": ["share"]}])
        self.assertEqual(self._get("/api/people/rows")["counts"], {"total": 4, "upload": 3, "confirm": 0, "private": 1})

    def test_bad_requests_write_nothing(self) -> None:
        status, payload = self._post({"people": [{"parent_id": "ghost", "tags": ["share"]}]})
        self.assertEqual((status, "not on the share list" in payload["error"]), (400, True))
        self.assertEqual(person_tags(self.db), ())

    def test_avatar_redirects_to_the_roster_url(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        try:
            urllib.request.build_opener(NoRedirect).open(self.base + "/api/people/avatar?id=parent-aaaa")
        except urllib.error.HTTPError as exc:
            self.assertEqual((exc.code, exc.headers["Location"]), (302, "https://img.example.com/jordan.jpg"))
        else:
            self.fail("expected a redirect")


class BrowserTests(ShareWebFixture):
    """The page in Chrome: facets filter, select-all-matching writes, undo restores."""

    def setUp(self) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright is not installed")
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(share_routes(self.db, self.people_csv)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_tabs_facets_select_all_matching_share_and_undo(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(self.base + "/people")
            # Opens on the Confirm tab: Casey alone; the tabs carry the totals.
            expect(page.locator(".row")).to_have_count(1)
            expect(page.locator("[data-count]")).to_have_text("1 person")
            expect(page.locator("[data-tab='yes'] b")).to_have_text("2")
            # Casey's two roster people share the row: both sources show.
            expect(page.locator(".row .source")).to_have_count(2)
            expect(page.locator(".row .source").first).to_have_attribute("title", "iMessage")
            expect(page.locator(".row .warmth-cell")).to_have_text("4.0")
            page.locator("[data-tab='yes']").click()
            expect(page.locator(".row")).to_have_count(2)
            page.locator("[data-facet-key='relationship_kind'][data-facet-value='colleague']").click()
            expect(page.locator(".row")).to_have_count(1)
            expect(page.locator(".row .who b")).to_have_text("Jordan Bravo")
            page.get_by_role("button", name="Clear").click()
            expect(page.locator(".row")).to_have_count(2)
            page.locator("[data-tab='confirm']").click()
            page.locator("[data-select-all]").check()
            expect(page.locator("[data-bulkbar] b")).to_have_text("1 selected")
            page.get_by_role("button", name="Share S").click()
            expect(page.locator(".toast")).to_contain_text("Marked 1 person for sharing.")
            expect(page.locator("[data-empty]")).to_have_text("No one needs confirmation.")
            expect(page.locator("[data-tab='yes'] b")).to_have_text("3")
            self.assertEqual(_decisions(self.db)["person-b"][:2], ("yes", "human_share"))
            self.assertEqual(_decisions(self.db)["person-e"][:2], ("yes", "human_share"))
            page.locator(".toast").get_by_role("button", name="Undo Z").click()
            expect(page.locator(".row")).to_have_count(1)
            expect(page.locator("[data-tab='yes'] b")).to_have_text("2")
            self.assertEqual(_decisions(self.db)["person-b"][:2], ("confirm", "family"))
            # A click opens the drawer on the same row node; the next click closes it.
            page.evaluate("document.querySelector('.row').__kept = true")
            page.locator(".row").click()
            expect(page.locator("[data-drawer] h2")).to_have_text("Casey Delta")
            expect(page.locator("[data-drawer] .barrow.active")).to_have_count(2)
            expect(page.locator("[data-people]")).to_have_attribute("data-drawer-open", "true")
            expect(page.locator(".row")).to_have_attribute("data-open", "true")
            self.assertTrue(page.evaluate("document.querySelector('.row').__kept === true"))
            page.locator(".row").click()
            expect(page.locator("[data-people]")).to_have_attribute("data-drawer-open", "false")
            expect(page.locator(".row")).to_have_attribute("data-open", "false")
            browser.close()


if __name__ == "__main__":
    unittest.main()
