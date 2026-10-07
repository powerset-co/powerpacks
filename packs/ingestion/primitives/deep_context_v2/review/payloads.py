"""What the Check LinkedIn page reads: its JSON shapes (field for field with web/src/types/review.ts), how a
card becomes one, and the dossier fragment the card shows under the person.

Created: 2026-10-07
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind, SourceChannel
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, Pending

TITLE = "Check LinkedIn"
# The page's names for the channels.
SOURCE_NAMES: dict[str, str] = {SourceChannel.GMAIL.value: "gmail", SourceChannel.IMESSAGE.value: "imessage",
                                SourceChannel.WHATSAPP.value: "whatsapp"}
# The badge titles: a family shows every label whose probability clears the threshold, highest first.
LABEL_TITLES: tuple[tuple[str, str], ...] = (
    ("is_family", "Family"), ("is_close_friend", "Close friend"),
    ("is_founder", "Founder"), ("is_investor", "Investor"),
    ("is_coworker_current", "Coworker"), ("is_coworker_past", "Former coworker"),
    ("is_classmate", "Classmate"), ("is_mentor_or_advisor", "Mentor / advisor"),
    ("is_client", "Client"), ("is_recruiter", "Recruiter"),
    ("is_service_provider", "Service provider"), ("is_automated_sender", "Automated sender"),
    ("is_stranger", "Stranger"), ("is_transactional", "Transactional"),
    ("is_professional", "Work-related"), ("is_personal", "Personal"),
    ("is_vendor_or_partner", "Vendor / partner"), ("is_mentee_or_report", "Mentee / report"),
    ("is_neighbor_or_local", "Neighbor / local"), ("met_in_person", "Met in person"),
    ("owner_would_intro", "Would introduce"), ("they_would_take_owner_call", "Would take your call"),
    ("notable", "Public figure"), ("sensitive_context", "Sensitive topics"),
    ("is_healthcare_legal_or_financial_provider", "Medical / legal / financial services"),
    ("confidential_dealings", "Confidential"), ("is_minor", "Under 18"),
    ("real_relationship", "Direct contact"), ("work_signal", "Work-related"),
    ("professional_standing", "Established professional"), ("noise", "Spam / broadcasts"),
    ("transactional_only", "Transactional"), ("evidence_incomplete", "Limited context"),
)
LABEL_THRESHOLD = 0.85


@dataclass(frozen=True)
class PageProgress:
    worth_pending: int
    worth_yes: int
    worth_no: int
    linkedin_pending: int
    linkedin_done: int
    rejected: int
    synthesize_pending: int


@dataclass(frozen=True)
class EnrichmentPanel:
    mode: str
    completed: int
    total: int
    approval_label: str
    error: str


@dataclass(frozen=True)
class ReviewPage:
    view: str
    tab: str
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
    confidence: float | None
    verdict: str
    reason: str


@dataclass(frozen=True)
class LinkedinCard:
    person: ReviewPerson
    candidates: tuple[ReviewCandidate, ...]


@dataclass(frozen=True)
class LinkedinFinished:
    synthesize_pending: bool


@dataclass(frozen=True)
class QueuePosition:
    index: int
    total: int


@dataclass(frozen=True)
class LinkedinCardPayload:
    card: LinkedinCard | None
    finished: LinkedinFinished | None
    pending: int
    queue: QueuePosition | None


@dataclass(frozen=True)
class DecideResult:
    """The page reads only `next` (and repaints its count from next.pending)."""

    ok: bool
    next: LinkedinCardPayload


def page(pending: int) -> ReviewPage:
    """The one screen. The page's stepper fields for other stages stay zero: there are no other stages here."""
    return ReviewPage("linkedin", "", TITLE, PageProgress(0, 0, 0, pending, 0, 0, 0),
                      EnrichmentPanel("completed", 0, 0, "", ""), "", False, False)


def _labels(labels_json: str | None) -> tuple[str, ...]:
    """Every badge title over the threshold, highest probability first; none when worth never judged."""
    if labels_json is None:
        return ()
    labels: dict[str, Any] = json.loads(labels_json)
    scores: dict[str, float] = {}
    for key, title in LABEL_TITLES:
        if key in labels:
            scores[title] = max(scores.get(title, 0.0), float(labels[key]))
    kind: str = str(labels.get("relationship_kind") or "")
    if kind and kind != "unknown" and "relationship_kind_p" in labels:
        title: str = kind.replace("_", " ").capitalize()
        scores[title] = max(scores.get(title, 0.0), float(labels["relationship_kind_p"]))
    ranked: list[tuple[float, str]] = []
    for title, score in scores.items():
        if score >= LABEL_THRESHOLD:
            ranked.append((-score, title))
    titles: list[str] = []
    for _, title in sorted(ranked):
        titles.append(title)
    return tuple(titles)


def person_name(card: Card) -> str:
    """The first member's written name; the dossier's name when no member has one."""
    for member in card.members:
        if member.display_name:
            return member.display_name
    return card.facts.canonical_name


def person(card: Card) -> ReviewPerson:
    sources: list[str] = []
    for source in card.sources:
        sources.append(SOURCE_NAMES[source])
    # The Contact line: every member's emails, then phones, once each.
    contacts: list[str] = []
    for kind in (IdentifierKind.EMAIL, IdentifierKind.PHONE):
        seen: set[str] = set()
        for identifier in card.identifiers:
            if identifier.kind == kind and identifier.normalized_value not in seen:
                seen.add(identifier.normalized_value)
                contacts.append(identifier.display_value)
    return ReviewPerson(card.parent_id, card.parent_id, person_name(card), tuple(sources), _labels(card.labels_json),
                        card.parent_id, " · ".join(contacts))


def _research_candidate(pending: Pending, research: dict[str, Any]) -> ReviewCandidate:
    """A synthetic card: the research's person, with no LinkedIn and no picture."""
    experiences: list[str] = []
    for row in research.get("work_experience") or []:
        experiences.append(f"{row.get('title') or '?'} @ {row.get('company_name') or '?'}")
    education: list[str] = []
    for row in research.get("education") or []:
        parts: list[str] = []
        for value in (row.get("degree"), row.get("field_of_study"), row.get("school_name")):
            if value:
                parts.append(value)
        education.append(", ".join(parts))
    places: list[str] = []
    for value in (research.get("location_city"), research.get("location_country")):
        if value:
            places.append(value)
    return ReviewCandidate(pending.key, research.get("real_name") or "", "", research.get("summary") or "",
                           ", ".join(places), tuple(experiences), tuple(education), True, "", None, "", "")


def candidates(card: Card) -> tuple[ReviewCandidate, ...]:
    """What the card offers. With nothing pending the page still needs one entry: the family itself, no URL."""
    shown: list[ReviewCandidate] = []
    for pending in card.pending:
        if pending.research is not None:
            shown.append(_research_candidate(pending, pending.research))
        elif pending.profile is not None:
            profile = pending.profile
            shown.append(ReviewCandidate(pending.key, profile.full_name, pending.linkedin_url, profile.headline,
                                         profile.location, profile.experiences, profile.education, False, "", None,
                                         "needs_review", ""))
        else:
            # Not in the profile cache: the URL alone.
            shown.append(ReviewCandidate(pending.key, "", pending.linkedin_url, "", "", (), (), False, "", None,
                                         "needs_review", ""))
    return tuple(shown)


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    items: list[str] = []
    for line in lines:
        items.append(f"<li>{html.escape(line)}</li>")
    return f"<h3>{html.escape(title)}</h3><ul>{''.join(items)}</ul>"


def dossier(card: Card) -> str:
    """The family's dossier under the card: members, messages, and the collapsed facts."""
    facts = card.facts
    members: list[str] = []
    for member in card.members:
        members.append(member.display_name or member.candidate_id)
    messages: list[str] = []
    for channel, count in card.messages.items():
        messages.append(f"{channel}: {count}")
    about: list[str] = []
    for value in (facts.title, facts.location, facts.relationship_to_owner, facts.relationship_category):
        if value:
            about.append(value)
    if facts.school:
        about.append(", ".join([facts.school, facts.field_of_study]).rstrip(", "))
    employers: list[str] = []
    for employer in facts.employers:
        employers.append(f"{employer.role or '?'} @ {employer.name} ({employer.status})")
    events: list[str] = []
    for event in facts.notable_events:
        events.append(f"{event.date}: {event.summary}")
    shared: list[str] = []
    for context in facts.shared_context:
        shared.append(f"{context.overlap}: {context.detail}")
    return "".join([
        _section("Members", members), _section("Messages", messages), _section("About", about),
        _section("Employers", employers), _section("Topics", list(facts.topics)), _section("Notable events", events),
        _section("Shared context", shared),
    ])
