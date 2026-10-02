"""The Review page's routes over a synthetic store: what each returns, what each writes.

The expected values are literal: they were frozen from the routes when the Jinja page they
mirrored was deleted (2026-10-01), so a change in what a route returns fails here.
The last class pins the payload dataclasses (review/payloads.py) to web/src/types/review.ts.
"""

from __future__ import annotations

import dataclasses
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
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids
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
from packs.ingestion.primitives.deep_context.db.view_models import (
    LinkedInQueueRow,
    WorthHumanRow,
    WorthMachineRow,
)
from packs.ingestion.primitives.deep_context.db.worth_views import worth_queue
from packs.ingestion.primitives.deep_context.enrich import enrichment_pipeline
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import (
    RelationshipDecision,
    finish_reviews,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import ResearchOutcome
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.review import api as review_api
from packs.ingestion.primitives.deep_context.review import auth_login
from packs.ingestion.primitives.deep_context.review import linkedin_queue
from packs.ingestion.primitives.deep_context.review import server as review_server
from packs.ingestion.primitives.deep_context.review import sqlite_adapter as review_adapter
from packs.ingestion.primitives.deep_context.review.enrichment import STAGE_BY_ACTION
from packs.ingestion.primitives.deep_context.review.models import EnrichmentApproval, GuidanceViewRow
from packs.ingestion.primitives.deep_context.review.payloads import (
    TITLES,
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
    ReviewView,
    WorthCardPayload,
    WorthDetails,
    WorthPendingEntry,
    WorthResult,
    WorthTab,
    WorthTablePayload,
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
# Each screen's title, and whether it watches the server (/api/events, /api/status).
SCREENS = {
    "worth": ("Add People", False),
    "enrich": ("Enrich Contacts", True),
    "linkedin": ("Check LinkedIn", False),
    "done": ("All Set", True),
}
# The columns a LinkedIn decision writes on its identity row.
DECISION_COLUMNS = (
    "decision_action",
    "decision_approved",
    "decision_source",
    "decision_note",
    "replacement_url",
    "replacement_public_identifier",
)
NOT_FINISHED = {
    "synthesize_pending": False,
    "linkedin_done": 0,
    "linkedin_complete": False,
    "retargets_in_flight": 0,
    "auto_continue": True,
}


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


def person(
    parent_id: str,
    slug: str,
    name: str,
    *,
    sources: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
    contacts: str = "",
) -> dict:
    """A `ReviewPerson` as JSON."""
    return {
        "parent_id": parent_id,
        "slug": slug,
        "name": name,
        "sources": list(sources),
        "labels": list(labels),
        "worth_key": f"parent-worth:{parent_id}",
        "contacts": contacts,
    }


def candidate(row_key: str, name: str, *, url: str = "") -> dict:
    """A `ReviewCandidate` as JSON: a profile nobody has fetched yet."""
    return {
        "row_key": row_key,
        "name": name,
        "url": url,
        "headline": "",
        "location": "",
        "experiences": [],
        "education": [],
        "synthetic": False,
        "avatar_url": "",
    }


def progress(pending: int, yes: int, no: int, linkedin: int, done: int = 0, rejected: int = 0, unsynthesized: int = 0) -> dict:
    """`PageProgress` as JSON."""
    return {
        "worth_pending": pending,
        "worth_yes": yes,
        "worth_no": no,
        "linkedin_pending": linkedin,
        "linkedin_done": done,
        "rejected": rejected,
        "synthesize_pending": unsynthesized,
    }


CASEY = person(
    "worth-parent",
    "casey-delta",
    "Casey Delta",
    labels=tuple(CASEY_TITLES),
    contacts="casey-delta@example.com · +15550100",
)
CASEY_CANDIDATE = candidate("candidate:email:casey-delta@example.com", "Casey Delta")
JORDAN = person("linkedin-parent", "jordan-bravo", "Jordan Bravo")
JORDAN_CANDIDATE = candidate("jordan-bravo", "Jordan Bravo", url="https://www.linkedin.com/in/jordan-bravo")
COMPLETED_PANEL = {"mode": "completed", "completed": 0, "total": 0, "approval_label": "", "error": ""}


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

    def add_second_candidate(self) -> None:
        """A second LinkedIn the Jordan parent might be."""
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

    def reach_enrich(self) -> None:
        self.db.decide_worth("worth-parent", "no", note="Synthetic note")

    def finish_questions(self) -> dict[str, str]:
        """What the enrichment pipeline's last step leaves: every pending LinkedIn handed to the reviewer."""
        decisions = []
        for parent_id in sorted(pending_parent_ids(self.db)):
            undecided = [
                row for row in links(self.db, parent_id=parent_id) if not row.decision_action and row.kind != "synthetic"
            ]
            urls = sorted({row.machine_proposed_url or row.linkedin_url for row in undecided} - {None, ""})
            choices = [
                {"url": url, "verdict": "review", "reason": "Owner can identify colleague", "confidence": 0.5}
                for url in urls
            ]
            decisions.append(RelationshipDecision.from_payload(parent_id, f"question:{parent_id}", {"candidates": choices}))
        finish_reviews(self.db, tuple(decisions))
        return {"status": "completed"}

    def reach_linkedin(self) -> None:
        self.reach_enrich()
        self.finish_questions()

    def reach_done(self) -> None:
        self.reach_linkedin()
        self.db.decide_identity("jordan-bravo", "verify")

    def estimate(self) -> float:
        """The spend the Enrich panel asks to approve right now."""
        return SqliteReviewAdapter(self.db, 0.7).enrichment().estimated_usd

    def link_rows(self) -> dict[str, dict]:
        """Every identity row by key, without the two clock columns."""
        rows = query(self.db, "SELECT * FROM links ORDER BY row_key")
        return {
            row["row_key"]: {key: row[key] for key in row.keys() if key not in {"decided_at", "updated_at"}}
            for row in rows
        }


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

    def payload(self, path: str) -> dict:
        status, payload = self.get_json(path)
        self.assertEqual(status, 200, payload)
        return payload


class ReviewPageTests(ReviewApiFixture):
    def test_page_is_the_screen_for_the_query_at_every_stage_of_the_store(self) -> None:
        approval = {
            "mode": "approval",
            "completed": 0,
            "total": 0,
            "approval_label": f"Approve ${self.store.estimate():.2f}",
            "error": "",
        }
        # With a person still to review, the worth stage is the worth screen.
        pending = {
            "?stage=worth": ("worth", "review"),
            "?stage=worth&view=review": ("worth", "review"),
        }
        # With none, the review tab opens Enrich; the piles and an unknown tab stay worth.
        emptied = {
            "?stage=worth": ("enrich", ""),
            "?stage=worth&view=review": ("enrich", ""),
        }
        asked = {
            "?stage=enrich": ("enrich", ""),
            "?stage=linkedin": ("linkedin", ""),
            "?stage=done": ("done", ""),
            "?stage=LINKEDIN": ("linkedin", ""),
            "?stage=worth&view=yes": ("worth", "yes"),
            "?stage=worth&view=no": ("worth", "no"),
            "?stage=worth&view=bogus": ("worth", "review"),
            "?stage=worth&view=YES": ("worth", "yes"),
            "?stage=worth&view=REVIEW": ("worth", "review"),
        }
        # Each driver moves the store one stage on; the page with no stage lands there.
        stores = (
            (
                "enrich", lambda: None, ("enrich", ""), pending,
                progress(1, 1, 0, 1), approval,
            ),
            (
                "enrich", self.store.reach_enrich, ("enrich", ""), emptied,
                progress(0, 1, 1, 1, rejected=1), approval,
            ),
            (
                "linkedin", self.store.reach_linkedin, ("linkedin", ""), emptied,
                progress(0, 1, 1, 1, rejected=1), COMPLETED_PANEL,
            ),
            (
                "done", self.store.reach_done, ("done", ""), emptied,
                progress(0, 1, 1, 0, done=1, rejected=1), COMPLETED_PANEL,
            ),
        )
        for current, drive, landing, worth_stage, counts, panel in stores:
            drive()
            _, status = self.get_json("/api/status")
            self.assertEqual(status["stage"], current)
            # No stage, or one that is not a stage, lands on the store's.
            for query_string, (view, tab) in {"": landing, "?stage=bogus": landing, **worth_stage, **asked}.items():
                with self.subTest(store=current, query=query_string):
                    title, watched = SCREENS[view]
                    self.assertEqual(
                        self.payload(f"/api/review/page{query_string}"),
                        {
                            "view": view,
                            "tab": tab,
                            "title": title,
                            "progress": counts,
                            "enrichment": panel,
                            # The token the page compares with /api/status to see a change.
                            "state_token": status["state_token"],
                            "needs_synthesis": False,
                            "external_updates": watched,
                        },
                    )

    def test_enrich_is_not_gated_by_people_still_to_review(self) -> None:
        page = self.payload("/api/review/page?stage=enrich")
        self.assertEqual((page["view"], page["progress"]["worth_pending"]), ("enrich", 1))
        self.assertEqual(page["enrichment"]["mode"], "approval")

    def test_worth_is_complete_after_synthesis_with_maybe_parents(self) -> None:
        page = self.payload("/api/review/page")

        self.assertEqual((page["view"], page["progress"]["worth_pending"]), ("enrich", 1))
        self.assertIn("worth", SqliteReviewAdapter(self.db).manifest().completed_stages)

    def test_unknown_api_paths_are_a_json_404(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, content_type, body, _ = self.http.request(method, "/api/review/missing", {})
                self.assertEqual((status, content_type), (404, JSON_TYPE))
                self.assertEqual(json.loads(body), {"error": "not found"})
        # A GET route is not a POST route, and the reverse.
        self.assertEqual(self.post_json("/api/review/page"), (404, {"error": "not found"}))
        self.assertEqual(self.get_json("/api/review/decide"), (404, {"error": "not found"}))
        # The Jinja page's routes are gone, not redirected.
        for method, path in (
            ("GET", "/api/worth-card"),
            ("GET", "/api/linkedin-card"),
            ("GET", "/api/worth-table?view=yes"),
            ("GET", "/api/worth-details?slug=jordan-bravo"),
            ("GET", "/api/avatar?pub=jordan-bravo"),
            ("GET", "/assets/reconcile-review.js"),
            ("GET", "/assets/reconcile-review.css"),
            ("POST", "/decide"),
            ("POST", "/approve-enrichment"),
        ):
            with self.subTest(gone=path):
                status, content_type, body, _ = self.http.request(method, path, {})
                self.assertEqual((status, content_type, body), (404, "text/plain", b"not found"))


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
        # Enrich and Done say it on the page; the two queues say it on their card reads.
        screens = {
            "": ("worth", "review", False),
            "worth": ("worth", "review", False),
            "enrich": ("enrich", "", True),
            "linkedin": ("linkedin", "", False),
            "done": ("done", "", True),
        }
        for stage, (view, tab, needs_synthesis) in screens.items():
            with self.subTest(stage=stage):
                page = self.payload(f"/api/review/page?stage={stage}")
                page.pop("state_token")
                title, watched = SCREENS[view]
                self.assertEqual(
                    page,
                    {
                        "view": view,
                        "tab": tab,
                        "title": title,
                        "progress": progress(0, 0, 0, 0, unsynthesized=1),
                        "enrichment": COMPLETED_PANEL,
                        "needs_synthesis": needs_synthesis,
                        "external_updates": watched,
                    },
                )

        self.assertEqual(
            self.payload("/api/review/worth-card"), {"card": None, "synthesize_pending": True, "queue": None}
        )
        self.assertEqual(self.payload("/api/review/worth-pending"), {"pending": []})
        self.assertEqual(
            self.payload("/api/review/linkedin-card"),
            {
                "card": None,
                "finished": {
                    "synthesize_pending": True,
                    "linkedin_done": 0,
                    "linkedin_complete": True,
                    "retargets_in_flight": 0,
                    "auto_continue": False,
                },
                "pending": 0,
                "queue": None,
            },
        )
        status = self.payload("/api/status")
        self.assertEqual((status["stage"], status["next_action"]), ("worth", "synthesize"))


class WorthRoutesTests(ReviewApiFixture):
    def test_worth_card_picks_by_index_exclude_and_pick(self) -> None:
        self.store.seed_worth_queue()
        avery, casey, morgan = (f"parent-worth:{name}-parent" for name in ("avery", "worth", "morgan"))
        # The queue is in name order: Avery Quinn, Casey Delta, Morgan Echo.
        self.assertEqual([row.key for row in worth_queue(self.db)], [avery, casey, morgan])
        picks = {
            "": ("avery-quinn", None),
            "?index=1": ("casey-delta", None),
            "?index=2": ("morgan-echo", None),
            # Past the end wraps; below zero and not-a-number are the first card.
            "?index=7": ("casey-delta", None),
            "?index=-3": ("avery-quinn", None),
            "?index=x": ("avery-quinn", None),
            "?debug=1": ("avery-quinn", {"index": 0, "total": 3}),
            "?debug=1&index=2": ("morgan-echo", {"index": 2, "total": 3}),
            f"?exclude={avery}": ("casey-delta", None),
            f"?exclude={avery},{casey.upper()}": ("morgan-echo", None),
            f"?exclude={avery}&index=1&debug=1": ("morgan-echo", {"index": 1, "total": 2}),
            f"?pick={morgan}": ("morgan-echo", None),
            f"?pick={casey.upper()}&debug=1": ("casey-delta", {"index": 0, "total": 1}),
            # A pick that is also excluded, and a queue with everyone excluded: no card.
            f"?pick={casey}&exclude={casey}": (None, None),
            f"?exclude={avery},{casey},{morgan}": (None, None),
        }
        for query_string, (slug, position) in picks.items():
            with self.subTest(query=query_string):
                payload = self.payload(f"/api/review/worth-card{query_string}")
                card = payload["card"]
                self.assertEqual(card["person"]["slug"] if card else None, slug)
                self.assertEqual((payload["queue"], payload["synthesize_pending"]), (position, False))

    def test_worth_card_orders_names_without_regard_to_case_or_accent_folding(self) -> None:
        # SQLite's lower() folds ASCII only; the queue is ordered by Python's.
        self.store.seed("emile-parent", "emile-foxtrot", "Émile Foxtrot", "maybe")
        self.store.seed("elodie-parent", "elodie-golf", "élodie Golf", "maybe")
        names = [
            self.payload(f"/api/review/worth-card?index={index}")["card"]["person"]["name"] for index in range(3)
        ]
        self.assertEqual(names, ["Casey Delta", "élodie Golf", "Émile Foxtrot"])

    def test_worth_card_is_gone_once_its_pick_is_decided(self) -> None:
        key = worth_queue(self.db)[0].key
        self.db.decide_worth("worth-parent", "yes")
        for pick in ("missing", key):
            with self.subTest(pick=pick):
                self.assertEqual(self.get_json(f"/api/review/worth-card?pick={pick}"), (404, {"error": "gone"}))

    def test_worth_card_is_gone_when_its_parent_left_the_store(self) -> None:
        with mock.patch.object(review_api, "person_detail", return_value=None):
            self.assertEqual(self.get_json("/api/review/worth-card"), (404, {"error": "gone"}))

    def test_worth_card_carries_the_person_and_the_first_candidate(self) -> None:
        replace_person_sources(
            self.db,
            "worth-parent-person",
            tuple(
                PersonSourceRow("worth-parent-person", source)
                for source in ("whatsapp", "gmail_msgvault", "linkedin_csv")
            ),
        )
        self.assertEqual(
            self.payload("/api/review/worth-card"),
            {
                # Message sources only, in the badges' order; every label over the threshold.
                "card": {"person": {**CASEY, "sources": ["gmail", "whatsapp"]}, "candidate": CASEY_CANDIDATE},
                "synthesize_pending": False,
                "queue": None,
            },
        )

    def test_last_worth_card_leaves_no_card_and_no_synthesis_handoff(self) -> None:
        key = worth_queue(self.db)[0].key
        self.assertEqual(
            self.payload(f"/api/review/worth-card?exclude={key}"),
            {"card": None, "synthesize_pending": False, "queue": None},
        )

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
        self.assertEqual(card, {"person": person("bare-parent", "aaron-bare", "Aaron Bare"), "candidate": None})

    def test_worth_pending_is_the_queue_in_name_order(self) -> None:
        self.store.seed_worth_queue()
        self.assertEqual(
            self.payload("/api/review/worth-pending"),
            {
                "pending": [
                    {"key": "parent-worth:avery-parent", "name": "Avery Quinn"},
                    {"key": "parent-worth:worth-parent", "name": "Casey Delta"},
                    {"key": "parent-worth:morgan-parent", "name": "Morgan Echo"},
                ]
            },
        )

    def test_worth_table_is_one_pile_with_names_labels_and_reasons(self) -> None:
        self.store.seed_worth_queue()
        self.store.seed_linkedin_queue()
        replace_person_sources(self.db, "worth-parent-person", (PersonSourceRow("worth-parent-person", "imessage"),))
        self.db.decide_worth("worth-parent", "yes", note="Synthetic note")
        self.db.decide_worth("morgan-parent", "no")
        self.db.decide_worth("avery-parent", "no", note="Synthetic refusal")
        # A pile page carries no profile, sources or contacts: an opened row reads worth-details.
        yes = {
            "rows": [
                {"person": {**CASEY, "contacts": ""}, "reason": "Synthetic note"},
                {"person": JORDAN, "reason": "fixture"},
                {"person": person("riley-parent", "riley-stone", "Riley Stone"), "reason": "fixture"},
                {"person": person("sam-parent", "sam-tango", "Sam Tango"), "reason": "fixture"},
            ],
            "total": 4,
        }
        no = {
            "rows": [
                {"person": person("avery-parent", "avery-quinn", "Avery Quinn"), "reason": "Synthetic refusal"},
                {"person": person("morgan-parent", "morgan-echo", "Morgan Echo"), "reason": "You said no"},
            ],
            "total": 2,
        }
        for view, table in (("yes", yes), ("YES", yes), ("no", no)):
            with self.subTest(view=view):
                self.assertEqual(self.payload(f"/api/review/worth-table?view={view}"), table)

    def test_worth_table_pages_by_offset(self) -> None:
        self.store.seed_linkedin_queue()
        pages = {
            "0": ["Jordan Bravo", "Riley Stone", "Sam Tango"],
            "1": ["Riley Stone", "Sam Tango"],
            "2": ["Sam Tango"],
            "9": [],
            "-4": ["Jordan Bravo", "Riley Stone", "Sam Tango"],
        }
        for offset, names in pages.items():
            with self.subTest(offset=offset):
                table = self.payload(f"/api/review/worth-table?view=yes&offset={offset}")
                self.assertEqual([row["person"]["name"] for row in table["rows"]], names)
                self.assertEqual(table["total"], 3)

    def test_worth_table_reads_one_light_page(self) -> None:
        self.store.seed_linkedin_queue()
        with mock.patch.object(view_rows, "_hydrate_parents", wraps=view_rows._hydrate_parents) as hydrate:
            self.payload("/api/review/worth-table?view=yes")
        hydrate.assert_not_called()

    def test_worth_details_is_the_profile_an_opened_row_shows(self) -> None:
        replace_person_sources(
            self.db,
            "worth-parent-person",
            tuple(PersonSourceRow("worth-parent-person", source) for source in ("imessage", "gmail_msgvault")),
        )
        self.db.decide_worth("worth-parent", "yes")
        casey = {"person": {**CASEY, "sources": ["gmail", "imessage"]}, "candidate": CASEY_CANDIDATE}
        # By slug or by parent id.
        self.assertEqual(self.payload("/api/review/worth-details?slug=casey-delta"), casey)
        self.assertEqual(self.payload("/api/review/worth-details?slug=worth-parent"), casey)
        self.assertEqual(
            self.payload("/api/review/worth-details?slug=jordan-bravo"),
            {"person": JORDAN, "candidate": JORDAN_CANDIDATE},
        )
        # Of several candidates, the profile shown is the first.
        self.store.add_second_candidate()
        self.assertEqual(
            self.payload("/api/review/worth-details?slug=jordan-bravo")["candidate"], JORDAN_CANDIDATE
        )

    def test_worth_details_is_gone_with_its_parent(self) -> None:
        for query_string in ("", "?slug=nobody"):
            with self.subTest(query=query_string):
                self.assertEqual(self.get_json(f"/api/review/worth-details{query_string}"), (404, {"error": "gone"}))

    def test_worth_table_refuses_a_bad_view_or_offset(self) -> None:
        refusals = {
            "": "view must be yes or no",
            "?view=maybe": "view must be yes or no",
            "?view=review": "view must be yes or no",
            "?view=yes&offset=x": "offset must be an integer",
        }
        for query_string, error in refusals.items():
            with self.subTest(query=query_string):
                self.assertEqual(self.get_json(f"/api/review/worth-table{query_string}"), (400, {"error": error}))

    def test_decision_reason_is_the_humans_note_or_call_else_the_machines_reason(self) -> None:
        parent = person_detail(self.db, "worth-parent")
        long_reason = "Synthetic reason. " * 12
        human = WorthHumanRow("yes", "2026-09-30T00:00:00Z", "")
        worth = parent.worth_row
        cases = (
            (replace(worth, machine=WorthMachineRow("yes", "Synthetic fit", "llm")), "Synthetic fit", "Synthetic fit"),
            # A long machine reason is cut to 140 characters and an ellipsis.
            (
                replace(worth, machine=WorthMachineRow("yes", long_reason, "llm")),
                long_reason[:140] + "…",
                long_reason[:140] + "…",
            ),
            (replace(worth, machine=WorthMachineRow("yes", "", "llm")), "Worth adding", "Not worth adding"),
            (replace(worth, machine=WorthMachineRow("yes", None, "llm")), "Worth adding", "Not worth adding"),
            # A human's call outranks the machine's reason: the note, else the call itself.
            (
                replace(worth, human=replace(human, note="Met at a synthetic dinner")),
                "Met at a synthetic dinner",
                "Met at a synthetic dinner",
            ),
            (replace(worth, human=human, effective="yes"), "You said yes", "You said yes"),
            (replace(worth, human=replace(human, decision="no"), effective="no"), "You said no", "You said no"),
        )
        self.assertEqual(len(long_reason[:140] + "…"), 141)
        for worth_row, in_yes_pile, in_no_pile in cases:
            with self.subTest(expected=in_yes_pile[:30]):
                row_parent = replace(parent, worth_row=worth_row)
                self.assertEqual(DecisionRow.from_parent(row_parent, "yes").reason, in_yes_pile)
                self.assertEqual(DecisionRow.from_parent(row_parent, "no").reason, in_no_pile)


class LinkedinRoutesTests(ReviewApiFixture):
    def retarget(self, slug: str, state: str, detail: str = "") -> GuidanceViewRow:
        return GuidanceViewRow(
            slug=slug, row_key=slug, name="", guidance="Synthetic guidance", state=state, detail=detail,
            submitted_at="", updated_at="", new_url="", wire_fields=(),
        )

    def test_linkedin_card_picks_by_index_and_exclude(self) -> None:
        self.store.seed_linkedin_queue()
        # The queue is in name order: Jordan Bravo, Riley Stone, Sam Tango.
        picks = {
            "": ("jordan-bravo", None),
            "?index=1": ("riley-stone", None),
            "?index=2": ("sam-tango", None),
            "?index=4": ("riley-stone", None),
            "?index=x": ("jordan-bravo", None),
            "?debug=1&index=1": ("riley-stone", {"index": 1, "total": 3}),
            "?exclude=jordan-bravo": ("riley-stone", None),
            "?exclude=JORDAN-BRAVO,%20sam-tango": ("riley-stone", None),
            "?exclude=riley-stone&index=1&debug=1": ("sam-tango", {"index": 1, "total": 2}),
        }
        for query_string, (slug, position) in picks.items():
            with self.subTest(query=query_string):
                payload = self.payload(f"/api/review/linkedin-card{query_string}")
                card = payload["card"]
                self.assertEqual(card["person"]["slug"], slug)
                # One candidate each: the parent's own LinkedIn.
                self.assertEqual([profile["row_key"] for profile in card["candidates"]], [slug])
                # `pending` counts the whole queue, whatever the card leaves out.
                self.assertEqual((payload["queue"], payload["pending"], payload["finished"]), (position, 3, None))

        self.assertEqual(
            self.payload("/api/review/linkedin-card?exclude=jordan-bravo,riley-stone,sam-tango"),
            {"card": None, "finished": NOT_FINISHED, "pending": 3, "queue": None},
        )

    def test_linkedin_card_carries_the_person_and_every_pending_candidate(self) -> None:
        card = {"person": JORDAN, "candidates": [JORDAN_CANDIDATE], "failure_note": ""}
        self.assertEqual(
            self.payload("/api/review/linkedin-card"), {"card": card, "finished": None, "pending": 1, "queue": None}
        )

        self.store.add_second_candidate()
        second = candidate("jordan-bravo-alt", "Jordan B. Bravo", url="https://www.linkedin.com/in/jordan-bravo-alt")
        self.assertEqual(
            self.payload("/api/review/linkedin-card")["card"], {**card, "candidates": [JORDAN_CANDIDATE, second]}
        )

    def test_linkedin_card_hides_in_flight_research_and_reports_a_failed_one(self) -> None:
        self.store.seed_linkedin_queue()
        retargets = [
            self.retarget("jordan-bravo", "researching"),
            self.retarget("sam-tango", "failed", "Synthetic provider outage"),
            self.retarget("sam-tango", "failed", "An older synthetic failure"),
            self.retarget("riley-stone", "failed"),
        ]
        with mock.patch.object(SqliteReviewAdapter, "retargets", return_value=retargets):
            notes = {}
            for index in (0, 1):
                payload = self.payload(f"/api/review/linkedin-card?index={index}")
                self.assertEqual(payload["pending"], 3)
                notes[payload["card"]["person"]["slug"]] = payload["card"]["failure_note"]
            finished = self.payload("/api/review/linkedin-card?exclude=riley-stone,sam-tango")
        # Jordan is being re-researched: no card. The latest failure is the one reported.
        self.assertEqual(
            notes, {"riley-stone": "the job did not finish", "sam-tango": "Synthetic provider outage"}
        )
        self.assertEqual(
            finished,
            {"card": None, "finished": {**NOT_FINISHED, "retargets_in_flight": 1}, "pending": 3, "queue": None},
        )

    def test_research_landing_mid_request_never_serves_a_blank_card(self) -> None:
        self.store.seed_linkedin_queue()
        # Jordan is first in the queue and is being re-researched; the worker's result
        # lands right after this request read the queue's order.
        landed = False
        read_order = linkedin_queue.linkedin_queue_order

        def order_then_land(db: Db) -> list:
            nonlocal landed
            order = read_order(db)
            db.decide_identity("jordan-bravo", "verify")
            landed = True
            return order

        def retargets(_adapter: SqliteReviewAdapter) -> list[GuidanceViewRow]:
            return [] if landed else [self.retarget("jordan-bravo", "researching")]

        with (
            mock.patch.object(linkedin_queue, "linkedin_queue_order", order_then_land),
            mock.patch.object(SqliteReviewAdapter, "retargets", retargets),
        ):
            payload = self.payload("/api/review/linkedin-card")
        self.assertEqual(payload["card"]["person"]["slug"], "riley-stone")
        self.assertEqual([profile["row_key"] for profile in payload["card"]["candidates"]], ["riley-stone"])

    def test_finished_state_follows_the_queue(self) -> None:
        self.store.reach_done()
        self.assertEqual(
            self.payload("/api/review/linkedin-card"),
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

    def test_contact_is_the_persons_across_every_merged_record(self) -> None:
        # The LinkedIn came from the record with the email; the phone arrived on another.
        replace_person_identifiers(
            self.db,
            "linkedin-parent-person",
            (PersonIdentifierRow("linkedin-parent-person", "email", "jordan@example.com"),),
        )
        self.db.project_rows((PersonRow("jordan-phone-person", "linkedin-parent", display_name="Jordan Bravo"),))
        replace_person_identifiers(
            self.db,
            "jordan-phone-person",
            (
                PersonIdentifierRow("jordan-phone-person", "phone", "+14155550101"),
                PersonIdentifierRow("jordan-phone-person", "phone", "4155550101"),
            ),
        )
        card = self.payload("/api/review/linkedin-card")["card"]
        self.assertEqual(card["person"]["contacts"], "jordan@example.com · +14155550101")
        self.assertNotIn("contacts", card["candidates"][0])
        opened = self.payload("/api/review/worth-details?slug=jordan-bravo")
        self.assertEqual(opened["person"]["contacts"], "jordan@example.com · +14155550101")

    def test_candidate_is_shaped_for_the_card(self) -> None:
        parent = person_detail(self.db, "linkedin-parent")
        fetched = replace(
            parent.candidates[0],
            headline="Synthetic operator",
            location="Example City",
            experiences=("Founder @ Bravo Robotics", " ", "Engineer @ Example Labs"),
            education=("BS — Example University", ""),
            profile_pic_url="https://example.com/photo.png",
        )
        self.assertEqual(
            dataclasses.asdict(ReviewCandidate.from_row(fetched)),
            {
                "row_key": "jordan-bravo",
                "name": "Jordan Bravo",
                "url": "https://www.linkedin.com/in/jordan-bravo",
                "headline": "Synthetic operator",
                "location": "Example City",
                # Blank entries are dropped.
                "experiences": ("Founder @ Bravo Robotics", "Engineer @ Example Labs"),
                "education": ("BS — Example University",),
                "synthetic": False,
                "avatar_url": "https://example.com/photo.png",
            },
        )
        # A researched profile links nowhere and shows no picture.
        researched = ReviewCandidate.from_row(replace(fetched, synthetic=True, url="https://example.com/researched"))
        self.assertEqual((researched.synthetic, researched.url, researched.avatar_url), (True, "", ""))
        self.assertEqual(ReviewCandidate.from_row(parent.candidates[0]).avatar_url, "")

        labelled = person_detail(self.db, "worth-parent")
        self.assertEqual(list(ReviewPerson.from_parent(labelled).labels), CASEY_TITLES)
        self.assertEqual(ReviewPerson.from_parent(parent).labels, ())


class LinkedinQueueTests(ReviewApiFixture):
    """The server reads who is pending once and keeps it; a click takes one parent out."""

    def keep(self, slug: str) -> dict:
        status, written = self.post_json(
            "/api/review/decide", {"pub": slug, "decision": "keep", "parent_slug": slug}
        )
        self.assertEqual(status, 200)
        return written

    def test_the_queue_is_read_once_and_each_click_takes_one_parent_out(self) -> None:
        self.store.seed_linkedin_queue()
        with (
            mock.patch.object(linkedin_queue, "linkedin_queue_order", wraps=linkedin_queue.linkedin_queue_order) as order,
            mock.patch.object(review_api, "worth_counts", wraps=review_api.worth_counts) as worth,
            mock.patch.object(review_api, "person_detail", wraps=review_api.person_detail) as detail,
        ):
            self.assertEqual(self.payload("/api/review/linkedin-card")["pending"], 3)
            first = self.keep("jordan-bravo")
            second = self.keep("riley-stone")
        self.assertEqual(order.call_count, 1)
        # A LinkedIn click cannot change the worth piles, and its card names its parent.
        self.assertEqual((worth.call_count, detail.call_count), (0, 0))
        self.assertEqual((first["next"]["pending"], first["next"]["card"]["person"]["slug"]), (2, "riley-stone"))
        self.assertEqual((second["next"]["pending"], second["next"]["card"]["person"]["slug"]), (1, "sam-tango"))
        self.assertNotIn("progress", first)

    def test_a_reset_puts_the_parent_back_in_its_place(self) -> None:
        self.store.seed_linkedin_queue()
        self.assertEqual(self.keep("riley-stone")["next"]["pending"], 2)
        status, written = self.post_json(
            "/api/review/decide", {"pub": "riley-stone", "decision": "reset", "parent_slug": "riley-stone"}
        )
        self.assertEqual((status, written["next"]["pending"]), (200, 3))
        slugs = [self.payload(f"/api/review/linkedin-card?index={index}")["card"]["person"]["slug"] for index in range(3)]
        self.assertEqual(slugs, ["jordan-bravo", "riley-stone", "sam-tango"])

    def test_a_card_decided_behind_the_servers_back_is_skipped(self) -> None:
        self.store.seed_linkedin_queue()
        self.assertEqual(self.payload("/api/review/linkedin-card")["card"]["person"]["slug"], "jordan-bravo")
        # Another process settles Jordan: the queue in memory still names him.
        self.db.decide_identity("jordan-bravo", "verify")
        following = self.payload("/api/review/linkedin-card")
        self.assertEqual((following["card"]["person"]["slug"], following["pending"]), ("riley-stone", 2))

    def test_a_card_read_before_a_reset_landed_does_not_take_the_parent_out(self) -> None:
        self.store.seed_linkedin_queue()
        self.assertEqual(self.payload("/api/review/linkedin-card")["pending"], 3)
        read = review_api.linkedin_queue_parent
        # The first read saw Jordan decided; a reset has put him back since.
        jordan = read(self.db, linkedin_queue.linkedin_queue_order(self.db)[0].parent_id)
        stale = dataclasses.replace(jordan, candidates=())
        with mock.patch.object(review_api, "linkedin_queue_parent", side_effect=[stale, jordan]):
            card = self.payload("/api/review/linkedin-card")
        self.assertEqual((card["card"]["person"]["slug"], card["pending"]), ("jordan-bravo", 3))

    def test_a_worth_decision_makes_the_next_read_load_the_queue_again(self) -> None:
        self.store.seed_linkedin_queue()
        self.assertEqual(self.payload("/api/review/linkedin-card")["pending"], 3)
        status, written = self.post_json(
            "/worth", {"pub": "parent-worth:riley-parent", "worth": "no", "parent_slug": "riley-stone"}
        )
        self.assertEqual((status, written["progress"]["linkedin_pending"]), (200, 2))
        slugs = [self.payload(f"/api/review/linkedin-card?index={index}")["card"]["person"]["slug"] for index in range(2)]
        self.assertEqual(slugs, ["jordan-bravo", "sam-tango"])

    def test_enrich_finishing_and_a_reresearch_changing_both_load_the_queue(self) -> None:
        with (
            mock.patch.object(review_server, "EnrichmentPipeline") as pipeline,
            mock.patch.object(review_server, "GuidedRetargetWorker") as worker,
            mock.patch.object(linkedin_queue, "linkedin_queue_order", wraps=linkedin_queue.linkedin_queue_order) as order,
        ):
            review_server.make_handler(db=self.db, run_jobs=True)
            self.assertEqual(order.call_count, 0)
            pipeline.call_args.kwargs["on_finish"]()
            self.assertEqual(order.call_count, 1)
            worker.call_args.kwargs["on_change"]()
            self.assertEqual(order.call_count, 2)

    def test_a_slow_read_never_replaces_a_newer_one(self) -> None:
        reading, release = threading.Event(), threading.Event()

        def order(_db: object) -> list[str]:
            if reading.is_set():
                return ["new"]
            reading.set()
            release.wait(5)
            return ["old"]

        queue = linkedin_queue.LinkedinQueue(self.db)
        with mock.patch.object(linkedin_queue, "linkedin_queue_order", order):
            slow = threading.Thread(target=queue.load)
            slow.start()
            reading.wait(5)
            newer = threading.Thread(target=queue.load)
            newer.start()
            # The newer read waits its turn; one that did not would finish here.
            newer.join(0.2)
            release.set()
            slow.join(5)
            newer.join(5)
        self.assertEqual(queue.rows(), ("new",))

    def test_two_settles_of_one_parent_leave_the_queue_as_the_store_has_it(self) -> None:
        row = LinkedInQueueRow("jordan-parent", "jordan-bravo")
        checking, release = threading.Event(), threading.Event()

        def pending(_db: object, _parent_id: str) -> bool:
            # The second check is made after a reset put the parent back.
            if checking.is_set():
                return True
            checking.set()
            release.wait(5)
            return False

        queue = linkedin_queue.LinkedinQueue(self.db)
        with (
            mock.patch.object(linkedin_queue, "linkedin_queue_order", lambda _db: [row]),
            mock.patch.object(linkedin_queue, "linkedin_parent_pending", pending),
        ):
            queue.load()
            kept = threading.Thread(target=queue.settle, args=(row.parent_id,))
            kept.start()
            checking.wait(5)
            reset = threading.Thread(target=queue.settle, args=(row.parent_id,))
            reset.start()
            # The second settle waits its turn; one that did not would finish here.
            reset.join(0.2)
            release.set()
            kept.join(5)
            reset.join(5)
        self.assertEqual(queue.rows(), (row,))


class DecideTests(ReviewApiFixture):
    def test_decide_writes_the_decision_on_its_row_and_nothing_else(self) -> None:
        url = "https://www.linkedin.com/in/jordan-bravo-correct"
        cases = (
            ({"decision": "keep"}, "verify", "", ("verify", "yes", "deep-context-review", None, None, None)),
            (
                {"decision": "detach", "note": "  Synthetic note  "},
                "detach",
                "",
                ("detach", "yes", "deep-context-review", "Synthetic note", None, None),
            ),
            (
                {"decision": "fix", "new_url": url},
                "retarget",
                url,
                ("retarget", "yes", "deep-context-review", None, url, "jordan-bravo-correct"),
            ),
            ({"decision": "exclude"}, "exclude", "", ("exclude", "yes", "deep-context-review", None, None, None)),
        )
        for index, (fields, action, new_url, written) in enumerate(cases):
            with self.subTest(decision=fields["decision"]):
                store = ReviewStore(self.root / f"decide-{index}")
                store.seed_linkedin_queue()
                before = store.link_rows()
                form = {"pub": "jordan-bravo", "parent_slug": "jordan-bravo", **fields}
                status, answer = self.post_json("/api/review/decide", form, http=store.http)
                self.assertEqual(status, 200)
                following = answer.pop("next")
                self.assertEqual(
                    answer,
                    {
                        "ok": True,
                        "pub": "jordan-bravo",
                        "action": action,
                        "approved": "yes",
                        "new_url": new_url,
                        "resolved_pubs": ["jordan-bravo"],
                    },
                )
                self.assertEqual((following["card"]["person"]["slug"], following["pending"]), ("riley-stone", 2))
                after = store.link_rows()
                self.assertEqual(tuple(after["jordan-bravo"][column] for column in DECISION_COLUMNS), written)
                # Only the decision columns of the decided row moved.
                self.assertEqual(
                    {**after, "jordan-bravo": {**after["jordan-bravo"], **dict.fromkeys(DECISION_COLUMNS)}}, before
                )

                # A reset takes the decision back off.
                reset = {"pub": "jordan-bravo", "parent_slug": "jordan-bravo", "decision": "reset"}
                status, answer = self.post_json("/api/review/decide", reset, http=store.http)
                self.assertEqual(status, 200)
                self.assertEqual((answer["action"], answer["approved"], answer["new_url"]), ("", "", ""))
                self.assertEqual(answer["next"]["pending"], 3)
                self.assertEqual(store.link_rows(), before)

    def test_decide_answers_with_the_next_card_and_its_count(self) -> None:
        self.store.seed_linkedin_queue()
        status, written = self.post_json(
            "/api/review/decide", {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(written["next"], self.payload("/api/review/linkedin-card?exclude=jordan-bravo"))
        self.assertEqual(written["next"]["pending"], 2)
        self.assertEqual(written["next"]["card"]["person"]["slug"], "riley-stone")
        self.assertEqual((written["ok"], written["pub"], written["approved"]), (True, "jordan-bravo", "yes"))

    def test_decide_never_serves_the_decided_parent_back(self) -> None:
        # A reset leaves the parent pending; with or without its slug, the next card is not it.
        for fields in ({"parent_slug": "jordan-bravo"}, {}):
            with self.subTest(fields=fields):
                status, written = self.post_json(
                    "/api/review/decide", {"pub": "jordan-bravo", "decision": "reset", **fields}
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    written["next"], {"card": None, "finished": NOT_FINISHED, "pending": 1, "queue": None}
                )

    def test_last_decision_answers_with_the_finished_state(self) -> None:
        self.store.reach_linkedin()
        status, written = self.post_json(
            "/api/review/decide", {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(written["next"]["pending"], 0)
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
        self.assertEqual(self.store.link_rows()["identity-row-42"]["decision_action"], "detach")

    def test_decide_refuses_a_bad_form_and_writes_nothing(self) -> None:
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
        before = self.store.link_rows()
        for fields, expected_status, text in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.post_json("/api/review/decide", fields), (expected_status, {"error": text}))
        self.assertEqual(self.store.link_rows(), before)

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
        self.assertEqual(
            self.post_json("/api/review/decide", {"pub": "owner-row", "decision": "keep"}),
            (404, {"error": "review row not found: owner-row"}),
        )

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
        self.assertEqual(
            self.post_json("/api/review/decide", {"pub": "ambiguous-public-identifier", "decision": "keep"}),
            (400, {"error": "ambiguous identity candidate: ambiguous-public-identifier"}),
        )

    def test_posts_refuse_another_origin_and_accept_this_machine(self) -> None:
        before = self.store.link_rows()
        fields = {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"}
        paths = (
            "/api/review/decide",
            "/api/review/approve-enrichment",
            "/worth",
            "/complete",
            "/retarget",
            "/feedback",
            "/auth/login",
        )
        for path in paths:
            with (
                self.subTest(path=path),
                mock.patch.object(
                    enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("refused first")
                ),
                mock.patch.object(auth_login, "start_auth_login", side_effect=AssertionError("refused first")),
            ):
                status, payload = self.post_json(path, fields, headers={"Origin": "https://example.test"})
                self.assertEqual((status, payload), (403, {"error": "cross-origin request rejected"}))
        self.assertEqual(self.store.link_rows(), before)

        for origin in ("http://127.0.0.1:8765", "http://localhost:5173"):
            with self.subTest(origin=origin):
                status, _ = self.post_json(
                    "/api/review/decide", {**fields, "decision": "reset"}, headers={"Origin": origin}
                )
                self.assertEqual(status, 200)


class EnrichmentPanelTests(ReviewApiFixture):
    def test_panel_is_one_of_five_states(self) -> None:
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
            # Nothing to research, but the judgments cost: still an approval of that estimate.
            (
                replace(base, status="not_started", state="profile_prep_pending", would_submit=0, estimated_usd=0.02),
                EnrichmentPanel("approval", approval_label="Approve $0.02"),
            ),
            # Under half a cent rounds to $0.00, which is the free continue.
            (
                replace(base, status="not_started", state="profile_prep_pending", would_submit=0, estimated_usd=0.004),
                EnrichmentPanel("approval", approval_label="Prepare profiles and judge LinkedIns"),
            ),
            (
                replace(base, status="not_started", state="profile_prep_pending", would_submit=3, estimated_usd=0.004),
                EnrichmentPanel("approval", approval_label="Approve $0.00"),
            ),
            # Cached research that still needs the free local chain is a $0 continue.
            (
                replace(base, status="completed", state="profile_prep_pending", would_submit=0, estimated_usd=0.0),
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
            with self.subTest(status=view.status, state=view.state, estimate=view.estimated_usd):
                self.assertEqual(EnrichmentPanel.from_view(view), expected)

    def test_page_carries_the_stores_enrichment_panel(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        estimate = self.store.estimate()
        self.assertGreater(round(estimate, 2), 0)
        page = self.payload("/api/review/page")
        self.assertEqual(page["view"], "enrich")
        self.assertEqual(
            page["enrichment"],
            {"mode": "approval", "completed": 0, "total": 0, "approval_label": f"Approve ${estimate:.2f}", "error": ""},
        )

    def test_judgments_alone_are_an_approval_of_their_estimate(self) -> None:
        # Nothing to research (Casey is a no), but Jordan's LinkedIn still has to be judged.
        self.store.reach_enrich()
        estimate = self.store.estimate()
        self.assertGreater(round(estimate, 2), 0)
        page = self.payload("/api/review/page")
        panel = page["enrichment"]
        self.assertEqual((panel["mode"], panel["approval_label"]), ("approval", f"Approve ${estimate:.2f}"))


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

    def test_reads_never_launch_the_pipeline(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("GET must not start work")
        ) as start:
            http = self.store.client()
            for path in (
                "/api/review/page?stage=enrich",
                "/api/review/worth-card",
                "/api/review/linkedin-card",
                "/api/status",
                "/api/enrichment",
            ):
                self.assertEqual(self.get_json(path, http=http)[0], 200)
        start.assert_not_called()

    def test_running_enrichment_approval_is_idempotent(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        estimate = self.store.estimate()
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
            relationships.return_value.run.side_effect = self.store.finish_questions
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
            # The pipeline hands research the approved estimate, to the cent.
            self.assertEqual(reconcile.call_args.kwargs["budget"], round(estimate, 2))
            self.assertIs(reconcile.call_args.kwargs["approve"], True)

    def test_approval_starts_the_pipeline_with_the_plan_it_showed(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        plan = SqliteReviewAdapter(self.db, 0.7).enrichment()
        with mock.patch.object(enrichment_pipeline.EnrichmentPipeline, "start", return_value=False) as start:
            status, answer = self.post_json("/api/review/approve-enrichment", http=self.store.client())
        # One person to research, the estimate the button showed, that plan's fingerprint.
        start.assert_called_once_with(1, plan.estimated_usd, plan.request_fingerprint)
        # Not launched: the panel is the store's, still waiting for the approval.
        self.assertEqual(
            (status, answer),
            (
                200,
                {
                    "ok": True,
                    "enrichment": {
                        "mode": "approval",
                        "completed": 0,
                        "total": 0,
                        "approval_label": f"Approve ${plan.estimated_usd:.2f}",
                        "error": "",
                    },
                },
            ),
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
            answer = self.post_json("/api/review/approve-enrichment", http=self.store.client())
        self.assertEqual(answer, (200, {"ok": True, "enrichment": COMPLETED_PANEL}))

    def test_disabled_job_execution_refuses_the_approval(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline, "start", side_effect=AssertionError("jobs are disabled")
        ):
            answer = self.post_json("/api/review/approve-enrichment", http=self.store.client(run_jobs=False))
        self.assertEqual(answer, (409, {"error": "enrichment job execution is disabled"}))

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
            answer = self.post_json("/api/review/approve-enrichment", http=self.store.client(run_jobs=False))
        self.assertEqual(answer, (409, {"error": "enrichment job execution is disabled"}))

    def test_an_approval_the_store_turns_down_is_a_conflict(self) -> None:
        refusal = StoreError("Enrichment is not waiting for approval")
        with mock.patch.object(SqliteReviewAdapter, "approve_enrichment", side_effect=refusal):
            answer = self.post_json("/api/review/approve-enrichment")
        self.assertEqual(answer, (409, {"error": "Enrichment is not waiting for approval"}))


class TypeScriptPinTests(ReviewApiFixture):
    """web/src/types/review.ts is what the page reads; a rename on either side fails here."""

    def test_payload_dataclasses_match_the_interfaces(self) -> None:
        shapes = {
            "DecisionProgress": DecisionProgress,
            "PageProgress": PageProgress,
            "EnrichmentPanel": EnrichmentPanel,
            "ReviewPage": ReviewPage,
            "ReviewPerson": ReviewPerson,
            "ReviewCandidate": ReviewCandidate,
            "QueuePosition": QueuePosition,
            "WorthDetails": WorthDetails,
            "WorthCardPayload": WorthCardPayload,
            "WorthPendingEntry": WorthPendingEntry,
            "DecisionRow": DecisionRow,
            "WorthTablePayload": WorthTablePayload,
            "LinkedinFinished": LinkedinFinished,
            "LinkedinCardPayload": LinkedinCardPayload,
            "WorthResult": WorthResult,
            "DecideResult": DecideResult,
            "ApproveResult": ApproveResult,
        }
        for interface, shape in shapes.items():
            with self.subTest(interface=interface):
                self.assertEqual(ts_fields(interface), field_names(shape))
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
        self.assertEqual(ts_union("ReviewView"), set(TITLES))
        self.assertEqual(ts_union("ReviewView"), set(STAGE_BY_ACTION.values()))
        self.assertEqual(ts_union("ReviewView"), set(SCREENS))

    def test_live_payloads_carry_exactly_the_interface_fields(self) -> None:
        self.store.seed_linkedin_queue()

        def fields(interface: str) -> set[str]:
            return set(ts_fields(interface))

        page = self.payload("/api/review/page")
        self.assertEqual(set(page), fields("ReviewPage"))
        self.assertEqual(set(page["progress"]), fields("PageProgress"))
        self.assertEqual(set(page["enrichment"]), fields("EnrichmentPanel"))

        worth = self.payload("/api/review/worth-card?debug=1")
        self.assertEqual(set(worth), fields("WorthCardPayload"))
        self.assertEqual(set(worth["card"]), fields("WorthDetails"))
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

        details = self.payload("/api/review/worth-details?slug=jordan-bravo")
        self.assertEqual(set(details), fields("WorthDetails"))
        self.assertEqual(set(details["person"]), fields("ReviewPerson"))
        self.assertEqual(set(details["candidate"]), fields("ReviewCandidate"))

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
        self.assertEqual(set(decided["next"]), fields("LinkedinCardPayload"))

        with mock.patch.object(enrichment_pipeline.EnrichmentPipeline, "start", return_value=False):
            _, approved = self.post_json("/api/review/approve-enrichment", http=self.store.client())
        self.assertEqual(set(approved), fields("ApproveResult"))
        self.assertEqual(set(approved["enrichment"]), fields("EnrichmentPanel"))

        _, worth = self.post_json(
            "/worth", {"pub": "parent-worth:worth-parent", "worth": "yes", "parent_slug": "casey-delta"}
        )
        self.assertEqual(set(worth), fields("WorthResult"))
        self.assertEqual(set(worth["progress"]), fields("DecisionProgress"))

    def test_status_carries_the_fields_the_page_reads(self) -> None:
        _, status = self.get_json("/api/status")
        self.assertLessEqual(set(ts_fields("ReviewStatus")), set(status))
        self.assertIn(status["stage"], ts_union("ReviewView"))
        self.assertEqual(set(status["pending"]), set(ts_fields("EnrichPending")))

    def test_status_says_what_enrichment_still_has_to_do(self) -> None:
        # The fixture store: one attached LinkedIn the judge has not checked, on one unsettled person.
        _, status = self.get_json("/api/status")
        self.assertEqual(status["stage"], "enrich")
        self.assertEqual(status["pending"], {"lookups": 0, "linkedin_checks": 1, "unsure": 1, "profiles": 0})
        self.assertEqual(
            sum(status["pending"].values()),
            SqliteReviewAdapter(self.db).snapshot().progress.enrichment_pending,
        )


if __name__ == "__main__":
    unittest.main()
