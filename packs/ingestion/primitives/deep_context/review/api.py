"""The Review page's routes: its reads under /api/review/ and every write it makes.

Flow: `server.make_handler` builds one `ReviewApi` and asks it before its own routes.

GET  /api/review/page?stage=&view=                        the screen to draw, the stepper,
                                                          the counts, the Enrich panel
GET  /api/review/worth-card?pick=&exclude=&index=&debug=  the next undecided person;
                                                          404 when `pick` is no longer pending
GET  /api/review/worth-pending                            the typeahead's names
GET  /api/review/worth-table?view=yes|no&offset=N         one page of a decided pile: names,
                                                          labels and reasons, no profiles
GET  /api/review/worth-details?slug=                      the person and profile an opened
                                                          pile row shows; 404 when gone
GET  /api/review/linkedin-card?exclude=&index=&debug=     the next LinkedIn card, or the
                                                          finished state
POST /api/review/decide              form pub, decision, new_url, parent_slug, note: one
                                     LinkedIn decision; answers with the next card
POST /api/review/approve-enrichment  approve the estimate and start the pipeline
POST /worth                          form pub, worth=yes|no|restore, parent_slug, note
POST /complete                       form stage=worth|enrich|linkedin: wakes the agent
POST /retarget                       form pub, parent_slug, guidance: PAID re-research
POST /feedback                       form pub, parent_slug, comment, action: files it with
                                     Powerset; the body is Powerset's reply, 502 unless submitted
POST /auth/login                     opens the Powerset sign-in on this machine

Any other path under /api/review/ is a JSON 404. The queue rules (exclude, in-flight
re-research, index, pick, sort) live here; the page only draws what it is given. Bodies
are form-encoded, a POST from another origin is refused, and an error is
`{"error": text}`. What each route answers with is a dataclass in payloads.py.

Changelog:
  2026-10-02: the finished LinkedIn state no longer asks the page to press Finish
    (`auto_continue`): /complete changes nothing in the store, so the page pressed it in a
    loop while a re-research was out.
  2026-10-01: the page carries no step list; worth overrides remain available.
  2026-09-30: created beside the Jinja page.
  2026-10-01: follows #635's review page. The approve label counts the judgment estimate;
    candidates carry `avatar_url`; a pile page no longer hydrates profiles, so
    `worth-details` serves the one an opened row shows.
  2026-10-01: the Jinja page is gone and this is the review page's one home. /worth,
    /complete, /retarget, /feedback and /auth/login moved here from server.py with their
    lookups (one `_submitted_row`, one `_decision_progress`); their errors are now
    `{"error": text}`, and /worth answers with the fields the page reads. The request
    helpers and the candidate shaping moved here from rendering.py. The judge's stored
    question is no longer served: the card always asks the usual one.
  2026-10-01: this module is the routes alone. The payload shapes moved to payloads.py
    and the Powerset sign-in to auth_login.py.
  2026-10-01: the LinkedIn routes read the server's queue (linkedin_queue.py) instead of
    deriving who is pending on every click, a decision no longer recounts the worth piles
    or loads its parent's full record, and its answer carries no `progress` (the next
    card's `pending` is the count).
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Callable, Protocol, get_args

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.identity_views import (
    decision_parents,
    linkedin_candidate_shown,
    linkedin_queue_parent,
    resolve_identity_key,
)
from packs.ingestion.primitives.deep_context.db.models import PARENT_WORTH_PREFIX
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.view_models import CandidateViewRow, ParentViewRow, WorthRow
from packs.ingestion.primitives.deep_context.db.worth_views import worth_counts, worth_queue, worth_row
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import GuidanceOutcome
from packs.ingestion.primitives.deep_context.review import auth_login
from packs.ingestion.primitives.deep_context.review.enrichment import STAGE_BY_ACTION, STAGES
from packs.ingestion.primitives.deep_context.review.linkedin_queue import LinkedinQueue
from packs.ingestion.primitives.deep_context.review.feedback import (
    FEEDBACK_ACTIONS,
    build_feedback_request,
    post_feedback_quietly,
    submit_directory_feedback,
)
from packs.ingestion.primitives.deep_context.review.models import FeedbackSubmission
from packs.ingestion.primitives.deep_context.review.payloads import (
    EXTERNAL_UPDATE_VIEWS,
    TITLES,
    ApproveResult,
    CompleteResult,
    DecideResult,
    DecisionProgress,
    DecisionRow,
    EnrichmentPanel,
    LinkedinCard,
    LinkedinCardPayload,
    LinkedinDecision,
    LinkedinFinished,
    PageProgress,
    Payload,
    QueuePosition,
    RetargetResult,
    ReviewCandidate,
    ReviewPage,
    ReviewPerson,
    ReviewView,
    SignInResult,
    WorthCall,
    WorthCardPayload,
    WorthDetails,
    WorthPending,
    WorthPendingEntry,
    WorthPile,
    WorthResult,
    WorthTab,
    WorthTablePayload,
    primary_candidate,
)
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import SqliteReviewAdapter
from packs.ingestion.primitives.share.web.server import LOCAL_HOSTS

API_PREFIX = "/api/review/"
FEEDBACK_PATH = "/feedback"
MAX_FORM_BYTES = 32_768
MAX_NOTE_CHARS = 2000
MAX_GUIDANCE_CHARS = 2000
MAX_COMMENT_CHARS = 4000
# What one re-research is expected to cost; the agent's launcher reads it from /api/retargets.
ESTIMATED_COST_USD = 0.06
# Non-terminal wire-level progress codes (GuidanceOutcome.state / GuidanceViewRow.state) —
# not the coarse persisted GuidanceState set in identity_reconcile/guidance.py.
IN_FLIGHT_RETARGET_STATES = frozenset({"queued", "researching", "judging", "hydrating"})
Params = dict[str, list[str]]


class GuidedRetargets(Protocol):
    """The re-research worker, as the routes use it."""

    def resume(self) -> int: ...

    def submit(self, request: GuidanceRequest) -> GuidanceOutcome: ...


class _Refusal(Exception):
    """A request the route turns down: the status and the words the page shows."""

    def __init__(self, status: HTTPStatus, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


class ReviewApi:
    """The Review page's reads and writes, mountable in any stdlib handler."""

    def __init__(
        self,
        *,
        db: Db,
        adapter: SqliteReviewAdapter,
        start_enrichment: Callable[[int, float, str], bool],
        notify: Callable[[], None],
        wake_agent: Callable[[], None],
        run_jobs: bool,
        guided_retargets: GuidedRetargets | None,
        linkedin: LinkedinQueue,
    ) -> None:
        self.db = db
        self.adapter = adapter
        self.linkedin = linkedin
        self.start_enrichment = start_enrichment
        self.notify = notify
        self.wake_agent = wake_agent
        self.run_jobs = run_jobs
        # None on a server that runs no jobs: /retarget is refused there.
        self.guided_retargets = guided_retargets
        self._get_routes: dict[str, Callable[[Params], Payload]] = {
            "page": self._page,
            "worth-card": self._worth_card,
            "worth-pending": self._worth_pending,
            "worth-table": self._worth_table,
            "worth-details": self._worth_details,
            "linkedin-card": self._linkedin_card,
        }
        self._post_routes: dict[str, Callable[[Params], Payload]] = {
            f"{API_PREFIX}decide": self._decide,
            f"{API_PREFIX}approve-enrichment": self._approve_enrichment,
            "/worth": self._worth,
            "/complete": self._complete,
            "/retarget": self._retarget,
            "/auth/login": self._sign_in,
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
        feedback = parsed.path == FEEDBACK_PATH
        route = self._post_routes.get(parsed.path)
        if route is None and not feedback:
            if not parsed.path.startswith(API_PREFIX):
                return False

            _send_error(handler, HTTPStatus.NOT_FOUND, "not found")
            return True

        origin = (handler.headers.get("Origin") or "").strip()
        if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            _send_error(handler, HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            return True

        length = min(int(handler.headers.get("Content-Length", "0")), MAX_FORM_BYTES)
        form = urllib.parse.parse_qs(handler.rfile.read(length).decode())
        if route is None:
            self._answer_feedback(handler, form)
        else:
            _answer(handler, route, form)
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

        return ReviewPage(
            view=view,
            tab=tab,
            title=TITLES[view],
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
            card=WorthDetails(ReviewPerson.from_parent(parent), ReviewCandidate.primary(parent)),
            synthesize_pending=False,
            queue=_debug_position(params, index, len(queue)),
        )

    def _worth_pending(self, _params: Params) -> WorthPending:
        queue = sorted(worth_queue(self.db), key=lambda row: row.name.lower())
        return WorthPending(tuple(WorthPendingEntry(row.key, row.name) for row in queue))

    def _worth_table(self, params: Params) -> WorthTablePayload:
        pile = _value(params, "view").lower()
        if pile not in get_args(WorthPile):
            raise _Refusal(HTTPStatus.BAD_REQUEST, "view must be yes or no")

        try:
            offset = max(0, int(_value(params, "offset", "0")))
        except ValueError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "offset must be an integer") from error

        # One LIMIT/OFFSET page, sorted by name within the page. A page carries no
        # candidates or sources: an opened row reads them from worth-details.
        parents = sorted(decision_parents(self.db, pile, offset=offset), key=lambda parent: parent.name.lower())
        counts = worth_counts(self.db)
        return WorthTablePayload(
            rows=tuple(DecisionRow.from_parent(parent, pile) for parent in parents),
            total=counts.yes if pile == "yes" else counts.no,
        )

    def _worth_details(self, params: Params) -> WorthDetails:
        parent = person_detail(self.db, _value(params, "slug"))
        if parent is None:
            raise _Refusal(HTTPStatus.NOT_FOUND, "gone")

        return WorthDetails(ReviewPerson.from_parent(parent), ReviewCandidate.primary(parent))

    def _linkedin_card(self, params: Params) -> LinkedinCardPayload:
        excluded = _excluded(params)
        # Re-research is read before the queue's order: a result that lands between
        # the two reads has either left the order or is still excluded here.
        retargets = self.adapter.retargets()
        inflight = {item.slug.lower() for item in retargets if item.state in IN_FLIGHT_RETARGET_STATES}
        while True:
            order = self.linkedin.rows()
            queue = [row for row in order if row.slug.lower() not in excluded | inflight]
            if not queue:
                return LinkedinCardPayload(
                    card=None, finished=self._linkedin_finished(len(inflight)), pending=len(order), queue=None
                )

            index = _index(params, len(queue))
            # Only the card on screen is hydrated; the queue itself is ids and slugs.
            parent = linkedin_queue_parent(self.db, queue[index].parent_id)
            if parent.candidates:
                break

            # Settled since the queue was read (a re-research, another process): it leaves,
            # unless the store has made it pending again since this read.
            self.linkedin.settle(parent.parent_id)

        card = LinkedinCard(
            person=ReviewPerson.from_parent(parent),
            candidates=tuple(ReviewCandidate.from_row(candidate) for candidate in parent.candidates),
        )
        return LinkedinCardPayload(
            card=card, finished=None, pending=len(order), queue=_debug_position(params, index, len(queue))
        )

    def _linkedin_finished(self, retargets_in_flight: int) -> LinkedinFinished:
        progress = self.adapter.snapshot().progress
        return LinkedinFinished(
            synthesize_pending=bool(progress.synthesize_pending),
            linkedin_done=progress.linkedin_done,
            retargets_in_flight=retargets_in_flight,
        )

    def _decide(self, form: Params) -> DecideResult:
        pub = _value(form, "pub")
        decision = _value(form, "decision")
        slug = _value(form, "parent_slug")
        if not pub or decision not in get_args(LinkedinDecision):
            raise _Refusal(HTTPStatus.BAD_REQUEST, "bad request")

        row_key, parent_id, parent_slug = self._decided_row(pub, slug)
        note = _value(form, "note").strip()[:MAX_NOTE_CHARS]
        try:
            result = self.adapter.decide(row_key, decision, _value(form, "new_url"), note)
        except StoreError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error

        self.linkedin.settle(parent_id)
        self.notify()
        self.wake_agent()
        # The next card is read AFTER the write committed: one round trip, and the
        # decided parent is never served back. Its `pending` is the count the page repaints.
        following = self._linkedin_card({"exclude": [slug or parent_slug]})
        return DecideResult(
            ok=True,
            pub=row_key,
            action=result.action,
            approved=result.approved,
            new_url=result.new_url,
            resolved_pubs=result.resolved_pubs,
            next=following,
        )

    def _decided_row(self, pub: str, slug: str) -> tuple[str, str, str]:
        """The identity row a LinkedIn card posted: its key, its parent and the parent's slug.

        A queued parent is named by the queue; any other row takes the full lookup, which
        also refuses a row no card shows.
        """
        try:
            resolved = resolve_identity_key(self.db, pub)
        except StoreError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error

        queued_slug = self.linkedin.slug(resolved[1]) if resolved else None
        if resolved and queued_slug is not None and linkedin_candidate_shown(self.db, resolved[0]):
            if slug and queued_slug != slug:
                raise _Refusal(HTTPStatus.BAD_REQUEST, "stale or mismatched person card")
            return resolved[0], resolved[1], queued_slug

        hit = self._submitted_row(pub, slug)
        if not hit:
            raise _Refusal(HTTPStatus.NOT_FOUND, f"review row not found: {pub}")

        row_key, parent, _ = hit
        return row_key, parent.parent_id, parent.slug

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

    def _worth(self, form: Params) -> WorthResult:
        pub = _value(form, "pub")
        value = _value(form, "worth").strip().lower()
        if value not in get_args(WorthCall):
            raise _Refusal(HTTPStatus.BAD_REQUEST, "worth must be yes, no, or restore")

        slug = _value(form, "parent_slug").strip()
        parent = person_detail(self.db, slug) if slug else None
        if not parent:
            raise _Refusal(HTTPStatus.NOT_FOUND, "person not found")

        key = parent.worth_row.key
        if pub and pub != key:
            raise _Refusal(HTTPStatus.NOT_FOUND, "worth row not found")

        try:
            self.adapter.set_worth(key, value, _value(form, "note").strip()[:MAX_NOTE_CHARS])
        except StoreError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error

        row: WorthRow | None = worth_row(self.db, key)
        if row is None:
            raise _Refusal(HTTPStatus.CONFLICT, "written worth row is missing")

        # The write is what the page waits on, so the answer is the counts it repaints
        # and nothing more: the full workflow state costs seconds on a large store.
        # A worth decision can move a person in or out of the LinkedIn queue.
        self.linkedin.forget()
        progress = self._decision_progress(len(self.linkedin.rows()))
        self.notify()
        self.wake_agent()
        return WorthResult(
            ok=True,
            pub=pub,
            effective=row.effective,
            progress=progress,
            next_stage="enrich" if progress.worth_pending == 0 else "worth",
        )

    def _complete(self, form: Params) -> CompleteResult:
        stage = _value(form, "stage").strip().lower()
        if stage not in STAGES:
            raise _Refusal(HTTPStatus.CONFLICT, f"unknown review stage: {stage}")

        state = self.adapter.snapshot()
        manifest = {**self.adapter.manifest(stage, state=state).as_dict(), "status": "completed"}
        self.notify()
        self.wake_agent()
        return CompleteResult(ok=True, manifest=manifest, progress=state.progress)

    def _retarget(self, form: Params) -> RetargetResult:
        pub = _value(form, "pub")
        guidance = _value(form, "guidance").strip()
        slug = _value(form, "parent_slug").strip()
        if not guidance or len(guidance) > MAX_GUIDANCE_CHARS:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "guidance must be 1-2000 characters")

        if self.guided_retargets is None:
            raise _Refusal(HTTPStatus.SERVICE_UNAVAILABLE, "in-app jobs are disabled on this server")

        row_key, parent, candidate = self._retarget_subject(pub, slug)
        request = GuidanceRequest(
            slug=parent.slug or slug,
            row_key=row_key,
            name=parent.name,
            guidance=guidance,
            person_ids=parent.person_ids,
            linkedin_url=candidate.url if candidate else "",
            submitted_at=now_iso(),
            match_emails=candidate.match_emails if candidate else (),
            match_phones=candidate.match_phones if candidate else (),
        )
        try:
            item = self.guided_retargets.submit(request)
        except (ValueError, StoreError) as error:
            raise _Refusal(HTTPStatus.CONFLICT, str(error)) from error

        try:
            feedback = build_feedback_request(
                parent, candidate, action="retarget", comment=guidance, retarget_items=[item]
            )
            threading.Thread(target=post_feedback_quietly, args=(feedback,), daemon=True).start()
        except SystemExit:
            pass

        self.notify()
        self.wake_agent()
        return RetargetResult(ok=True, item=item.as_dict(), estimated_cost_usd=ESTIMATED_COST_USD)

    def _retarget_subject(self, pub: str, slug: str) -> tuple[str, ParentViewRow, CandidateViewRow | None]:
        """Who to re-research: the posted candidate's parent, else the parent itself by its first person."""
        if pub:
            hit = self._submitted_row(pub, slug)
            if not hit:
                raise _Refusal(HTTPStatus.NOT_FOUND, "review row not found")

            return hit

        parent = person_detail(self.db, slug)
        if not parent:
            raise _Refusal(HTTPStatus.NOT_FOUND, "person not found")

        if not parent.person_ids:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "person has no research key")

        return parent.person_ids[0], parent, None

    def _answer_feedback(self, handler: BaseHTTPRequestHandler, form: Params) -> None:
        """Powerset's reply is the body, whatever keys it carries; unless it says `submitted`, a 502."""
        try:
            feedback = self._feedback(form)
        except _Refusal as refusal:
            _send_error(handler, refusal.status, refusal.text)
            return

        submitted = feedback.status == "submitted"
        status = HTTPStatus.OK if submitted else HTTPStatus.BAD_GATEWAY
        _send_json(handler, {"ok": submitted, **feedback.as_dict()}, status)

    def _feedback(self, form: Params) -> FeedbackSubmission:
        comment = _value(form, "comment").strip()
        action = _value(form, "action").strip()
        if not comment or len(comment) > MAX_COMMENT_CHARS:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "comment must be 1-4000 characters")

        if action not in FEEDBACK_ACTIONS:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "unknown feedback action")

        parent, candidate = self._feedback_subject(_value(form, "pub"), _value(form, "parent_slug").strip())
        request = build_feedback_request(
            parent, candidate, action=action, comment=comment, retarget_items=self.adapter.retargets()
        )
        return submit_directory_feedback(request)

    def _feedback_subject(self, pub: str, slug: str) -> tuple[ParentViewRow, CandidateViewRow | None]:
        """Who the feedback is about: a worth row's parent, a posted candidate, else the parent by slug."""
        if pub.startswith(PARENT_WORTH_PREFIX):
            parent = person_detail(self.db, pub.removeprefix(PARENT_WORTH_PREFIX))
            if not parent or parent.worth_row.key != pub:
                raise _Refusal(HTTPStatus.NOT_FOUND, "review row not found")

            return parent, None

        if pub:
            hit = self._submitted_row(pub, slug)
            if not hit:
                raise _Refusal(HTTPStatus.NOT_FOUND, "review row not found")

            return hit[1], hit[2]

        parent = person_detail(self.db, slug)
        if not parent:
            raise _Refusal(HTTPStatus.NOT_FOUND, "person not found")

        return parent, primary_candidate(parent)

    def _sign_in(self, _form: Params) -> SignInResult:
        return SignInResult(ok=True, status=auth_login.start_auth_login())

    def _submitted_row(self, pub: str, slug: str) -> tuple[str, ParentViewRow, CandidateViewRow] | None:
        """The identity row a card posted, with its parent and candidate; None when no card shows it."""
        try:
            resolved = self.adapter.resolve_candidate(pub)
        except StoreError as error:
            raise _Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error

        if not resolved:
            return None

        row_key, parent = resolved
        candidate = self.adapter.candidate(parent, row_key)
        if not candidate:
            return None

        if slug and parent.slug != slug:
            raise _Refusal(HTTPStatus.BAD_REQUEST, "stale or mismatched person card")

        return row_key, parent, candidate

    def _decision_progress(self, linkedin_pending: int) -> DecisionProgress:
        worth = worth_counts(self.db)
        return DecisionProgress(
            worth_pending=worth.pending,
            worth_yes=worth.yes,
            worth_no=worth.no,
            linkedin_pending=linkedin_pending,
        )


def _value(params: Params, key: str, default: str = "") -> str:
    return str((params.get(key) or [default])[0])


def _phase_view(params: Params) -> str:
    """The stage the URL asks for, or "" when it asks for none."""
    requested = _value(params, "stage").lower()
    return requested if requested in get_args(ReviewView) else ""


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
