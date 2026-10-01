"""The Review page's JSON routes, and their parity with the Jinja page they mirror.

Every parity test asks the old route and the new one about the same seeded store:
the JSON must name the screen, steps, people, labels, contacts and reasons the HTML shows.
"""

from __future__ import annotations

import dataclasses
import html
import json
import re
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import get_args
from unittest import mock

from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.deep_context.db import _view_rows as view_rows
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ProjectionStatus,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.view_models import WorthHumanRow, WorthMachineRow
from packs.ingestion.primitives.deep_context.db.worth_views import worth_queue
from packs.ingestion.primitives.deep_context.enrich import enrichment_pipeline
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import ResearchOutcome
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.review import api as review_api
from packs.ingestion.primitives.deep_context.review import server as review_server
from packs.ingestion.primitives.deep_context.review import sqlite_adapter as review_adapter
from packs.ingestion.primitives.deep_context.review.api import (
    ApproveResult,
    DecideResult,
    DecisionProgress,
    DecisionRow,
    EnrichmentMode,
    EnrichmentPanel,
    LinkedinCard,
    LinkedinCardPayload,
    LinkedinDecision,
    LinkedinFinished,
    PageProgress,
    QueuePosition,
    ReviewCandidate,
    ReviewPage,
    ReviewPerson,
    ReviewStep,
    ReviewView,
    WorthCard,
    WorthCardPayload,
    WorthTab,
    WorthTablePayload,
)
from packs.ingestion.primitives.deep_context.review.enrichment import STAGE_BY_ACTION
from packs.ingestion.primitives.deep_context.review.models import EnrichmentApproval, GuidanceViewRow
from packs.ingestion.primitives.deep_context.review.rendering import (
    WorthPendingEntry,
    decision_rows_html,
    render_enrichment,
    render_linkedin_card,
    render_worth_card,
)
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import SqliteReviewAdapter
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponsesCaller
from packs.ingestion.primitives.enrich.rapidapi_client import RapidApiClient
from deep_context_sqlite_test_helpers import (
    query,
    replace_candidate_people,
    replace_person_identifiers,
    replace_person_sources,
    seed_identity,
)
from http_handler_test_helpers import InProcessHttpClient

REVIEW_TYPES = Path(__file__).resolve().parents[1] / "web" / "src" / "types" / "review.ts"
JSON_TYPE = "application/json; charset=utf-8"
STAGES = ("", "worth", "enrich", "linkedin", "done")
# The panel heading the Jinja page draws for each mode (templates/enrichment.html.j2).
ENRICH_HEADINGS = {
    "running": "Enriching Contacts",
    "approval": "Ready to Enrich",
    "completed": "Contacts Enriched",
    "failed": "Enrichment Paused",
    "preparing": "Preparing Enrichment",
}
CASEY_LABELS = {
    "is_family": 0.95,
    "is_close_friend": 0.9,
    "is_founder": 0.99,
    "is_investor": 0.86,
    "is_client": 0.5,
    "relationship_kind": "college_friend",
    "relationship_kind_p": 0.97,
}
CASEY_TITLES = ["Founder", "College friend", "Family", "Close friend", "Investor"]
STEP_RE = re.compile(
    r"<a class='step(?: (\w+))?' href='/\?stage=(\w+)&amp;preview=1'>\s*<span>(.*?)</span>\s*"
    r"<div>([^<]*)(?:<small>(\d+) left</small>)?</div>"
)


def ts_fields(interface: str) -> tuple[str, ...]:
    """The field names of one `export interface` in types/review.ts, inherited ones first."""
    source = REVIEW_TYPES.read_text(encoding="utf-8")
    found = re.search(rf"^export interface {interface}(?: extends (\w+))? \{{\n(.*?)^\}}", source, re.S | re.M)
    assert found is not None, interface
    inherited = ts_fields(found.group(1)) if found.group(1) else ()
    return inherited + tuple(re.findall(r"^  (\w+)\??:", found.group(2), re.M))


def ts_inline_fields(interface: str, field: str) -> tuple[str, ...]:
    """The field names of an inline `field: { a: A; b: B } | null` object type."""
    source = REVIEW_TYPES.read_text(encoding="utf-8")
    body = re.search(rf"^export interface {interface} \{{\n(.*?)^\}}", source, re.S | re.M)
    assert body is not None, interface
    inline = re.search(rf"^  {field}: \{{ (.*?) \}}", body.group(1), re.M)
    assert inline is not None, f"{interface}.{field}"
    return tuple(re.findall(r"(\w+):", inline.group(1)))


def ts_union(name: str) -> set[str]:
    """The string literals of `export type <name> = "a" | "b"` in types/review.ts."""
    source = REVIEW_TYPES.read_text(encoding="utf-8")
    union = re.search(rf"^export type {name} =(.*?)$", source, re.M)
    assert union is not None, name
    return set(re.findall(r'"(\w*)"', union.group(1)))


def field_names(shape: type) -> tuple[str, ...]:
    return tuple(field.name for field in dataclasses.fields(shape))


def jinja_labels(fragment: str) -> list[str]:
    """Every label title one Jinja `person-labels` block shows: the badges, then the tooltip's."""
    block = fragment.split("<span class='person-labels'>", 1)
    if len(block) == 1:
        return []
    first = block[1].split("</span></span></span>", 1)[0] + "</span>"
    shown = re.findall(r"<span class='person-label'>(.*?)</span>", first)
    tooltip = re.search(r"role='tooltip'>(.*?)</span>", first)
    return [html.unescape(title) for title in shown + (tooltip.group(1).split(" · ") if tooltip else [])]


def refuse_paid_call(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("paid call in unit test")


class ReviewStore:
    """One synthetic store behind the review server: a worth parent and a LinkedIn parent."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = Db(root / "deep-context.sqlite")
        self.seed("worth-parent", "casey-delta", "Casey Delta", "maybe", labels=CASEY_LABELS)
        self.seed("linkedin-parent", "jordan-bravo", "Jordan Bravo", "yes")
        replace_person_identifiers(
            self.db,
            "worth-parent-person",
            (
                PersonIdentifierRow("worth-parent-person", "email", "casey-delta@example.com"),
                PersonIdentifierRow("worth-parent-person", "phone", "+15550100"),
            ),
        )
        self.http = self.client()

    def client(self, *, run_jobs: bool = True) -> InProcessHttpClient:
        # A stand-in retarget worker: re-research is paid and never runs here.
        handler = review_server.make_handler(
            confirm_threshold=0.7, run_jobs=run_jobs, guided_retargets=mock.Mock(), db=self.db
        )
        return InProcessHttpClient(handler)

    def seed(self, parent_id: str, slug: str, name: str, worth: str, *, labels: dict | None = None) -> None:
        """A `maybe` parent is an email candidate (the worth queue); a `yes` one holds a LinkedIn."""
        pending_worth = worth == "maybe"
        seed_identity(
            self.db,
            parent_id=parent_id,
            person_id=f"{parent_id}-person",
            row_key=f"candidate:email:{slug}@example.com" if pending_worth else slug,
            name=name,
            machine_worth=worth,
            display_slug=slug,
            kind=RowKind.CANDIDATE_EMAIL.value if pending_worth else RowKind.PUB.value,
            linkedin_url=None if pending_worth else f"https://www.linkedin.com/in/{slug}",
            link_updates={
                "machine_action": "verify",
                "machine_confidence": 0.5,
                **({"candidate_origin": 1, "raw_import": 1} if pending_worth else {"paid_profile": 1}),
            },
            candidate_people=True,
            artifact_root=self.root,
            dossier_body=f"# {name}\n\n## Relationship\nSynthetic collaborator.\n",
            labels=labels,
        )

    def seed_worth_queue(self) -> None:
        self.seed("morgan-parent", "morgan-echo", "Morgan Echo", "maybe")
        self.seed("avery-parent", "avery-quinn", "Avery Quinn", "maybe")

    def seed_linkedin_queue(self) -> None:
        self.seed("riley-parent", "riley-stone", "Riley Stone", "yes")
        self.seed("sam-parent", "sam-tango", "Sam Tango", "yes")

    def reach_enrich(self) -> None:
        self.db.decide_worth("worth-parent", "no", note="Synthetic note")

    def reach_linkedin(self) -> None:
        self.reach_enrich()
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT row_key,parent_id,linkedin_url FROM links WHERE kind='pub'").fetchall():
                payload = {
                    "verdict": "needs_review", "confidence": 0.5,
                    "relationship_decision": {
                        "parent_id": row["parent_id"], "fingerprint": "fixture",
                        "candidates": [{"url": row["linkedin_url"], "verdict": "review",
                                        "reason": "Synthetic fixture needs human review", "confidence": 0.5}],
                    },
                }
                conn.execute(
                    "UPDATE links SET judgment_payload_json=?, judgment_fingerprint='fixture' WHERE row_key=?",
                    (json.dumps(payload), row["row_key"]),
                )

    def reach_done(self) -> None:
        self.reach_linkedin()
        self.db.decide_identity("jordan-bravo", "verify")

    def links(self) -> list[dict]:
        """Every identity row, without the two clock columns."""
        rows = query(self.db, "SELECT * FROM links ORDER BY row_key")
        return [{key: row[key] for key in row.keys() if key not in {"decided_at", "updated_at"}} for row in rows]


class ReviewApiFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The RapidAPI profile fetch and the OpenAI caller are refused at their
        # definitions, so a missing stub fails loudly instead of billing .env.
        for patcher in (
            mock.patch.object(RapidApiClient, "get_profile", refuse_paid_call),
            mock.patch.object(OpenAIResponsesCaller, "__init__", refuse_paid_call),
        ):
            patcher.start()
            cls.addClassCleanup(patcher.stop)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enrichment_manifest = self.root / "deep-research" / "manifest.json"
        for patcher in (
            mock.patch.object(enrichment_pipeline, "ENRICH_MANIFEST", self.enrichment_manifest),
            mock.patch.object(projection, "hydrate_profiles", return_value={"ok": 0, "failed": 0}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = ReviewStore(self.root / "store")
        self.db = self.store.db
        self.http = self.store.http

    def get_json(self, path: str, *, http: InProcessHttpClient | None = None) -> tuple[int, dict]:
        status, content_type, body, headers = (http or self.http).request("GET", path)
        self.assertEqual(content_type, JSON_TYPE, body)
        self.assertEqual(headers["cache-control"], "no-store")
        return status, json.loads(body)

    def post_json(
        self,
        path: str,
        fields: dict[str, str] | None = None,
        *,
        headers: dict[str, str] | None = None,
        http: InProcessHttpClient | None = None,
    ) -> tuple[int, dict]:
        status, content_type, body, _ = (http or self.http).request("POST", path, fields or {}, headers)
        self.assertEqual(content_type, JSON_TYPE, body)
        return status, json.loads(body)

    def jinja(self, method: str, path: str, fields: dict[str, str] | None = None) -> tuple[int, str]:
        """The old route's status and body, as text."""
        status, _, body, _ = self.http.request(method, path, fields)
        return status, body.decode()

    def payload(self, path: str) -> dict:
        status, payload = self.get_json(path)
        self.assertEqual(status, 200, payload)
        return payload


class ReviewPageTests(ReviewApiFixture):
    def assert_page_parity(self, query_string: str) -> dict:
        """The page payload says what the Jinja page at the same query draws."""
        page = self.payload(f"/api/review/page{query_string}")
        status, old = self.jinja("GET", f"/{query_string}")
        self.assertEqual(status, 200)
        view = page["view"]
        self.assertEqual(view, re.search(r"data-stage='(\w+)'", old).group(1))
        self.assertEqual(page["title"], re.search(r"<h1 class='topbar-title'>(.*?)</h1>", old).group(1))
        self.assertEqual(page["state_token"], re.search(r"data-state-token='(.*?)'", old).group(1))
        self.assertEqual(
            str(page["external_updates"]).lower(), re.search(r"data-external-updates='(\w+)'", old).group(1)
        )

        drawn = STEP_RE.findall(old)
        self.assertEqual(len(drawn), 3)
        for step, (_, stage, marker, label, count) in zip(page["steps"], drawn, strict=True):
            self.assertEqual((step["stage"], step["label"], step["count"]), (stage, label, int(count or 0)))
            checked = step["complete"] and step["count"] == 0
            self.assertEqual(marker, "✓" if checked else str(step["number"]))

        if view == "worth":
            tabs = dict(re.findall(r"data-tab='(\w+)'\s+href='[^']*'>\w+<span>(\d+)</span>", old))
            progress = page["progress"]
            self.assertEqual(
                {key: int(count) for key, count in tabs.items()},
                {"review": progress["worth_pending"], "yes": progress["worth_yes"], "no": progress["worth_no"]},
            )
            self.assertEqual(page["tab"], re.search(r"decision-tab active' data-tab='(\w+)'", old).group(1))
        else:
            self.assertEqual(page["tab"], "")
        if view == "enrich":
            self.assertIn(f"<h2>{ENRICH_HEADINGS[page['enrichment']['mode']]}</h2>", old)
            self.assertIn(page["enrichment"]["approval_label"], old)
        if view == "done":
            progress = page["progress"]
            self.assertIn(f"{progress['linkedin_done']} identities checked · {progress['rejected']} rejected", old)
        return page

    def test_page_matches_the_jinja_page_at_every_stage_of_the_store(self) -> None:
        # Each driver moves the store one stage on; the page with no stage lands there.
        drivers = (
            ("worth", lambda: None),
            ("enrich", self.store.reach_enrich),
            ("linkedin", self.store.reach_linkedin),
            ("done", self.store.reach_done),
        )
        for current, drive in drivers:
            drive()
            for stage in STAGES:
                with self.subTest(store=current, stage=stage):
                    page = self.assert_page_parity(f"?stage={stage}" if stage else "")
                    if not stage:
                        self.assertEqual(page["view"], current)
            for tab in ("review", "yes", "no", "bogus", "YES", "REVIEW"):
                with self.subTest(store=current, tab=tab):
                    self.assert_page_parity(f"?stage=worth&view={tab}")

    def test_an_emptied_worth_queue_opens_enrich_but_its_piles_stay_reachable(self) -> None:
        self.store.reach_enrich()
        self.assertEqual(self.payload("/api/review/page?stage=worth")["view"], "enrich")
        self.assertEqual(self.payload("/api/review/page?stage=worth&view=review")["view"], "enrich")
        piles = self.payload("/api/review/page?stage=worth&view=no")
        self.assertEqual((piles["view"], piles["tab"], piles["title"]), ("worth", "no", "Add People"))

    def test_page_names_the_screen_and_its_counts(self) -> None:
        page = self.payload("/api/review/page")
        self.assertEqual((page["view"], page["tab"], page["title"]), ("worth", "review", "Add People"))
        self.assertIs(page["external_updates"], False)
        self.assertEqual(
            page["progress"],
            {
                "worth_pending": 1,
                "worth_yes": 1,
                "worth_no": 0,
                "linkedin_pending": 1,
                "linkedin_done": 0,
                "rejected": 0,
                "synthesize_pending": 0,
            },
        )
        self.assertEqual(
            [(step["number"], step["label"], step["stage"]) for step in page["steps"]],
            [(1, "Review Decisions", "worth"), (2, "Enrich Contacts", "enrich"), (3, "Check LinkedIn", "linkedin")],
        )
        for stage, watched in (("enrich", True), ("done", True), ("linkedin", False)):
            self.assertIs(self.payload(f"/api/review/page?stage={stage}")["external_updates"], watched)

    def test_page_shell_and_unknown_api_paths(self) -> None:
        status, content_type, body, _ = self.http.request("GET", "/review?stage=worth&preview=1")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"/app/assets/app.js", body)

        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, content_type, body, _ = self.http.request(method, "/api/review/missing", {})
                self.assertEqual((status, content_type), (404, JSON_TYPE))
                self.assertEqual(json.loads(body), {"error": "not found"})
        # A GET route is not a POST route, and the reverse.
        self.assertEqual(self.post_json("/api/review/page")[0], 404)
        self.assertEqual(self.get_json("/api/review/decide")[0], 404)


class SynthesisPendingTests(unittest.TestCase):
    """A collected store whose synthesis never ran: no step is complete, no queue is done."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Db(Path(tmp.name) / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow("collected", "collected", "Casey Delta", "casey-delta"),
                PersonRow("collected-person", "collected", display_name="Casey Delta"),
                ArtifactRow(
                    "source_bundle:collected",
                    ArtifactKind.SOURCE_BUNDLE.value,
                    "collected",
                    "/raw/collected.json",
                    "sha-collected",
                    ProjectionStatus.PROJECTED.value,
                ),
            )
        )
        self.http = InProcessHttpClient(review_server.make_handler(db=self.db))

    def payload(self, path: str) -> dict:
        status, _, body, _ = self.http.request("GET", path)
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_every_route_says_synthesis_is_pending(self) -> None:
        for stage in STAGES:
            with self.subTest(stage=stage):
                page = self.payload(f"/api/review/page?stage={stage}")
                self.assertEqual(page["progress"]["synthesize_pending"], 1)
                self.assertEqual([step["complete"] for step in page["steps"]], [False, False, False])
                _, _, old, _ = self.http.request("GET", f"/?stage={stage}")
                self.assertEqual(page["view"], re.search(rb"data-stage='(\w+)'", old).group(1).decode())
                self.assertNotIn("✓".encode(), old)
                # Enrich and Done say it on the page; the two queues say it on their card reads.
                if page["view"] in ("enrich", "done"):
                    self.assertIs(page["needs_synthesis"], b"Synthesis has not run" in old)
                else:
                    self.assertIs(page["needs_synthesis"], False)

        self.assertEqual(
            self.payload("/api/review/worth-card"), {"card": None, "synthesize_pending": True, "queue": None}
        )
        finished = self.payload("/api/review/linkedin-card")
        self.assertIsNone(finished["card"])
        self.assertIs(finished["finished"]["synthesize_pending"], True)


class WorthRoutesTests(ReviewApiFixture):
    def test_worth_card_picks_the_person_the_jinja_route_picks(self) -> None:
        self.store.seed_worth_queue()
        keys = [row.key for row in worth_queue(self.db)]
        self.assertEqual(len(keys), 3)
        queries = (
            "",
            "?index=1",
            "?index=2",
            "?index=7",
            "?index=-3",
            "?index=x",
            "?debug=1",
            "?debug=1&index=2",
            f"?exclude={keys[0]}",
            f"?exclude={keys[0]},{keys[1].upper()}",
            f"?exclude={keys[0]}&index=1&debug=1",
            f"?pick={keys[2]}",
            f"?pick={keys[1].upper()}&debug=1",
            f"?pick={keys[1]}&exclude={keys[1]}",
            f"?exclude={','.join(keys)}",
        )
        picked = set()
        for query_string in queries:
            with self.subTest(query=query_string):
                payload = self.payload(f"/api/review/worth-card{query_string}")
                status, old = self.jinja("GET", f"/api/worth-card{query_string}")
                self.assertEqual(status, 200)
                self.assertIs(payload["synthesize_pending"], False)
                shown = re.search(r"data-pub='(.*?)' data-parent='(.*?)'>No</button>", old)
                if shown is None:
                    self.assertEqual((old, payload["card"], payload["queue"]), ("", None, None))
                    continue
                person = payload["card"]["person"]
                self.assertEqual((person["worth_key"], person["slug"]), shown.groups())
                picked.add(person["slug"])
                position = re.search(r"data-queue-index='(\d+)' data-queue-total='(\d+)'", old)
                self.assertEqual(
                    payload["queue"],
                    {"index": int(position.group(1)), "total": int(position.group(2))} if position else None,
                )
        self.assertEqual(picked, {"casey-delta", "morgan-echo", "avery-quinn"})

    def test_worth_card_orders_names_the_way_the_jinja_route_does(self) -> None:
        # SQLite's lower() folds ASCII only; the queue is ordered by Python's.
        self.store.seed("emile-parent", "emile-foxtrot", "Émile Foxtrot", "maybe")
        self.store.seed("elodie-parent", "elodie-golf", "élodie Golf", "maybe")
        names = []
        for index in range(3):
            card = self.payload(f"/api/review/worth-card?index={index}")["card"]
            _, old = self.jinja("GET", f"/api/worth-card?index={index}")
            self.assertIn(f"data-parent='{card['person']['slug']}'", old)
            names.append(card["person"]["name"])
        self.assertEqual(names, ["Casey Delta", "élodie Golf", "Émile Foxtrot"])

    def test_worth_card_is_gone_once_its_pick_is_decided(self) -> None:
        key = worth_queue(self.db)[0].key
        self.db.decide_worth("worth-parent", "yes")
        for pick in ("missing", key):
            with self.subTest(pick=pick):
                self.assertEqual(self.get_json(f"/api/review/worth-card?pick={pick}"), (404, {"error": "gone"}))
                self.assertEqual(self.jinja("GET", f"/api/worth-card?pick={pick}"), (404, "gone"))

    def test_worth_card_is_gone_when_its_parent_left_the_store(self) -> None:
        with mock.patch.object(review_api, "person_detail", return_value=None):
            self.assertEqual(self.get_json("/api/review/worth-card"), (404, {"error": "gone"}))

    def test_worth_card_carries_what_the_jinja_card_shows(self) -> None:
        replace_person_sources(
            self.db,
            "worth-parent-person",
            tuple(
                PersonSourceRow("worth-parent-person", source)
                for source in ("whatsapp", "gmail_msgvault", "linkedin_csv")
            ),
        )
        card = self.payload("/api/review/worth-card")["card"]
        _, old = self.jinja("GET", "/api/worth-card")
        person, candidate = card["person"], card["candidate"]
        # Message sources only, in the badges' order.
        self.assertEqual(person["sources"], ["gmail", "whatsapp"])
        self.assertEqual(person["sources"], re.findall(r"<span class='source source-(\w+)'>", old))
        self.assertEqual(person["labels"], CASEY_TITLES)
        self.assertEqual(person["labels"], jinja_labels(old))
        self.assertEqual((person["parent_id"], person["name"]), ("worth-parent", "Casey Delta"))
        self.assertIn(f"<h2>{candidate['name']}</h2>", old)
        self.assertEqual(candidate["contacts"], "casey-delta@example.com · +15550100")
        self.assertIn(f"<dd>{candidate['contacts']}</dd>", old)
        self.assertNotIn("/api/avatar?pub=", old)
        self.assertIn("<span>CD</span>", old)
        self.assertEqual((candidate["url"], candidate["synthetic"]), ("", False))

    def test_worth_card_without_a_candidate_has_a_null_candidate(self) -> None:
        seed_identity(
            self.db,
            parent_id="bare-parent",
            person_id="bare-person",
            row_key="unused",
            name="Aaron Bare",
            machine_worth="maybe",
            display_slug="aaron-bare",
            include_link=False,
        )
        card = self.payload("/api/review/worth-card?pick=parent-worth:bare-parent")["card"]
        self.assertEqual(card["person"]["name"], "Aaron Bare")
        self.assertIsNone(card["candidate"])

    def test_worth_pending_is_the_typeahead_list_the_jinja_page_embeds(self) -> None:
        self.store.seed_worth_queue()
        pending = self.payload("/api/review/worth-pending")["pending"]
        _, old = self.jinja("GET", "/?stage=worth")
        embedded = re.search(r"<script type='application/json' data-worth-pending>(.*?)</script>", old)
        self.assertEqual(pending, json.loads(embedded.group(1)))
        self.assertEqual([entry["name"] for entry in pending], ["Avery Quinn", "Casey Delta", "Morgan Echo"])

    def test_worth_table_is_the_pile_the_jinja_table_shows(self) -> None:
        self.store.seed_worth_queue()
        self.store.seed_linkedin_queue()
        self.db.decide_worth("worth-parent", "yes", note="Synthetic note")
        self.db.decide_worth("morgan-parent", "no")
        self.db.decide_worth("avery-parent", "no", note="Synthetic refusal")
        for pile in ("yes", "no"):
            with self.subTest(pile=pile):
                table = self.payload(f"/api/review/worth-table?view={pile}")
                _, old = self.jinja("GET", f"/?stage=worth&view={pile}")
                rows = table["rows"]
                self.assertEqual(
                    [row["person"]["name"] for row in rows], re.findall(r"<strong>(.*?)</strong>", old)
                )
                self.assertEqual(
                    [row["reason"] for row in rows], re.findall(r"<dt>Why (?:yes|no)</dt><dd>(.*?)</dd>", old)
                )
                self.assertEqual(
                    [(row["person"]["worth_key"], row["person"]["slug"]) for row in rows],
                    re.findall(r"data-pub='(.*?)'\s+data-parent='(.*?)' aria-label", old),
                )
                tabs = dict(re.findall(r"data-tab='(\w+)'\s+href='[^']*'>\w+<span>(\d+)</span>", old))
                self.assertEqual(table["total"], int(tabs[pile]))
                self.assertEqual(table["total"], len(rows))
                for row in rows:
                    if row["candidate"] and row["candidate"]["contacts"]:
                        self.assertIn(f"<dd>{row['candidate']['contacts']}</dd>", old)

        yes = self.payload("/api/review/worth-table?view=YES")["rows"]
        self.assertEqual(yes[0]["person"]["labels"], CASEY_TITLES)
        self.assertEqual(
            [row["reason"] for row in yes], ["Synthetic note", "fixture", "fixture", "fixture"]
        )
        no = self.payload("/api/review/worth-table?view=no")["rows"]
        self.assertEqual([row["reason"] for row in no], ["Synthetic refusal", "You said no"])

    def test_worth_table_pages_by_offset_like_the_jinja_route(self) -> None:
        self.store.seed_linkedin_queue()
        for offset in ("0", "1", "2", "9", "-4"):
            with self.subTest(offset=offset):
                table = self.payload(f"/api/review/worth-table?view=yes&offset={offset}")
                _, old = self.jinja("GET", f"/api/worth-table?view=yes&offset={offset}")
                self.assertEqual(
                    [row["person"]["name"] for row in table["rows"]], re.findall(r"<strong>(.*?)</strong>", old)
                )
                self.assertEqual(table["total"], 3)

    def test_worth_table_refuses_a_bad_view_or_offset(self) -> None:
        for query_string in ("", "?view=maybe", "?view=review", "?view=yes&offset=x"):
            with self.subTest(query=query_string):
                status, payload = self.get_json(f"/api/review/worth-table{query_string}")
                old_status, old = self.jinja("GET", f"/api/worth-table{query_string}")
                self.assertEqual((status, payload), (old_status, {"error": old}))
                self.assertEqual(status, 400)

    def test_decision_reason_follows_the_jinja_row_rule(self) -> None:
        parent = person_detail(self.db, "worth-parent")
        long_reason = "Synthetic reason. " * 12
        human = WorthHumanRow("yes", "2026-09-30T00:00:00Z", "")
        cases = (
            ("machine reason", replace(parent.worth_row, machine=WorthMachineRow("yes", "Synthetic fit", "llm"))),
            ("long machine reason", replace(parent.worth_row, machine=WorthMachineRow("yes", long_reason, "llm"))),
            ("no machine reason", replace(parent.worth_row, machine=WorthMachineRow("yes", "", "llm"))),
            ("null machine reason", replace(parent.worth_row, machine=WorthMachineRow("yes", None, "llm"))),
            ("human note", replace(parent.worth_row, human=replace(human, note="Met at a synthetic dinner"))),
            ("human yes", replace(parent.worth_row, human=human, effective="yes")),
            ("human no", replace(parent.worth_row, human=replace(human, decision="no"), effective="no")),
        )
        for name, worth_row in cases:
            for pile in ("yes", "no"):
                with self.subTest(case=name, pile=pile):
                    row_parent = replace(parent, worth_row=worth_row)
                    row = DecisionRow.from_parent(row_parent, pile)
                    old = decision_rows_html([row_parent], pile)
                    drawn = re.search(r"<dt>Why (?:yes|no)</dt><dd>(.*?)</dd>", old).group(1)
                    self.assertEqual(row.reason, html.unescape(drawn))

        cut = DecisionRow.from_parent(replace(parent, worth_row=cases[1][1]), "yes").reason
        self.assertEqual((len(cut), cut[-1]), (141, "…"))
        empty = replace(parent, worth_row=cases[2][1])
        self.assertEqual(DecisionRow.from_parent(empty, "yes").reason, "Worth adding")
        self.assertEqual(DecisionRow.from_parent(empty, "no").reason, "Not worth adding")


class LinkedinRoutesTests(ReviewApiFixture):
    def retarget(self, slug: str, state: str, detail: str = "") -> GuidanceViewRow:
        return GuidanceViewRow(
            slug=slug, row_key=slug, name="", guidance="Synthetic guidance", state=state, detail=detail,
            submitted_at="", updated_at="", new_url="", wire_fields=(),
        )

    def assert_card_parity(self, query_string: str) -> dict:
        payload = self.payload(f"/api/review/linkedin-card{query_string}")
        status, old = self.jinja("GET", f"/api/linkedin-card{query_string}")
        self.assertEqual(status, 200)
        shown = re.search(r"<article class='decision-card identity-card[^']*' data-card data-parent='(.*?)'>", old)
        if shown is None:
            self.assertIsNone(payload["card"])
            self.assertIsNone(payload["queue"])
            finished = payload["finished"]
            self.assertIn("LinkedIn Profiles Checked", old)
            self.assertIn(f"<p>{finished['linkedin_done']} decisions saved</p>", old)
            self.assertEqual("data-auto-complete" in old, finished["auto_continue"])
            self.assertEqual("data-complete='linkedin'" in old, not finished["linkedin_complete"])
            self.assertEqual("Review complete, continue" in old, finished["linkedin_complete"])
            running = re.search(r"<p>(\d+) re-research still running</p>", old)
            self.assertEqual(finished["retargets_in_flight"], int(running.group(1)) if running else 0)
            return payload

        self.assertIsNone(payload["finished"])
        card = payload["card"]
        self.assertEqual(card["person"]["slug"], shown.group(1))
        self.assertEqual(
            [candidate["row_key"] for candidate in card["candidates"]],
            re.findall(r"data-decide='keep' data-pub='(.*?)'", old),
        )
        failed = re.search(r"<div class='reresearch-failed'>Re-research failed: (.*?)</div>", old)
        self.assertEqual(card["failure_note"], html.unescape(failed.group(1)) if failed else "")
        position = re.search(r"data-queue-index='(\d+)' data-queue-total='(\d+)'", old)
        self.assertEqual(
            payload["queue"],
            {"index": int(position.group(1)), "total": int(position.group(2))} if position else None,
        )
        return payload

    def test_linkedin_card_picks_the_parent_the_jinja_route_picks(self) -> None:
        self.store.seed_linkedin_queue()
        queries = (
            "",
            "?index=1",
            "?index=2",
            "?index=4",
            "?index=x",
            "?debug=1&index=1",
            "?exclude=jordan-bravo",
            "?exclude=JORDAN-BRAVO, sam-tango",
            "?exclude=riley-stone&index=1&debug=1",
            "?exclude=jordan-bravo,riley-stone,sam-tango",
        )
        picked = set()
        for query_string in queries:
            with self.subTest(query=query_string):
                payload = self.assert_card_parity(query_string.replace(" ", "%20"))
                self.assertEqual(payload["pending"], 3)
                if payload["card"]:
                    picked.add(payload["card"]["person"]["slug"])
        self.assertEqual(picked, {"jordan-bravo", "riley-stone", "sam-tango"})

    def test_linkedin_card_hides_in_flight_research_and_reports_a_failed_one(self) -> None:
        self.store.seed_linkedin_queue()
        first = self.payload("/api/review/linkedin-card")["card"]["person"]["slug"]
        retargets = [
            self.retarget(first, "researching"),
            self.retarget("sam-tango", "failed", "Synthetic provider outage"),
            self.retarget("sam-tango", "failed", "An older synthetic failure"),
            self.retarget("riley-stone", "failed"),
        ]
        with mock.patch.object(SqliteReviewAdapter, "retargets", return_value=retargets):
            notes = {}
            for index in (0, 1):
                payload = self.assert_card_parity(f"?index={index}")
                self.assertEqual(payload["pending"], 3)
                notes[payload["card"]["person"]["slug"]] = payload["card"]["failure_note"]
            everyone_else = ",".join(notes)
            finished = self.assert_card_parity(f"?exclude={everyone_else}")
        self.assertNotIn(first, notes)
        self.assertEqual(
            notes, {"sam-tango": "Synthetic provider outage", "riley-stone": "the job did not finish"}
        )
        self.assertEqual(
            finished["finished"],
            {
                "synthesize_pending": False,
                "linkedin_done": 0,
                "linkedin_complete": False,
                "retargets_in_flight": 1,
                "auto_continue": True,
            },
        )
        self.assertEqual(finished["pending"], 3)

    def test_research_landing_mid_request_never_serves_a_blank_card(self) -> None:
        self.store.seed_linkedin_queue()
        first = self.payload("/api/review/linkedin-card")["card"]["person"]["slug"]
        # The first parent is being re-researched; the worker's result lands right
        # after this request read the queue's order.
        landed = False
        read_order = review_api.linkedin_queue_order

        def order_then_land(db: Db) -> list:
            nonlocal landed
            order = read_order(db)
            db.decide_identity(first, "verify")
            landed = True
            return order

        def retargets(_adapter: SqliteReviewAdapter) -> list[GuidanceViewRow]:
            return [] if landed else [self.retarget(first, "researching")]

        with (
            mock.patch.object(review_api, "linkedin_queue_order", order_then_land),
            mock.patch.object(SqliteReviewAdapter, "retargets", retargets),
        ):
            payload = self.payload("/api/review/linkedin-card")
        self.assertNotEqual(payload["card"]["person"]["slug"], first)
        self.assertTrue(payload["card"]["candidates"])

    def test_linkedin_card_lists_every_pending_candidate(self) -> None:
        self.db.project_rows(
            (
                LinkRow(
                    "jordan-bravo-alt",
                    "linkedin-parent",
                    "jordan-bravo-alt",
                    RowKind.PUB.value,
                    "https://www.linkedin.com/in/jordan-bravo-alt",
                    "Jordan B. Bravo",
                    machine_action="verify",
                    machine_confidence=0.5,
                    paid_profile=1,
                    source=WriterSource.RECONCILE.value,
                ),
            )
        )
        replace_candidate_people(self.db, "jordan-bravo-alt", ())
        card = self.assert_card_parity("")["card"]
        self.assertEqual(
            sorted((candidate["row_key"], candidate["name"]) for candidate in card["candidates"]),
            [("jordan-bravo", "Jordan Bravo"), ("jordan-bravo-alt", "Jordan B. Bravo")],
        )
        self.assertEqual(
            {candidate["url"] for candidate in card["candidates"]},
            {"https://www.linkedin.com/in/jordan-bravo", "https://www.linkedin.com/in/jordan-bravo-alt"},
        )

    def test_finished_state_follows_the_queue(self) -> None:
        self.store.reach_done()
        finished = self.assert_card_parity("")
        self.assertEqual(
            finished,
            {
                "card": None,
                "finished": {
                    "synthesize_pending": False,
                    "linkedin_done": 1,
                    "linkedin_complete": True,
                    "retargets_in_flight": 0,
                    "auto_continue": False,
                },
                "pending": 0,
                "queue": None,
            },
        )

    def test_candidate_carries_what_the_jinja_cards_show(self) -> None:
        parent = person_detail(self.db, "linkedin-parent")
        base = replace(
            parent.candidates[0],
            headline="Synthetic operator",
            location="Example City",
            experiences=("Founder @ Bravo Robotics", " ", "Engineer @ Example Labs"),
            education=("BS — Example University", ""),
            match_emails=("jordan@example.com", "jordan@example.com"),
            match_phones=("+14155550100", "4155550100", "+14155550101"),
        )
        linked = ReviewCandidate.from_row(base)
        self.assertEqual(
            dataclasses.asdict(linked),
            {
                "row_key": "jordan-bravo",
                "name": "Jordan Bravo",
                "url": "https://www.linkedin.com/in/jordan-bravo",
                "headline": "Synthetic operator",
                "location": "Example City",
                "experiences": ("Founder @ Bravo Robotics", "Engineer @ Example Labs"),
                "education": ("BS — Example University",),
                "synthetic": False,
                "contacts": "jordan@example.com · +14155550100 · +14155550101",
            },
        )
        researched = ReviewCandidate.from_row(replace(base, synthetic=True, url="https://example.com/researched"))
        self.assertEqual((researched.synthetic, researched.url), (True, ""))

        for candidate, payload in ((base, linked), (replace(base, synthetic=True), researched)):
            rich = replace(parent, candidates=(candidate,))
            for old in (render_worth_card(rich), render_linkedin_card(rich, rich.candidates)):
                with self.subTest(synthetic=candidate.synthetic, card=old[:40]):
                    drawn = html.unescape(old)
                    for value in (payload.name, payload.headline, payload.location, payload.contacts,
                                  *payload.experiences, *payload.education):
                        self.assertIn(f">{value}<", drawn)
                    self.assertEqual(drawn.count("<li"), len(payload.experiences) + len(payload.education))
                    self.assertEqual(f"href='{candidate.url}'" in drawn, bool(payload.url))

        labelled = person_detail(self.db, "worth-parent")
        self.assertEqual(list(ReviewPerson.from_parent(labelled).labels), jinja_labels(render_worth_card(labelled)))
        self.assertEqual(ReviewPerson.from_parent(parent).labels, ())


class DecideTests(ReviewApiFixture):
    def test_decide_writes_the_rows_the_jinja_route_writes(self) -> None:
        cases = (
            ({"decision": "keep"}, "verify"),
            ({"decision": "detach", "note": "  Synthetic note  "}, "detach"),
            ({"decision": "fix", "new_url": "https://www.linkedin.com/in/jordan-bravo-correct"}, "retarget"),
            ({"decision": "exclude"}, "exclude"),
        )
        for index, (fields, action) in enumerate(cases):
            with self.subTest(decision=fields["decision"]):
                old_store = ReviewStore(self.root / f"old-{index}")
                new_store = ReviewStore(self.root / f"new-{index}")
                for store in (old_store, new_store):
                    store.seed_linkedin_queue()
                form = {"pub": "jordan-bravo", "parent_slug": "jordan-bravo", **fields}
                old_status, _, old_body, _ = old_store.http.request("POST", "/decide", form)
                status, written = self.post_json("/api/review/decide", form, http=new_store.http)
                self.assertEqual((status, old_status), (200, 200))
                old = json.loads(old_body)
                self.assertEqual(written["action"], action)
                for key in ("ok", "pub", "action", "approved", "new_url", "progress", "resolved_pubs"):
                    self.assertEqual(written[key], old[key], key)
                self.assertEqual(new_store.links(), old_store.links())
                decided = next(row for row in new_store.links() if row["row_key"] == "jordan-bravo")
                self.assertEqual(decided["decision_action"], action)

                # The same one-shot reset, through both routes.
                reset = {"pub": "jordan-bravo", "parent_slug": "jordan-bravo", "decision": "reset"}
                old_store.http.request("POST", "/decide", reset)
                status, _ = self.post_json("/api/review/decide", reset, http=new_store.http)
                self.assertEqual(status, 200)
                self.assertEqual(new_store.links(), old_store.links())

    def test_decide_answers_with_the_next_card_and_the_counts(self) -> None:
        self.store.seed_linkedin_queue()
        status, written = self.post_json(
            "/api/review/decide", {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(written["next"], self.payload("/api/review/linkedin-card?exclude=jordan-bravo"))
        self.assertEqual(written["next"]["pending"], 2)
        self.assertNotEqual(written["next"]["card"]["person"]["slug"], "jordan-bravo")
        self.assertEqual(
            written["progress"], {"worth_pending": 1, "worth_yes": 3, "worth_no": 0, "linkedin_pending": 2}
        )
        self.assertEqual((written["ok"], written["pub"], written["approved"]), (True, "jordan-bravo", "yes"))

    def test_decide_never_serves_the_decided_parent_back(self) -> None:
        # A reset leaves the parent pending; with or without its slug, the next card is not it.
        for fields in ({"parent_slug": "jordan-bravo"}, {}):
            with self.subTest(fields=fields):
                status, written = self.post_json(
                    "/api/review/decide", {"pub": "jordan-bravo", "decision": "reset", **fields}
                )
                self.assertEqual(status, 200)
                self.assertIsNone(written["next"]["card"])
                self.assertEqual(written["next"]["pending"], 1)
                self.assertEqual(written["progress"]["linkedin_pending"], 1)
                self.assertIs(written["next"]["finished"]["linkedin_complete"], False)

    def test_last_decision_answers_with_the_finished_state(self) -> None:
        self.store.reach_linkedin()
        status, written = self.post_json(
            "/api/review/decide", {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(written["progress"]["linkedin_pending"], 0)
        self.assertEqual(
            written["next"]["finished"],
            {
                "synthesize_pending": False,
                "linkedin_done": 1,
                "linkedin_complete": True,
                "retargets_in_flight": 0,
                "auto_continue": False,
            },
        )

    def test_decide_loads_one_card_and_no_workflow_state(self) -> None:
        self.store.seed_linkedin_queue()
        with (
            mock.patch.object(view_rows, "_hydrate_parents", wraps=view_rows._hydrate_parents) as hydrate,
            mock.patch.object(review_adapter, "workflow_state", wraps=review_adapter.workflow_state) as workflow_state,
        ):
            status, _ = self.post_json(
                "/api/review/decide", {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(max(len(call.args[1]) for call in hydrate.call_args_list), 1)
        self.assertEqual(workflow_state.call_count, 0)

    def test_decide_resolves_a_public_identifier_to_its_row_key(self) -> None:
        seed_identity(
            self.db,
            parent_id="opaque-parent",
            person_id="opaque-person",
            row_key="identity-row-42",
            public_identifier="public-jordan",
            name="Jordan Opaque",
            machine_worth="yes",
            display_slug="jordan-opaque",
            linkedin_url="https://www.linkedin.com/in/public-jordan",
        )
        status, written = self.post_json(
            "/api/review/decide", {"pub": "public-jordan", "parent_slug": "jordan-opaque", "decision": "detach"}
        )
        self.assertEqual((status, written["pub"]), (200, "identity-row-42"))
        row = query(self.db, "SELECT decision_action FROM links WHERE row_key='identity-row-42'")[0]
        self.assertEqual(row["decision_action"], "detach")

    def test_decide_refuses_what_the_jinja_route_refuses(self) -> None:
        cases = (
            ({}, 400, "bad request"),
            ({"pub": "jordan-bravo", "decision": "maybe"}, 400, "bad request"),
            ({"decision": "keep"}, 400, "bad request"),
            ({"pub": "nobody", "decision": "keep"}, 404, "review row not found: nobody"),
            (
                {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "casey-delta"},
                400,
                "stale or mismatched person card",
            ),
            ({"pub": "jordan-bravo", "decision": "fix", "new_url": ""}, 400, "fix needs a LinkedIn URL"),
        )
        before = self.store.links()
        for fields, expected_status, text in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.post_json("/api/review/decide", fields), (expected_status, {"error": text}))
                self.assertEqual(self.jinja("POST", "/decide", fields), (expected_status, text))
        self.assertEqual(self.store.links(), before)

    def test_decide_refuses_a_row_no_card_ever_showed(self) -> None:
        # The owner's own identity row resolves, but it is nobody's candidate.
        self.db.project_rows(
            (
                PersonRow("owner-person", "linkedin-parent", display_name="Owner Sierra", is_owner=True),
                LinkRow(
                    "owner-row",
                    "linkedin-parent",
                    "owner-row",
                    RowKind.PUB.value,
                    "https://www.linkedin.com/in/owner-row",
                    "Owner Sierra",
                    source=WriterSource.RECONCILE.value,
                ),
            )
        )
        replace_candidate_people(
            self.db, "owner-row", (CandidatePersonRow("owner-row", "owner-person", "linkedin-parent"),)
        )
        fields = {"pub": "owner-row", "decision": "keep"}
        self.assertEqual(
            self.post_json("/api/review/decide", fields), (404, {"error": "review row not found: owner-row"})
        )
        self.assertEqual(self.jinja("POST", "/decide", fields), (404, "review row not found: owner-row"))

    def test_decide_refuses_an_ambiguous_public_identifier(self) -> None:
        self.db.project_rows(
            tuple(
                LinkRow(
                    f"ambiguous-row-{index}",
                    "linkedin-parent",
                    "ambiguous-public-identifier",
                    RowKind.PUB.value,
                    f"https://www.linkedin.com/in/ambiguous-row-{index}",
                    f"Ambiguous {index}",
                    source=WriterSource.RECONCILE.value,
                )
                for index in (1, 2)
            )
        )
        fields = {"pub": "ambiguous-public-identifier", "decision": "keep"}
        status, payload = self.post_json("/api/review/decide", fields)
        self.assertEqual(
            (status, payload), (400, {"error": "ambiguous identity candidate: ambiguous-public-identifier"})
        )
        self.assertEqual(self.jinja("POST", "/decide", fields), (400, payload["error"]))

    def test_posts_refuse_another_origin_and_accept_this_machine(self) -> None:
        before = self.store.links()
        fields = {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        for path in ("/api/review/decide", "/api/review/approve-enrichment"):
            with self.subTest(path=path), mock.patch.object(
                enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("refused first")
            ):
                status, payload = self.post_json(path, fields, headers={"Origin": "https://example.test"})
                self.assertEqual((status, payload), (403, {"error": "cross-origin request rejected"}))
        self.assertEqual(self.store.links(), before)

        for origin in ("http://127.0.0.1:8765", "http://localhost:5173"):
            with self.subTest(origin=origin):
                status, _ = self.post_json(
                    "/api/review/decide", {**fields, "decision": "reset"}, headers={"Origin": origin}
                )
                self.assertEqual(status, 200)


class EnrichmentPanelTests(ReviewApiFixture):
    def test_panel_takes_the_branch_the_jinja_panel_takes(self) -> None:
        base = SqliteReviewAdapter(self.db, 0.7).enrichment()
        counts = base.counts
        cases = (
            (
                replace(base, status="running", state="running", counts=replace(counts, total=4, completed=1)),
                EnrichmentPanel("running", completed=1, total=4),
            ),
            # The bar never overshoots or goes negative.
            (
                replace(base, status="running", state="running", counts=replace(counts, total=4, completed=9)),
                EnrichmentPanel("running", completed=4, total=4),
            ),
            (
                replace(base, status="running", state="running", counts=replace(counts, total=-2, completed=-1)),
                EnrichmentPanel("running", completed=0, total=0),
            ),
            (
                replace(base, status="needs_approval", state="needs_approval", estimated_usd=2.85, would_submit=57),
                EnrichmentPanel("approval", approval_label="Approve $2.85"),
            ),
            (
                replace(base, status="not_started", state="profile_prep_pending", would_submit=0, estimated_usd=0),
                EnrichmentPanel("approval", approval_label="Prepare profiles and judge LinkedIns"),
            ),
            # Cached research that still needs the free local chain is a $0 continue.
            (
                replace(base, status="completed", state="profile_prep_pending", would_submit=0, estimated_usd=0),
                EnrichmentPanel("approval", approval_label="Prepare profiles and judge LinkedIns"),
            ),
            (replace(base, status="completed", state="done"), EnrichmentPanel("completed")),
            (
                replace(base, status="failed", state="failed", error="research stopped with status failed"),
                EnrichmentPanel("failed", error="research stopped with status failed"),
            ),
            (replace(base, status="not_started", state="not_started"), EnrichmentPanel("preparing")),
        )
        self.assertEqual({panel.mode for _, panel in cases}, set(get_args(EnrichmentMode)))
        for view, expected in cases:
            with self.subTest(status=view.status, state=view.state):
                panel = EnrichmentPanel.from_view(view)
                self.assertEqual(panel, expected)
                old = render_enrichment(view)
                self.assertIn(f"<h2>{ENRICH_HEADINGS[panel.mode]}</h2>", old)
                if panel.mode == "running":
                    self.assertIn(f"<p>{panel.completed} of {panel.total} complete</p>", old)
                    self.assertIn(f"aria-valuemax='{panel.total}' aria-valuenow='{panel.completed}'", old)
                if panel.mode == "approval":
                    self.assertIn(f"data-approve-enrichment>{panel.approval_label}</button>", old)
                if panel.mode == "failed":
                    self.assertIn(f"<p>{panel.error}</p>", old)

    def test_page_carries_the_stores_enrichment_panel(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        page = self.payload("/api/review/page")
        self.assertEqual(page["view"], "enrich")
        self.assertEqual(
            page["enrichment"],
            {"mode": "approval", "completed": 0, "total": 0, "approval_label": "Approve $0.08", "error": ""},
        )
        self.assertEqual((page["steps"][1]["complete"], page["steps"][1]["count"]), (False, 1))


class ApproveEnrichmentTests(ReviewApiFixture):
    def wait_for_enrichment_job(self, status: str) -> dict:
        expected = "completed" if status == "applied" else status
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.enrichment_manifest.exists():
                payload = read_json(self.enrichment_manifest, {})
                if payload.get("status") == expected:
                    return payload
            time.sleep(0.01)
        self.fail(f"enrichment job did not reach {status}")

    def test_page_and_card_reads_never_launch_the_pipeline(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("GET must not start work")
        ) as start:
            http = self.store.client()
            for path in ("/api/review/page?stage=enrich", "/api/review/worth-card", "/api/review/linkedin-card"):
                self.assertEqual(self.get_json(path, http=http)[0], 200)
        start.assert_not_called()

    def test_running_enrichment_approval_is_idempotent(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        entered, release = threading.Event(), threading.Event()

        def reconcile_run() -> ResearchOutcome:
            entered.set()
            self.assertTrue(release.wait(5))
            return ResearchOutcome(ReceiptStatus.RAN, ReceiptCounts(1, 1, 0, 0), None, 0.0, 0)

        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile"),
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
            mock.patch.object(enrichment_pipeline, "judge_mapped_candidates") as mapped_judge,
            mock.patch.object(enrichment_pipeline, "ReviewRelationships") as relationships,
        ):
            reconcile.return_value.run.side_effect = reconcile_run
            prefetch.return_value.run.return_value.status = "completed"
            prefetch.return_value.run.return_value.note = None
            mapped_judge.return_value.judge_errors = 0
            relationships.return_value.run.return_value = {"status": "completed"}
            running = {"mode": "running", "completed": 0, "total": 1, "approval_label": "", "error": ""}
            # This reconcile blocks, so the first answer is deterministically the running panel.
            self.assertEqual(
                self.post_json("/api/review/approve-enrichment"), (200, {"ok": True, "enrichment": running})
            )
            self.assertTrue(entered.wait(5))
            self.assertEqual(
                self.post_json("/api/review/approve-enrichment"), (200, {"ok": True, "enrichment": running})
            )
            self.assertEqual(self.payload("/api/review/page?stage=enrich")["enrichment"], running)
            release.set()
            self.wait_for_enrichment_job("applied")
            self.assertEqual(reconcile.return_value.run.call_count, 1)
            self.assertEqual(reconcile.call_args.kwargs["budget"], 0.08)
            self.assertIs(reconcile.call_args.kwargs["approve"], True)

    def test_approval_starts_the_pipeline_the_jinja_route_starts(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        calls = []
        for path in ("/approve-enrichment", "/api/review/approve-enrichment"):
            with mock.patch.object(enrichment_pipeline.EnrichmentPipeline, "start", return_value=False) as start:
                status, content_type, body, _ = self.store.client().request("POST", path, {})
            self.assertEqual((status, content_type), (200, JSON_TYPE))
            start.assert_called_once()
            calls.append(start.call_args)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[1].args[:2], (1, 0.079004))
        # Not launched: the panel is the store's, still waiting for the approval.
        self.assertEqual(
            json.loads(body),
            {
                "ok": True,
                "enrichment": {
                    "mode": "approval", "completed": 0, "total": 0, "approval_label": "Approve $0.08", "error": "",
                },
            },
        )

    def test_a_launch_wakes_the_agent_and_a_refused_one_does_not(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        for launched in (True, False):
            with self.subTest(launched=launched):
                notifier = mock.Mock()
                with mock.patch.object(enrichment_pipeline.EnrichmentPipeline, "start", return_value=launched):
                    handler = review_server.make_handler(
                        confirm_threshold=0.7, run_jobs=True, guided_retargets=mock.Mock(), db=self.db,
                        agent_notifier=notifier,
                    )
                    status, _ = self.post_json("/api/review/approve-enrichment", http=InProcessHttpClient(handler))
                self.assertEqual(status, 200)
                self.assertEqual(notifier.call_count, int(launched))

    def test_finished_enrichment_has_nothing_to_approve(self) -> None:
        self.store.reach_linkedin()
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("nothing to start")
        ):
            status, payload = self.post_json("/api/review/approve-enrichment", http=self.store.client())
        self.assertEqual(status, 200)
        self.assertEqual(payload["enrichment"]["mode"], "completed")

    def test_disabled_job_execution_refuses_the_approval(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("jobs are disabled")
        ):
            http = self.store.client(run_jobs=False)
            status, payload = self.post_json("/api/review/approve-enrichment", http=http)
            old_status, _, old_body, _ = http.request("POST", "/approve-enrichment", {})
        self.assertEqual((status, payload), (409, {"error": "enrichment job execution is disabled"}))
        self.assertEqual((old_status, old_body.decode()), (409, payload["error"]))

    def test_disabled_job_execution_refuses_a_computed_approval(self) -> None:
        base = SqliteReviewAdapter(self.db, 0.7).enrichment()
        approved = replace(
            base,
            status="needs_approval",
            approval=EnrichmentApproval(
                status="approved",
                approved_at="2026-08-06T00:00:00Z",
                approved_budget_usd=0.05,
                estimated_usd=0.05,
                would_submit=1,
            ),
        )
        with mock.patch.object(SqliteReviewAdapter, "approve_enrichment", return_value=approved):
            status, payload = self.post_json("/api/review/approve-enrichment", http=self.store.client(run_jobs=False))
        self.assertEqual((status, payload), (409, {"error": "enrichment job execution is disabled"}))

    def test_an_approval_the_store_turns_down_is_a_conflict(self) -> None:
        refusal = StoreError("Enrichment is not waiting for approval")
        with mock.patch.object(SqliteReviewAdapter, "approve_enrichment", side_effect=refusal):
            status, payload = self.post_json("/api/review/approve-enrichment")
            old = self.jinja("POST", "/approve-enrichment", {})
        self.assertEqual((status, payload), (409, {"error": "Enrichment is not waiting for approval"}))
        self.assertEqual(old, (409, payload["error"]))


class TypeScriptPinTests(ReviewApiFixture):
    """web/src/types/review.ts is what the page reads; a rename on either side fails here."""

    def test_payload_dataclasses_match_the_interfaces(self) -> None:
        shapes = {
            "DecisionProgress": DecisionProgress,
            "PageProgress": PageProgress,
            "ReviewStep": ReviewStep,
            "EnrichmentPanel": EnrichmentPanel,
            "ReviewPage": ReviewPage,
            "ReviewPerson": ReviewPerson,
            "ReviewCandidate": ReviewCandidate,
            "QueuePosition": QueuePosition,
            "WorthCardPayload": WorthCardPayload,
            "WorthPendingEntry": WorthPendingEntry,
            "DecisionRow": DecisionRow,
            "WorthTablePayload": WorthTablePayload,
            "LinkedinFinished": LinkedinFinished,
            "LinkedinCardPayload": LinkedinCardPayload,
            "DecideResult": DecideResult,
            "ApproveResult": ApproveResult,
        }
        for interface, shape in shapes.items():
            with self.subTest(interface=interface):
                self.assertEqual(ts_fields(interface), field_names(shape))
        self.assertEqual(ts_inline_fields("WorthCardPayload", "card"), field_names(WorthCard))
        self.assertEqual(ts_inline_fields("LinkedinCardPayload", "card"), field_names(LinkedinCard))

    def test_vocabularies_match_the_unions(self) -> None:
        for name, literal in (
            ("ReviewView", ReviewView),
            ("WorthTab", WorthTab),
            ("EnrichmentMode", EnrichmentMode),
            ("LinkedinDecision", LinkedinDecision),
        ):
            with self.subTest(union=name):
                self.assertEqual(ts_union(name), set(get_args(literal)))
        self.assertEqual(ts_union("ReviewView"), set(review_api.TITLES))
        self.assertEqual(ts_union("ReviewView"), set(STAGE_BY_ACTION.values()))

    def test_live_payloads_carry_exactly_the_interface_fields(self) -> None:
        self.store.seed_linkedin_queue()

        def fields(interface: str) -> set[str]:
            return set(ts_fields(interface))

        page = self.payload("/api/review/page")
        self.assertEqual(set(page), fields("ReviewPage"))
        self.assertEqual(set(page["progress"]), fields("PageProgress"))
        self.assertEqual(set(page["enrichment"]), fields("EnrichmentPanel"))
        for step in page["steps"]:
            self.assertEqual(set(step), fields("ReviewStep"))

        worth = self.payload("/api/review/worth-card?debug=1")
        self.assertEqual(set(worth), fields("WorthCardPayload"))
        self.assertEqual(set(worth["card"]), set(ts_inline_fields("WorthCardPayload", "card")))
        self.assertEqual(set(worth["card"]["person"]), fields("ReviewPerson"))
        self.assertEqual(set(worth["card"]["candidate"]), fields("ReviewCandidate"))
        self.assertEqual(set(worth["queue"]), fields("QueuePosition"))

        pending = self.payload("/api/review/worth-pending")
        self.assertEqual(set(pending), {"pending"})
        self.assertEqual(set(pending["pending"][0]), fields("WorthPendingEntry"))

        table = self.payload("/api/review/worth-table?view=yes")
        self.assertEqual(set(table), fields("WorthTablePayload"))
        self.assertEqual(set(table["rows"][0]), fields("DecisionRow"))
        self.assertEqual(set(table["rows"][0]["person"]), fields("ReviewPerson"))
        self.assertIsNone(table["rows"][0]["candidate"])

        linkedin = self.payload("/api/review/linkedin-card?debug=1")
        self.assertEqual(set(linkedin), fields("LinkedinCardPayload"))
        self.assertEqual(set(linkedin["card"]), set(ts_inline_fields("LinkedinCardPayload", "card")))
        self.assertEqual(set(linkedin["card"]["candidates"][0]), fields("ReviewCandidate"))
        finished = self.payload("/api/review/linkedin-card?exclude=jordan-bravo,riley-stone,sam-tango")
        self.assertEqual(set(finished["finished"]), fields("LinkedinFinished"))

        _, decided = self.post_json(
            "/api/review/decide", {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        )
        self.assertEqual(set(decided), fields("DecideResult"))
        self.assertEqual(set(decided["progress"]), fields("DecisionProgress"))
        self.assertEqual(set(decided["next"]), fields("LinkedinCardPayload"))

        with mock.patch.object(enrichment_pipeline.EnrichmentPipeline, "start", return_value=False):
            _, approved = self.post_json("/api/review/approve-enrichment", http=self.store.client())
        self.assertEqual(set(approved), fields("ApproveResult"))
        self.assertEqual(set(approved["enrichment"]), fields("EnrichmentPanel"))

    def test_unchanged_routes_carry_the_fields_the_page_reads(self) -> None:
        _, status = self.get_json("/api/status")
        self.assertLessEqual(set(ts_fields("ReviewStatus")), set(status))
        self.assertIn(status["stage"], ts_union("ReviewView"))

        _, worth = self.post_json(
            "/worth", {"pub": "parent-worth:worth-parent", "worth": "yes", "parent_slug": "casey-delta"}
        )
        self.assertLessEqual(set(ts_fields("WorthResult")), set(worth))
        self.assertEqual(set(worth["progress"]), set(ts_fields("DecisionProgress")))


if __name__ == "__main__":
    unittest.main()
