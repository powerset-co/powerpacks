"""The Review page's JSON routes: the data the Jinja review page renders into HTML.

Flow: `server.make_handler` builds one `ReviewApi` and asks it before the legacy
routes. Every route lives under `/api/review/`; an unknown path there is a JSON 404.

GET  /api/review/page?stage=&view=                        the screen to draw, the stepper,
                                                          the counts, the Enrich panel
GET  /api/review/worth-card?pick=&exclude=&index=&debug=  the next undecided person;
                                                          404 when `pick` is no longer pending
GET  /api/review/worth-pending                            the typeahead's names
GET  /api/review/worth-table?view=yes|no&offset=N         one page of a decided pile
GET  /api/review/linkedin-card?exclude=&index=&debug=     the next LinkedIn card, or the
                                                          finished state
POST /api/review/decide              form pub, decision, new_url, parent_slug, note: one
                                     LinkedIn decision; answers with the next card
POST /api/review/approve-enrichment  approve the estimate and start the pipeline

The queue rules (exclude, in-flight re-research, index, pick, sort) stay here; the page
only draws what it is given. An error is `{"error": text}` with the legacy status code.
The payload dataclasses are field for field with web/src/types/review.ts
(tests/test_deep_context_review_api.py pins the two).

Changelog:
  2026-09-30: created beside the Jinja page, which still serves `/`. `_index`,
    `_excluded`, `_failed_notes`, `IN_FLIGHT_RETARGET_STATES`, `_submitted_row`
    (`parent_hit`), `_decision_progress` (`review_progress`) and the two queue selections
    are copies of server.py's. Delete those originals when the Jinja page goes.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Callable, Literal, get_args

from packs.ingestion.primitives.deep_context.db.identity_views import (
    decision_parents,
    linkedin_queue_order,
    linkedin_queue_parent,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.view_models import CandidateViewRow, ParentViewRow
from packs.ingestion.primitives.deep_context.db.workflow_views import StageProgress
from packs.ingestion.primitives.deep_context.db.worth_views import worth_counts, worth_queue
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.review.enrichment import STAGE_BY_ACTION
from packs.ingestion.primitives.deep_context.review.models import EnrichmentView, GuidanceViewRow
from packs.ingestion.primitives.deep_context.review.rendering import (
    WorthPendingEntry,
    _candidate_contacts,
    _nonempty,
    _phase_view,
    _primary_candidate,
    _value,
    label_titles,
    worth_pending_entries,
)
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import SqliteReviewAdapter
from packs.ingestion.primitives.share.web.server import LOCAL_HOSTS

API_PREFIX = "/api/review/"
MAX_FORM_BYTES = 32_768
MAX_NOTE_CHARS = 2000
MAX_REASON_CHARS = 140
# Non-terminal wire-level progress codes (GuidanceViewRow.state).
IN_FLIGHT_RETARGET_STATES = frozenset({"queued", "researching", "judging", "hydrating"})

ReviewView = Literal["worth", "enrich", "linkedin", "done"]
WorthTab = Literal["review", "yes", "no"]
WorthPile = Literal["yes", "no"]
EnrichmentMode = Literal["running", "approval", "completed", "failed", "preparing"]
LinkedinDecision = Literal["keep", "detach", "fix", "exclude", "reset"]

TITLES: dict[ReviewView, str] = {
    "worth": "Add People",
    "enrich": "Enrich Contacts",
    "linkedin": "Check LinkedIn",
    "done": "All Set",
}
# The screens that watch /api/events and re-read /api/status.
EXTERNAL_UPDATE_VIEWS: frozenset[ReviewView] = frozenset({"enrich", "done"})

Params = dict[str, list[str]]


@dataclass(frozen=True)
class DecisionProgress:
    """The counts a decision click repaints: the worth tabs and the step badges."""

    worth_pending: int
    worth_yes: int
    worth_no: int
    linkedin_pending: int


@dataclass(frozen=True)
class PageProgress(DecisionProgress):
    linkedin_done: int
    rejected: int
    synthesize_pending: int

    @classmethod
    def from_stage(cls, progress: StageProgress) -> PageProgress:
        return cls(
            worth_pending=progress.worth_pending,
            worth_yes=progress.worth_yes,
            worth_no=progress.worth_no,
            linkedin_pending=progress.linkedin_pending,
            linkedin_done=progress.linkedin_done,
            rejected=progress.rejected,
            synthesize_pending=progress.synthesize_pending,
        )


@dataclass(frozen=True)
class ReviewStep:
    number: Literal[1, 2, 3]
    label: str
    stage: Literal["worth", "enrich", "linkedin"]
    complete: bool
    count: int


@dataclass(frozen=True)
class EnrichmentPanel:
    """The Enrich screen's one panel; the branches are rendering.py `render_enrichment`'s."""

    mode: EnrichmentMode
    completed: int = 0
    total: int = 0
    approval_label: str = ""
    error: str = ""

    @classmethod
    def from_view(cls, enrichment: EnrichmentView) -> EnrichmentPanel:
        if enrichment.status == ReceiptStatus.RUNNING:
            total = max(0, enrichment.counts.total)
            return cls("running", completed=min(total, max(0, enrichment.counts.completed)), total=total)

        if enrichment.status == ReceiptStatus.NEEDS_APPROVAL or enrichment.state == "profile_prep_pending":
            # Cached research can still need paid profile and identity work.
            label = (
                f"Approve ${enrichment.estimated_usd:.2f}"
                if enrichment.would_submit or round(enrichment.estimated_usd, 2) > 0
                else "Prepare profiles and judge LinkedIns"
            )
            return cls("approval", approval_label=label)

        if enrichment.status == "completed":
            return cls("completed")

        if enrichment.status == ReceiptStatus.FAILED:
            return cls("failed", error=enrichment.error or "")

        return cls("preparing")


@dataclass(frozen=True)
class ReviewPage:
    view: ReviewView
    tab: WorthTab | Literal[""]
    title: str
    steps: tuple[ReviewStep, ReviewStep, ReviewStep]
    progress: PageProgress
    enrichment: EnrichmentPanel
    state_token: str
    needs_synthesis: bool
    external_updates: bool


@dataclass(frozen=True)
class ReviewPerson:
    parent_id: str
    slug: str
    name: str
    sources: tuple[str, ...]
    labels: tuple[str, ...]
    worth_key: str

    @classmethod
    def from_parent(cls, parent: ParentViewRow) -> ReviewPerson:
        return cls(
            parent_id=parent.parent_id,
            slug=parent.slug,
            name=parent.name,
            sources=parent.sources,
            labels=label_titles(parent),
            worth_key=parent.worth_row.key,
        )


@dataclass(frozen=True)
class ReviewCandidate:
    row_key: str
    name: str
    url: str
    headline: str
    location: str
    experiences: tuple[str, ...]
    education: tuple[str, ...]
    synthetic: bool
    contacts: str

    @classmethod
    def from_row(cls, candidate: CandidateViewRow) -> ReviewCandidate:
        return cls(
            row_key=candidate.row_key,
            name=candidate.full_name,
            # A researched profile never links out: the cards show it has no LinkedIn.
            url="" if candidate.synthetic else candidate.url,
            headline=candidate.headline,
            location=candidate.location,
            experiences=_nonempty(candidate.experiences),
            education=_nonempty(candidate.education),
            synthetic=candidate.synthetic,
            contacts=_candidate_contacts(candidate),
        )

    @classmethod
    def primary(cls, parent: ParentViewRow) -> ReviewCandidate | None:
        candidate = _primary_candidate(parent)
        return cls.from_row(candidate) if candidate else None


@dataclass(frozen=True)
class QueuePosition:
    index: int
    total: int


@dataclass(frozen=True)
class WorthCard:
    person: ReviewPerson
    candidate: ReviewCandidate | None


@dataclass(frozen=True)
class WorthCardPayload:
    card: WorthCard | None
    synthesize_pending: bool
    queue: QueuePosition | None


@dataclass(frozen=True)
class WorthPending:
    pending: tuple[WorthPendingEntry, ...]


@dataclass(frozen=True)
class DecisionRow:
    person: ReviewPerson
    candidate: ReviewCandidate | None
    reason: str

    @classmethod
    def from_parent(cls, parent: ParentViewRow, pile: WorthPile) -> DecisionRow:
        return cls(ReviewPerson.from_parent(parent), ReviewCandidate.primary(parent), cls._reason(parent, pile))

    @staticmethod
    def _reason(parent: ParentViewRow, pile: WorthPile) -> str:
        """Why the parent sits in this pile (templates/decision_row.html.j2)."""
        worth = parent.worth_row
        if worth.human:
            return worth.human.note or f"You said {worth.effective}"

        machine = worth.machine.reason or ""
        if not machine:
            return "Worth adding" if pile == "yes" else "Not worth adding"

        return machine[:MAX_REASON_CHARS] + ("…" if len(machine) > MAX_REASON_CHARS else "")


@dataclass(frozen=True)
class WorthTablePayload:
    rows: tuple[DecisionRow, ...]
    total: int


@dataclass(frozen=True)
class LinkedinFinished:
    synthesize_pending: bool
    linkedin_done: int
    linkedin_complete: bool
    retargets_in_flight: int
    auto_continue: bool


@dataclass(frozen=True)
class LinkedinCard:
    person: ReviewPerson
    candidates: tuple[ReviewCandidate, ...]
    failure_note: str


@dataclass(frozen=True)
class LinkedinCardPayload:
    card: LinkedinCard | None
    finished: LinkedinFinished | None
    pending: int
    queue: QueuePosition | None


@dataclass(frozen=True)
class DecideResult:
    ok: bool
    pub: str
    action: str
    approved: str
    new_url: str
    progress: DecisionProgress
    resolved_pubs: tuple[str, ...]
    next: LinkedinCardPayload


@dataclass(frozen=True)
class ApproveResult:
    ok: bool
    enrichment: EnrichmentPanel


Payload = (
    ReviewPage | WorthCardPayload | WorthPending | WorthTablePayload | LinkedinCardPayload | DecideResult | ApproveResult
)


class _Refusal(Exception):
    """A request the route turns down: the status and the words the page shows."""

    def __init__(self, status: HTTPStatus, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


class ReviewApi:
    """The Review page's JSON GET and POST routes, mountable in any stdlib handler."""

    def __init__(
        self,
        *,
        db: Db,
        adapter: SqliteReviewAdapter,
        start_enrichment: Callable[[int, float, str], bool],
        notify: Callable[[], None],
        wake_agent: Callable[[], None],
        run_jobs: bool,
    ) -> None:
        self.db = db
        self.adapter = adapter
        self.start_enrichment = start_enrichment
        self.notify = notify
        self.wake_agent = wake_agent
        self.run_jobs = run_jobs
        self._get_routes: dict[str, Callable[[Params], Payload]] = {
            "page": self._page,
            "worth-card": self._worth_card,
            "worth-pending": self._worth_pending,
            "worth-table": self._worth_table,
            "linkedin-card": self._linkedin_card,
        }
        self._post_routes: dict[str, Callable[[Params], Payload]] = {
            "decide": self._decide,
            "approve-enrichment": self._approve_enrichment,
        }

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if not parsed.path.startswith(API_PREFIX):
            return False

        route = self._get_routes.get(parsed.path[len(API_PREFIX):])
        if route is None:
            _send_error(handler, HTTPStatus.NOT_FOUND, "not found")
            return True

        _answer(handler, route, urllib.parse.parse_qs(parsed.query))
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if not parsed.path.startswith(API_PREFIX):
            return False

        route = self._post_routes.get(parsed.path[len(API_PREFIX):])
        if route is None:
            _send_error(handler, HTTPStatus.NOT_FOUND, "not found")
            return True

        origin = (handler.headers.get("Origin") or "").strip()
        if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            _send_error(handler, HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            return True

        length = min(int(handler.headers.get("Content-Length", "0")), MAX_FORM_BYTES)
        _answer(handler, route, urllib.parse.parse_qs(handler.rfile.read(length).decode()))
        return True

    def _page(self, params: Params) -> ReviewPage:
        state = self.adapter.snapshot()
        progress = state.progress
        enrichment = self.adapter.enrichment(state)
        # No stage in the URL: land on the stage the store is at.
        view = _phase_view(params) or STAGE_BY_ACTION[state.next_action]
        requested_tab = _value(params, "view", "review")
        if (
            view == "worth"
            and requested_tab == "review"
            and not progress.worth_pending
            and not progress.synthesize_pending
        ):
            view = "enrich"

        tab = ""
        if view == "worth":
            tab = requested_tab.lower() if requested_tab.lower() in get_args(WorthTab) else "review"

        # Strict sequence: no step is complete while synthesis is pending.
        synthesized = not progress.synthesize_pending
        steps = (
            ReviewStep(
                1, "Review Decisions", "worth", synthesized and not progress.worth_pending, progress.worth_pending
            ),
            ReviewStep(
                2, "Enrich Contacts", "enrich", synthesized and enrichment.status == "completed",
                enrichment.counts.pending,
            ),
            ReviewStep(
                3, "Check LinkedIn", "linkedin", synthesized and not progress.linkedin_pending,
                progress.linkedin_pending,
            ),
        )
        return ReviewPage(
            view=view,
            tab=tab,
            title=TITLES[view],
            steps=steps,
            progress=PageProgress.from_stage(progress),
            enrichment=EnrichmentPanel.from_view(enrichment),
            state_token=state.state_token,
            # Done, and Enrich once its plan is complete, show the synthesis handoff instead.
            needs_synthesis=bool(progress.synthesize_pending)
            and (view == "done" or (view == "enrich" and enrichment.status == "completed")),
            external_updates=view in EXTERNAL_UPDATE_VIEWS,
        )

    def _worth_card(self, params: Params) -> WorthCardPayload:
        queue = worth_queue(self.db)
        pick = _value(params, "pick").strip().lower()
        if pick:
            queue = [row for row in queue if row.key.lower() == pick]
            if not queue:
                raise _Refusal(HTTPStatus.NOT_FOUND, "gone")

        excluded = _excluded(params)
        queue = [row for row in queue if row.key.lower() not in excluded]
        queue.sort(key=lambda row: row.name.lower())
        if not queue:
            synthesize_pending = bool(self.adapter.snapshot().progress.synthesize_pending)
            return WorthCardPayload(card=None, synthesize_pending=synthesize_pending, queue=None)

        index = _index(params, len(queue))
        parent = person_detail(self.db, queue[index].parent_id)
        if parent is None:
            raise _Refusal(HTTPStatus.NOT_FOUND, "gone")

        return WorthCardPayload(
            card=WorthCard(ReviewPerson.from_parent(parent), ReviewCandidate.primary(parent)),
            synthesize_pending=False,
            queue=_debug_position(params, index, len(queue)),
        )

    def _worth_pending(self, _params: Params) -> WorthPending:
        return WorthPending(tuple(worth_pending_entries(worth_queue(self.db))))

    def _worth_table(self, params: Params) -> WorthTablePayload:
        pile = _value(params, "view").lower()
        if pile not in get_args(WorthPile):
            raise _Refusal(HTTPStatus.BAD_REQUEST, "view must be yes or no")

        try:
            offset = max(0, int(_value(params, "offset", "0")))
        except ValueError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "offset must be an integer") from error

        # One LIMIT/OFFSET page, sorted by name within the page.
        parents = sorted(decision_parents(self.db, pile, offset=offset), key=lambda parent: parent.name.lower())
        counts = worth_counts(self.db)
        return WorthTablePayload(
            rows=tuple(DecisionRow.from_parent(parent, pile) for parent in parents),
            total=counts.yes if pile == "yes" else counts.no,
        )

    def _linkedin_card(self, params: Params) -> LinkedinCardPayload:
        excluded = _excluded(params)
        # Re-research is read before the queue's order: a result that lands between
        # the two reads has either left the order or is still excluded here.
        retargets = self.adapter.retargets()
        inflight = {item.slug.lower() for item in retargets if item.state in IN_FLIGHT_RETARGET_STATES}
        order = linkedin_queue_order(self.db)
        queue = [row for row in order if row.slug.lower() not in excluded | inflight]
        if not queue:
            progress = self.adapter.snapshot().progress
            completed = not progress.linkedin_pending
            finished = LinkedinFinished(
                synthesize_pending=bool(progress.synthesize_pending),
                linkedin_done=progress.linkedin_done,
                linkedin_complete=completed,
                retargets_in_flight=len(inflight),
                auto_continue=not completed,
            )
            return LinkedinCardPayload(card=None, finished=finished, pending=len(order), queue=None)

        index = _index(params, len(queue))
        # Only the card on screen is hydrated; the queue itself is ids and slugs.
        parent = linkedin_queue_parent(self.db, queue[index].parent_id)
        card = LinkedinCard(
            person=ReviewPerson.from_parent(parent),
            candidates=tuple(ReviewCandidate.from_row(candidate) for candidate in parent.candidates),
            failure_note=_failed_notes(retargets).get(parent.slug, "").strip(),
        )
        return LinkedinCardPayload(
            card=card, finished=None, pending=len(order), queue=_debug_position(params, index, len(queue))
        )

    def _decide(self, form: Params) -> DecideResult:
        pub = _value(form, "pub")
        decision = _value(form, "decision")
        slug = _value(form, "parent_slug")
        if not pub or decision not in get_args(LinkedinDecision):
            raise _Refusal(HTTPStatus.BAD_REQUEST, "bad request")

        row_key, parent = self._submitted_row(pub, slug)
        note = _value(form, "note").strip()[:MAX_NOTE_CHARS]
        try:
            result = self.adapter.decide(row_key, decision, _value(form, "new_url"), note)
        except StoreError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error

        self.notify()
        self.wake_agent()
        # The next card is read AFTER the write committed: one round trip, and the
        # decided parent is never served back.
        following = self._linkedin_card({"exclude": [slug or parent.slug]})
        return DecideResult(
            ok=True,
            pub=row_key,
            action=result.action,
            approved=result.approved,
            new_url=result.new_url,
            progress=self._decision_progress(following.pending),
            resolved_pubs=result.resolved_pubs,
            next=following,
        )

    def _approve_enrichment(self, _form: Params) -> ApproveResult:
        try:
            enrichment = self.adapter.approve_enrichment()
        except (KeyError, StoreError, ValueError) as error:
            raise _Refusal(HTTPStatus.CONFLICT, str(error)) from error

        approval = enrichment.approval
        # Already running, or already done: nothing to start.
        if not approval:
            return ApproveResult(ok=True, enrichment=EnrichmentPanel.from_view(enrichment))

        if not self.run_jobs:
            raise _Refusal(HTTPStatus.CONFLICT, "enrichment job execution is disabled")

        launched = self.start_enrichment(
            enrichment.counts.total, approval.approved_budget_usd, enrichment.request_fingerprint
        )
        if launched:
            self.wake_agent()

        # The panel is re-read after the start, so the page swaps in the running bar.
        return ApproveResult(ok=True, enrichment=EnrichmentPanel.from_view(self.adapter.enrichment()))

    def _submitted_row(self, pub: str, slug: str) -> tuple[str, ParentViewRow]:
        """The identity row a card posted, and its parent."""
        try:
            resolved = self.adapter.resolve_candidate(pub)
        except StoreError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error

        if not resolved or not self.adapter.candidate(resolved[1], resolved[0]):
            raise _Refusal(HTTPStatus.NOT_FOUND, f"review row not found: {pub}")

        if slug and resolved[1].slug != slug:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "stale or mismatched person card")

        return resolved

    def _decision_progress(self, linkedin_pending: int) -> DecisionProgress:
        worth = worth_counts(self.db)
        return DecisionProgress(
            worth_pending=worth.pending,
            worth_yes=worth.yes,
            worth_no=worth.no,
            linkedin_pending=linkedin_pending,
        )


def _failed_notes(items: list[GuidanceViewRow]) -> dict[str, str]:
    """Each slug whose latest re-research failed, and why."""
    latest: dict[str, GuidanceViewRow] = {}
    for item in items:
        slug = item.slug.lower()
        if slug and slug not in latest:
            latest[slug] = item

    return {slug: item.detail or "the job did not finish" for slug, item in latest.items() if item.state == "failed"}


def _index(params: Params, size: int) -> int:
    try:
        return max(0, int(_value(params, "index", "0"))) % size
    except ValueError:
        return 0


def _excluded(params: Params) -> set[str]:
    values = _value(params, "exclude").split(",")
    return {value.strip().lower() for value in values if value.strip()}


def _debug_position(params: Params, index: int, total: int) -> QueuePosition | None:
    """The carousel's place in the queue, which only `?debug=1` asks for."""
    return QueuePosition(index, total) if _value(params, "debug") == "1" else None


def _answer(handler: BaseHTTPRequestHandler, route: Callable[[Params], Payload], fields: Params) -> None:
    try:
        payload = route(fields)
    except _Refusal as refusal:
        _send_error(handler, refusal.status, refusal.text)
        return

    _send_json(handler, asdict(payload))


def _send_error(handler: BaseHTTPRequestHandler, status: HTTPStatus, text: str) -> None:
    _send_json(handler, {"error": text}, status)


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, object], status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
