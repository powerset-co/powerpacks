"""What the Review page's routes answer with: the wire shapes and how a store row becomes one.

Each frozen dataclass here is one JSON payload, `asdict` to the wire. Those the page reads
are field for field with web/src/types/review.ts, and the `Literal` vocabularies are its
unions (tests/test_deep_context_review_api.py pins both). The `from_*` constructors hold
the shaping rules: which enrichment state a panel shows, why a person sits in a pile, what
of a candidate a card draws.

Changelog:
  2026-10-01: split out of api.py, which keeps the routes.
  2026-10-01: `contacts` is the person's (every merged record's emails and phones), not the
    first candidate's: a phone that arrived on another record was missing from the card.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from packs.ingestion.primitives.deep_context.db.view_models import CandidateViewRow, ParentViewRow
from packs.ingestion.primitives.deep_context.db.workflow_views import StageProgress
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.review.label_titles import label_titles
from packs.ingestion.primitives.deep_context.review.models import EnrichmentView

MAX_REASON_CHARS = 140

ReviewView = Literal["worth", "enrich", "linkedin", "done"]
WorthTab = Literal["review", "yes", "no"]
WorthPile = Literal["yes", "no"]
WorthCall = Literal["yes", "no", "restore"]
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
class EnrichmentPanel:
    """The Enrich screen's one panel: which of its five states the store is in."""

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
            # The estimate covers research and both judgment passes; one that rounds to $0.00 is the free continue.
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
    contacts: str

    @classmethod
    def from_parent(cls, parent: ParentViewRow) -> ReviewPerson:
        return cls(
            parent_id=parent.parent_id,
            slug=parent.slug,
            name=parent.name,
            sources=parent.sources,
            labels=label_titles(parent),
            worth_key=parent.worth_row.key,
            contacts=_contacts(parent.emails, parent.phones),
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
    avatar_url: str

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
            # A researched profile shows initials only.
            avatar_url="" if candidate.synthetic else candidate.profile_pic_url,
        )

    @classmethod
    def primary(cls, parent: ParentViewRow) -> ReviewCandidate | None:
        candidate = primary_candidate(parent)
        return cls.from_row(candidate) if candidate else None


@dataclass(frozen=True)
class QueuePosition:
    index: int
    total: int


@dataclass(frozen=True)
class WorthDetails:
    """A person with the profile shown beside them: a worth card, an opened pile row."""

    person: ReviewPerson
    candidate: ReviewCandidate | None


@dataclass(frozen=True)
class WorthCardPayload:
    card: WorthDetails | None
    synthesize_pending: bool
    queue: QueuePosition | None


@dataclass(frozen=True)
class WorthPendingEntry:
    key: str
    name: str


@dataclass(frozen=True)
class WorthPending:
    pending: tuple[WorthPendingEntry, ...]


@dataclass(frozen=True)
class DecisionRow:
    person: ReviewPerson
    reason: str

    @classmethod
    def from_parent(cls, parent: ParentViewRow, pile: WorthPile) -> DecisionRow:
        return cls(ReviewPerson.from_parent(parent), cls._reason(parent, pile))

    @staticmethod
    def _reason(parent: ParentViewRow, pile: WorthPile) -> str:
        """Why the parent sits in this pile: the human's note or call, else the machine's reason."""
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
    resolved_pubs: tuple[str, ...]
    next: LinkedinCardPayload


@dataclass(frozen=True)
class ApproveResult:
    ok: bool
    enrichment: EnrichmentPanel


@dataclass(frozen=True)
class WorthResult:
    ok: bool
    pub: str
    effective: str
    progress: DecisionProgress
    next_stage: Literal["enrich", "worth"]


@dataclass(frozen=True)
class CompleteResult:
    ok: bool
    manifest: dict[str, object]
    progress: StageProgress


@dataclass(frozen=True)
class RetargetResult:
    ok: bool
    item: dict[str, object]
    estimated_cost_usd: float


@dataclass(frozen=True)
class SignInResult:
    ok: bool
    status: str


Payload = (
    ReviewPage
    | WorthCardPayload
    | WorthPending
    | WorthTablePayload
    | WorthDetails
    | LinkedinCardPayload
    | DecideResult
    | ApproveResult
    | WorthResult
    | CompleteResult
    | RetargetResult
    | SignInResult
)


def primary_candidate(parent: ParentViewRow) -> CandidateViewRow | None:
    """The candidate a card shows beside the person: the first."""
    return parent.candidates[0] if parent.candidates else None


def _nonempty(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in items if item.strip())


def _contacts(emails: tuple[str, ...], phones: tuple[str, ...]) -> str:
    """A person's emails then phones, as the card's Contact line."""

    # The same phone arrives as E.164 and bare-local; collapse to one entry
    # per number, preferring whichever display came first.
    def phone_key(value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits[-10:] if len(digits) > 10 else digits

    shown: list[str] = []
    seen: set[str] = set()
    for value in phones:
        if not value:
            continue
        key = phone_key(value)
        if key not in seen:
            seen.add(key)
            shown.append(value)
    return " · ".join([*dict.fromkeys(value for value in emails if value), *shown])
