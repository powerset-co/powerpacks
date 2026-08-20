"""Presentation-only HTML for SQLite-hydrated Deep Context rows."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from markupsafe import Markup
from markdown_it import MarkdownIt

from packs.ingestion.primitives.deep_context.db.people_views import (
    CandidateViewRow,
    ParentViewRow,
)
from packs.ingestion.primitives.deep_context.db.view_models import WorthRow
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
)
_TEMPLATES.filters["urlencode"] = urllib.parse.quote
GO_BACK_HTML = _render("go_back.html.j2")


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


def _phase_view(params: dict[str, list[str]]) -> str:
    requested = str((params.get("stage") or [""])[0]).lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(enrichment: EnrichmentView) -> str:
    status = enrichment.status or enrichment.state or "not_started"
    if status in {ReceiptStatus.RUNNING, "submitted"}:
        total = max(0, enrichment.counts.total)
        completed = min(total, max(0, enrichment.counts.completed))
        percent = round((completed / total) * 100) if total else 0
        return _render(
            "enrichment.html.j2", mode="running", completed=completed,
            total=total, percent=percent,
        )
    if status == ReceiptStatus.NEEDS_APPROVAL or enrichment.state == "profile_prep_pending":
        estimate = enrichment.estimated_usd
        detail = (
            f"Parallel estimate: ${estimate:.2f}. " if estimate else "No Parallel charge is estimated. "
        ) + "Approval also covers profile fetches and identity-judge calls, which are not included in that estimate."
        return _render(
            "enrichment.html.j2",
            mode="approval",
            approval_label=f"Approve ${enrichment.estimated_usd:.2f}",
            approval_detail=detail,
        )
    if status == "completed":
        return _render("enrichment.html.j2", mode="completed")
    if status in {ReceiptStatus.FAILED, "completed_with_errors"}:
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
    return _render(
        "worth_finished.html.j2", progress=progress, auto_continue=auto_continue,
    )


def linkedin_finished_body(progress: StageProgress, *, linkedin_complete: bool,
                           retargets_in_flight: int = 0, auto_continue: bool = False) -> str:
    return _render(
        "linkedin_finished.html.j2",
        progress=progress,
        linkedin_complete=linkedin_complete,
        retargets_in_flight=retargets_in_flight,
        auto_continue=auto_continue and not linkedin_complete,
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
                        *, handoff: bool = False) -> bytes:
    entries = [
        {"slug": parent.slug, "name": parent.name,
         "worth": parent.worth_row.effective.lower()}
        for parent in sorted(parents, key=lambda parent: parent.name.lower())
        if parent.slug
    ]
    selected = str((params.get("person") or [""])[0]).lower()
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
    content = _render(
        "directory.html.j2",
        handoff=handoff,
        go_back=Markup(GO_BACK_HTML),
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
