"""Presentation-only HTML for SQLite-hydrated Deep Context rows.

Changelog:
  2026-09-25: label badges show the title only; the percentage is gone.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from markupsafe import Markup, escape
from markdown_it import MarkdownIt

from packs.ingestion.primitives.deep_context.db.view_models import (
    CandidateViewRow,
    ParentViewRow,
    WorthRow,
)
from packs.ingestion.primitives.deep_context.db.workflow_views import StageProgress
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.review.models import EnrichmentView
from packs.ingestion.primitives.deep_context.shared.template_engine import template_environment

_TEMPLATE_DIR = Path(__file__).with_name("templates")
_TEMPLATES = template_environment(_TEMPLATE_DIR, html=True)
REVIEW_CSS = Path(__file__).with_name("reconcile_review.css")
REVIEW_JS = Path(__file__).with_name("reconcile_review.js")


def _render(template: str, **context: object) -> str:
    return _TEMPLATES.get_template(template).render(**context).strip()


def _primary_candidate(parent: ParentViewRow) -> CandidateViewRow | None:
    return parent.candidates[0] if parent.candidates else None


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "?" if not words else (
        words[0][0] + (words[-1][0] if len(words) > 1 else "")
    ).upper()


def _nonempty(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in items if item.strip())


# 33 badge titles: the 27 share labels plus the 6 worth signals. The badges show
# every label whose probability clears the threshold, highest first.
_LABEL_TITLES = (
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
_VISIBLE_LABELS = 3
_LABEL_THRESHOLD = 0.85


def _label_badges(parent: ParentViewRow) -> Markup:
    """The visible share-label badges for one parent, highest probability first."""

    def sort_key(item: tuple[str, float]) -> float:
        return -item[1]

    labels = dict(parent.labels)
    scores: dict[str, float] = {}
    for key, title in _LABEL_TITLES:
        if key in labels:
            scores[title] = max(scores.get(title, 0.0), float(labels[key]))
    relationship = str(labels.get("relationship_kind") or "")
    if relationship and relationship != "unknown" and "relationship_kind_p" in labels:
        title = relationship.replace("_", " ").capitalize()
        scores[title] = max(scores.get(title, 0.0), float(labels["relationship_kind_p"]))
    names = [
        title
        for title, score in sorted(scores.items(), key=sort_key)
        if score >= _LABEL_THRESHOLD
    ]
    if not names:
        return Markup("")
    shown = "".join(
        f"<span class='person-label'>{escape(name)}</span>"
        for name in names[:_VISIBLE_LABELS]
    )
    remaining = names[_VISIBLE_LABELS:]
    if remaining:
        tooltip = " · ".join(remaining)
        shown += (
            f"<span class='person-label-more' tabindex='0' role='button' "
            f"aria-label='{escape('More labels: ' + tooltip)}'>+{len(remaining)}"
            f"<span class='person-label-tooltip' role='tooltip'>{escape(tooltip)}</span></span>"
        )
    return Markup(f"<span class='person-labels'>{shown}</span>")


def _candidate_contacts(candidate: CandidateViewRow) -> str:
    # The same phone arrives as E.164 and bare-local; collapse to one entry
    # per number, preferring whichever display came first.
    def phone_key(value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits[-10:] if len(digits) > 10 else digits

    emails = [value for value in candidate.match_emails if value]
    phones: list[str] = []
    seen: set[str] = set()
    for value in candidate.match_phones:
        if not value:
            continue
        key = phone_key(value)
        if key not in seen:
            seen.add(key)
            phones.append(value)
    return " · ".join([*dict.fromkeys(emails), *phones])


_TEMPLATES.globals.update(
    candidate_contacts=_candidate_contacts,
    initials=_initials,
    nonempty=_nonempty,
    primary_candidate=_primary_candidate,
    label_badges=_label_badges,
)
_TEMPLATES.filters["urlencode"] = urllib.parse.quote
GO_BACK_HTML = _render("go_back.html.j2")
SYNTHESIZE_HTML = _render("synthesize_pending.html.j2")


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"(</?)h([1-6])>")

# markdown-it-py is already a direct dependency (dossier validation). The
# card sits inside an <article> with its own <h2>, so dossier headings render
# two levels deeper: <h3>..<h6>.
_MD = MarkdownIt("commonmark").enable("table")
_HEADING_SHIFT = 2


def markdown_to_html(markdown: str, *, skip_name_and_contact: bool = False) -> str:
    """Render a dossier body — the full markdown vocabulary, headings clamped.

    YAML frontmatter is file metadata, never UI content; HTML comments are
    the composer's internal markers (e.g. parent-link) and stay stripped.
    ``skip_name_and_contact`` drops the leading ``# Name`` heading and the
    ``## Contact`` section — surfaces that already show the name and contact
    above the markdown (the expanded decision rows) pass True.
    """
    body = _FRONTMATTER_RE.sub("", _COMMENT_RE.sub("", markdown), count=1)
    if skip_name_and_contact:
        body = re.sub(r"\A\s*# [^\n]*\n?", "", body, count=1)
        body = re.sub(r"\n?## Contact\n(?:(?!#)[^\n]*\n?)*", "", body, count=1)
    html = _MD.render(body)
    return _HEADING_RE.sub(
        lambda m: f"{m.group(1)}h{min(6, int(m.group(2)) + _HEADING_SHIFT)}>",
        html,
    )


def render_worth_card(parent: ParentViewRow) -> str:
    return _render(
        "worth_card.html.j2",
        parent=parent,
        candidate=_primary_candidate(parent),
    )


def render_linkedin_card(parent: ParentViewRow, candidates: tuple[CandidateViewRow, ...],
                         *, failure_note: str = "") -> str:
    if not candidates:
        return ""
    return _render(
        "linkedin_card.html.j2",
        parent=parent,
        candidates=candidates,
        failure_note=failure_note.strip(),
    )


def decision_rows_html(parents: list[ParentViewRow], decision: str) -> str:
    """Render one decision-table page's rows — an append-safe fragment."""
    target = 'no' if decision == 'yes' else 'yes'
    rows = sorted(parents, key=lambda parent: parent.name.lower())
    return "".join(
        _render(
            "decision_row.html.j2",
            parent=parent,
            target=target,
        )
        for parent in rows
    )


def render_decision_table(
    parents: list[ParentViewRow],
    decision: str,
    *,
    total: int = 0,
) -> str:
    shown = decision_rows_html(parents, decision)
    more = ""
    if total > len(parents):
        remaining = total - len(parents)
        more = (
            f"<button class='button button-outline table-more' data-table-more "
            f"data-view='{decision}' data-offset='{len(parents)}' data-remaining='{remaining}'>"
            f"Show more ({remaining} left)</button>"
        )
    return (
        f"<div class='decision-table' data-view='{decision}'>{shown}</div>{more}"
    )


@dataclass(frozen=True)
class WorthPendingEntry:
    key: str
    name: str


def worth_pending_entries(parents: list[WorthRow]) -> list[WorthPendingEntry]:
    return [
        WorthPendingEntry(parent.key, parent.name)
        for parent in sorted(parents, key=lambda item: item.name.lower())
    ]


def worth_search_html(view: str, pending: list[WorthPendingEntry] | None = None) -> str:
    pending_json = ""
    if pending is not None:
        pending_json = json.dumps(
            [asdict(row) for row in pending], ensure_ascii=False,
        ).replace("<", "\\u003c")
    return _render(
        "worth_search.html.j2",
        view=view,
        pending=pending,
        pending_json=Markup(pending_json),
    )


def render_decision_tabs(progress: StageProgress, active: str, *, preview: bool = False) -> str:
    suffix = "&amp;preview=1" if preview else ""
    tabs = (("review", "Review", progress.worth_pending), ("yes", "Yes", progress.worth_yes),
            ("no", "No", progress.worth_no))
    return _render(
        "decision_tabs.html.j2", tabs=tabs, active=active, suffix=Markup(suffix),
    )


def _value(params: dict[str, list[str]], key: str, default: str = "") -> str:
    return str((params.get(key) or [default])[0])


def _phase_view(params: dict[str, list[str]]) -> str:
    requested = _value(params, "stage").lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(enrichment: EnrichmentView) -> str:
    status = enrichment.status
    if status == ReceiptStatus.RUNNING:
        total = max(0, enrichment.counts.total)
        completed = min(total, max(0, enrichment.counts.completed))
        percent = round((completed / total) * 100) if total else 0
        label = f"{completed} of {total} complete"
        return _render(
            "enrichment.html.j2", mode="running", completed=completed,
            total=total, percent=percent, label=label,
        )
    if status == ReceiptStatus.NEEDS_APPROVAL or enrichment.state == "profile_prep_pending":
        # The button carries the estimate ("Approve $X"); no redundant
        # paragraph above it. Cached-only plans spend nothing, so their
        # continue is not an approval and must not read like one.
        label = (
            f"Approve ${enrichment.estimated_usd:.2f}"
            if enrichment.would_submit
            else "Continue"
        )
        return _render(
            "enrichment.html.j2",
            mode="approval",
            approval_label=label,
        )
    if status == "completed":
        return _render("enrichment.html.j2", mode="completed")
    if status == ReceiptStatus.FAILED:
        return _render("enrichment.html.j2", mode="failed", error=enrichment.error)
    return _render("enrichment.html.j2", mode="preparing")


def _empty_state(title: str, body: str = "", *, extra_class: str = "") -> str:
    return _render(
        "empty_state.html.j2", title=title, body=Markup(body),
        extra_class=extra_class,
    )


def _step(number: int, label: str, active: bool, complete: bool, count: int = 0, href: str = "") -> str:
    state = " active" if active else (" complete" if complete and not count else "")
    marker = "✓" if complete and not count else str(number)
    return _render(
        "step.html.j2", state=state, marker=marker, label=label, count=count,
        href=href,
    )


def _carousel_nav() -> str:
    return _render("carousel_nav.html.j2")


def worth_finished_body(progress: StageProgress, *, auto_continue: bool = False) -> str:
    if progress.synthesize_pending:
        return SYNTHESIZE_HTML
    return _render(
        "worth_finished.html.j2", progress=progress, auto_continue=auto_continue,
    )


def linkedin_finished_body(progress: StageProgress, *, linkedin_complete: bool,
                           retargets_in_flight: int = 0, auto_continue: bool = False) -> str:
    if progress.synthesize_pending:
        return SYNTHESIZE_HTML
    return _render(
        "linkedin_finished.html.j2",
        progress=progress,
        linkedin_complete=linkedin_complete,
        retargets_in_flight=retargets_in_flight,
        auto_continue=auto_continue,
        go_back=Markup(GO_BACK_HTML),
    )


def render_person_detail(parent: ParentViewRow) -> str:
    candidate = _primary_candidate(parent)
    dossier = markdown_to_html(parent.dossier_body)
    key = parent.worth_row.key
    effective = parent.worth_row.effective.lower()
    targets = ("no",) if effective == "yes" else (("yes",) if effective == "no" else ("yes", "no"))
    menu_key = candidate.row_key if candidate else key
    return _render(
        "person_detail.html.j2",
        parent=parent,
        candidate=candidate,
        targets=targets,
        menu_key=menu_key,
        dossier=Markup(dossier),
    )


def directory_page_html(parents: list[ParentViewRow], params: dict[str, list[str]],
                        *, next_action: str) -> bytes:
    entries = [
        {"slug": parent.slug, "name": parent.name,
         "worth": parent.worth_row.effective.lower()}
        for parent in sorted(parents, key=lambda parent: parent.name.lower())
        if parent.slug
    ]
    selected = _value(params, "person").lower()
    parent: ParentViewRow | None = next(
        (item for item in parents if item.slug.lower() == selected), None
    )
    detail = render_person_detail(parent) if parent else _empty_state(f"{len(entries)} people")
    payload = json.dumps(entries, ensure_ascii=False).replace("<", "\\u003c")
    counts = {
        decision: sum(entry["worth"] == decision for entry in entries)
        for decision in ("yes", "maybe", "no")
    }
    tabs = tuple(
        (decision, counts[decision])
        for decision in ("yes", "maybe", "no")
        if decision != "maybe" or counts[decision]
    )
    banner = {"synthesize": SYNTHESIZE_HTML, "realize": GO_BACK_HTML}.get(next_action, "")
    content = _render(
        "directory.html.j2",
        banner=Markup(banner),
        tabs=tabs,
        detail=Markup(detail),
        payload=Markup(payload),
    )
    return page_html("Directory", "directory", content)


def page_html(title: str, stage: str, content: str, *, preview: bool = False,
              external_updates: bool = False, state_token: str = "",
              enrichment_status: str = "", stepper: str = "") -> bytes:
    return _render(
        "page.html.j2",
        title=title,
        stage=stage,
        preview=str(preview),
        external_updates=str(external_updates),
        state_token=state_token,
        enrichment_status=enrichment_status,
        stepper=Markup(stepper),
        content=Markup(content),
    ).encode()
