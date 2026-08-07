"""Presentation-only HTML for SQLite-hydrated Deep Context rows."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.db.people_views import (
    CandidateViewRow,
    ParentViewRow,
)
from packs.ingestion.primitives.deep_context.db.worth_views import WorthRow
from packs.ingestion.primitives.deep_context.db.workflow_views import StageProgress
from packs.ingestion.primitives.deep_context.review_web.models import EnrichmentView

REVIEW_HTML = Path(__file__).with_name("reconcile_review.html")
REVIEW_CSS = Path(__file__).with_name("reconcile_review.css")
REVIEW_JS = Path(__file__).with_name("reconcile_review.js")
GO_BACK_HTML = (
    "<p class='handoff-note'>Review complete — go back to Codex.</p>"
    "<div class='handoff-copy'><code>Review complete proceed with enrichment</code>"
    "<button class='button button-outline' type='button' data-copy-continue data-phrase='Review complete proceed with enrichment' data-toast='Copied'>Copy</button></div>"
)


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _primary_candidate(parent: ParentViewRow) -> CandidateViewRow | None:
    return parent.candidates[0] if parent.candidates else None


def _avatar(parent: ParentViewRow, candidate: CandidateViewRow | None) -> str:
    name = str((candidate.full_name if candidate else "") or parent.name or "?")
    words = re.findall(r"[A-Za-z0-9]+", name)
    initials = "?" if not words else (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()
    row_key = candidate.row_key if candidate else ""
    image = ""
    if row_key and candidate and not candidate.synthetic:
        image = f"<img src='/api/avatar?pub={urllib.parse.quote(row_key)}' alt='' onerror='this.remove()'>"
    return f"<span class='avatar'><span>{esc(initials)}</span>{image}</span>"


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")


def markdown_to_html(markdown: str) -> str:
    out: list[str] = []
    bullets: list[str] = []
    for raw in _COMMENT_RE.sub("", markdown).splitlines():
        line = raw.strip()
        bullet = _BULLET_RE.match(line)
        if bullet:
            bullets.append(esc(bullet.group(1)))
            continue
        if bullets:
            out.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
            bullets.clear()
        heading = _HEADING_RE.match(line)
        if heading:
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{esc(heading.group(2))}</h{level}>")
        elif line:
            out.append(f"<p>{esc(line)}</p>")
    if bullets:
        out.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
    return "".join(out)


def _fact_list(items: tuple[Any, ...], *, visible: int = 3) -> str:
    values = [str(item) for item in items if str(item).strip()]
    shown = "".join(f"<li>{esc(item)}</li>" for item in values[:visible])
    hidden = "".join(
        f"<li hidden data-more-item>{esc(item)}</li>" for item in values[visible:]
    )
    extra = len(values) - visible
    toggle = (
        "<button type='button' class='show-more' data-show-more "
        f"data-more-label='+ show {extra} more' data-less-label='show fewer'>"
        f"+ show {extra} more</button>"
        if extra > 0
        else ""
    )
    return f"<ul class='fact-list'>{shown}{hidden}</ul>{toggle}"


def _profile(
    parent: ParentViewRow,
    candidate: CandidateViewRow | None,
    *,
    load_dossier: bool = False,
) -> str:
    name = str((candidate.full_name if candidate else "") or parent.name or "This person")
    url = "" if candidate is None or candidate.synthetic else candidate.url
    link = ""
    if url:
        link = f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer'>View LinkedIn<span aria-hidden='true'>↗</span></a>"
    contacts = " · ".join(dict.fromkeys(str(value) for value in [
        *(candidate.match_emails if candidate else ()),
        *(candidate.match_phones if candidate else ()),
    ] if value))
    rows = [
        f"<div><dt>{label}</dt><dd>{esc(value)}</dd></div>"
        for label, value in (("Contact", contacts),
                             ("Summary", candidate.headline if candidate else ""),
                             ("Location", candidate.location if candidate else ""))
        if value
    ]
    if candidate and candidate.experiences:
        rows.append(f"<div><dt>Work</dt><dd>{_fact_list(candidate.experiences)}</dd></div>")
    if candidate and candidate.education:
        rows.append(f"<div><dt>Education</dt><dd>{_fact_list(candidate.education)}</dd></div>")
    dossier = (
        "<dl class='dossier-text' aria-busy='true'></dl>"
        if load_dossier
        else ""
    )
    slug = f" data-slug='{esc(parent.slug)}'" if load_dossier else ""
    return (
        f"<div class='profile-card'>{_avatar(parent, candidate)}<div class='profile-copy'>"
        f"<h2>{esc(name)}</h2>{link}</div></div>"
        f"<section class='details'{slug}><dl>{''.join(rows)}</dl>{dossier}</section>"
    )


def _scroll_region(content: str) -> str:
    return (
        "<div class='identity-scroll-shell'><div class='identity-scroll'>"
        f"{content}</div><button class='scroll-cue' type='button' data-scroll-cue "
        "aria-label='Scroll down' hidden>&#8964;</button></div>"
    )


def _person_menu(pub: str, slug: str) -> str:
    return (
        "<div class='person-menu' data-person-menu>"
        "<button type='button' class='button button-outline person-menu-toggle' "
        "aria-label='More actions' data-menu-toggle>&#8943;</button>"
        "<div class='person-menu-items' hidden>"
        f"<button type='button' data-feedback-general data-pub='{esc(pub)}' "
        f"data-parent='{esc(slug)}'>Leave feedback</button></div></div>"
    )


def render_worth_card(parent: ParentViewRow) -> str:
    candidate = _primary_candidate(parent)
    key = parent.worth_row.key
    slug = parent.slug
    return (
        "<article class='decision-card identity-card worth-card' data-card>"
        f"{_scroll_region(_profile(parent, candidate, load_dossier=True))}"
        "<details class='worth-why'><summary>Why? Give feedback (optional)</summary>"
        "<textarea data-worth-note rows='2' maxlength='2000'></textarea></details>"
        "<div class='binary-actions'>"
        f"<button class='button button-outline' data-worth='no' data-pub='{esc(key)}' "
        f"data-parent='{esc(slug)}'>No</button>"
        f"<button class='button button-primary' data-worth='yes' data-pub='{esc(key)}' "
        f"data-parent='{esc(slug)}'>Yes</button></div></article>"
    )


def _guidance_form(candidate: CandidateViewRow | None, slug: str) -> str:
    row_key = candidate.row_key if candidate else ""
    return (
        "<details class='retarget-guidance'><summary>Wrong person? Provide LinkedIn or re-research</summary>"
        f"<form class='retarget-form' data-retarget-form data-pub='{esc(row_key)}' "
        f"data-parent='{esc(slug)}'><textarea name='guidance' maxlength='2000' required></textarea>"
        "<button class='button button-primary' type='submit'>Retarget</button>"
        "<span data-retarget-note hidden></span></form></details>"
    )


def render_linkedin_card(parent: ParentViewRow, candidates: tuple[CandidateViewRow, ...],
                         *, failure_note: str = "") -> str:
    if not candidates:
        return ""
    slug = parent.slug
    cards = "".join(
        "<li class='linkedin-option'>"
        f"{_profile(parent, candidate, load_dossier=True)}<div class='binary-actions'>"
        f"<button data-decide='detach' data-pub='{esc(candidate.row_key)}' data-parent='{esc(slug)}'>No</button>"
        f"<button data-decide='keep' data-pub='{esc(candidate.row_key)}' data-parent='{esc(slug)}'>Yes</button>"
        "</div></li>"
        for candidate in candidates
    )
    failure = (
        f"<div class='reresearch-failed'>Re-research failed: "
        f"{esc(failure_note.strip())}</div>"
        if failure_note.strip()
        else ""
    )
    primary = candidates[0]
    return (
        f"<article class='decision-card identity-card' data-card data-parent='{esc(slug)}'>"
        f"{_person_menu(primary.row_key, slug)}"
        f"{_scroll_region(f'{failure}<h2>Check LinkedIn</h2><ul class=\'linkedin-options\'>{cards}</ul>')}"
        "<div class='identity-decision'><div class='question'>Is this the right profile? "
        "Or <button type='button' class='skip-link' data-open-skip>Skip</button>?</div>"
        "<button type='button' class='button button-outline' data-open-guidance "
        "aria-expanded='false'>None of these</button>"
        f"{_guidance_form(primary, slug)}</div></article>"
    )


def _decision_row_html(parent: ParentViewRow, decision: str) -> str:
    candidate = _primary_candidate(parent)
    key = parent.worth_row.key
    slug = parent.slug
    target, label = ("no", "Move to No") if decision == "yes" else ("yes", "Move to Yes")
    return (
        "<article class='decision-row'>"
        f"{_profile(parent, candidate)}<button data-worth='{target}' data-pub='{esc(key)}' "
        f"data-parent='{esc(slug)}'>{label}</button></article>"
    )


def render_decision_table(parents: list[ParentViewRow], decision: str) -> str:
    rows = [
        parent
        for parent in parents
        if parent.worth_row.effective.lower() == decision
    ]
    rows.sort(key=lambda parent: parent.name.lower())
    return (
        f"<div class='decision-table' data-view='{esc(decision)}'>"
        f"{''.join(_decision_row_html(parent, decision) for parent in rows)}</div>"
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
    data = ""
    if pending is not None:
        data = ("<script type='application/json' data-worth-pending>"
                f"{json.dumps([asdict(row) for row in pending], ensure_ascii=False).replace('<', '\\u003c')}</script>")
    listbox = "<ul class='worth-search-list' data-search-list role='listbox' hidden></ul>" if pending is not None else ""
    return (f"<div class='worth-search' data-worth-search data-search-view='{esc(view)}'>"
            "<input class='worth-search-input' type='search' placeholder='Search people…' "
            "aria-label='Search people by name' autocomplete='off' spellcheck='false'>"
            "<span class='worth-search-count' data-search-count hidden></span>"
            f"{listbox}"
            f"{data}</div>")


def render_decision_tabs(progress: StageProgress, active: str, *, preview: bool = False) -> str:
    suffix = "&amp;preview=1" if preview else ""
    tabs = (("review", "Review", progress.worth_pending), ("yes", "Yes", progress.worth_yes),
            ("no", "No", progress.worth_no))
    return "<nav class='decision-tabs'>" + "".join(
        f"<a class='decision-tab{' active' if key == active else ''}' "
        f"data-tab='{key}' href='/?stage=worth&amp;view={key}{suffix}'>{label}<span>{count}</span></a>"
        for key, label, count in tabs
    ) + "</nav>"


def _phase_view(params: dict[str, list[str]]) -> str:
    requested = str((params.get("stage") or [""])[0]).lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(enrichment: EnrichmentView) -> str:
    status = enrichment.status or enrichment.state or "not_started"
    if status in {"running", "submitted", "research_complete"}:
        total = max(0, enrichment.counts.total)
        completed = min(total, max(0, enrichment.counts.completed))
        percent = round((completed / total) * 100) if total else 0
        progress = (
            f"<div class='enrich-progress' role='progressbar' aria-valuemin='0' "
            f"aria-valuemax='{total}' aria-valuenow='{completed}'>"
            f"<div class='enrich-progress-fill' style='width:{percent}%'></div></div>"
        )
        return _empty_state(
            "Enriching contacts",
            f"<p>{completed} of {total} complete</p>{progress}",
            extra_class="enrich-state",
        )
    if status == "needs_approval" or enrichment.state == "profile_prep_pending":
        estimate = enrichment.estimated_usd
        label = f"Approve ${estimate:.2f}" if estimate else "Start enrichment"
        return _empty_state("Ready to enrich", f"<button data-approve-enrichment>{label}</button>", extra_class="enrich-state")
    if status == "completed":
        return _empty_state("Contacts enriched", "<button data-complete='enrich'>Continue</button>", extra_class="enrich-state")
    if status in {"failed", "completed_with_errors"}:
        return _empty_state("Enrichment paused", f"<p>{esc(enrichment.error)}</p>", extra_class="enrich-state")
    return _empty_state("Preparing enrichment", extra_class="enrich-state")


def _empty_state(title: str, body: str = "", *, extra_class: str = "") -> str:
    classes = f"empty-state {extra_class}".strip()
    return f"<div class='{classes}'><h2>{title}</h2>{body}</div>"


def _step(number: int, label: str, active: bool, complete: bool, count: int = 0, href: str = "") -> str:
    state = " active" if active else (" complete" if complete and not count else "")
    marker = "✓" if complete and not count else str(number)
    content = f"<span>{marker}</span><div>{esc(label)}{'<small>'+str(count)+' left</small>' if count else ''}</div>"
    return f"<a class='step{state}' href='{esc(href)}'>{content}</a>" if href else f"<div class='step{state}'>{content}</div>"


def _carousel_nav() -> str:
    return "<button class='carousel-nav' data-carousel='prev'>&#8249;</button><button class='carousel-nav' data-carousel='next'>&#8250;</button>"


def worth_finished_body(progress: StageProgress, *, auto_continue: bool = False) -> str:
    auto = " data-auto-complete" if auto_continue else ""
    return _empty_state("Decisions ready", f"<p>{progress.lookup_ready} people will be enriched</p>"
                        f"<button data-complete='worth'{auto}>Continue</button>")


def linkedin_finished_body(progress: StageProgress, *, linkedin_complete: bool,
                           retargets_in_flight: int = 0, auto_continue: bool = False) -> str:
    auto = " data-auto-complete" if auto_continue and not linkedin_complete else ""
    tail = GO_BACK_HTML if linkedin_complete else f"<button data-complete='linkedin'{auto}>Finish</button>"
    running = f"<p>{retargets_in_flight} re-research still running</p>" if retargets_in_flight else ""
    body = f"<p>{progress.linkedin_done} decisions saved</p>{running}{tail}"
    return _empty_state("LinkedIn profiles checked", body)


def render_person_detail(parent: ParentViewRow) -> str:
    candidate = _primary_candidate(parent)
    slug = parent.slug
    dossier = markdown_to_html(parent.dossier_body)
    key = parent.worth_row.key
    effective = parent.worth_row.effective.lower()
    targets = ("no",) if effective == "yes" else (("yes",) if effective == "no" else ("yes", "no"))
    actions = "".join(
        f"<button data-dir-worth='{target}' data-pub='{esc(key)}' data-parent='{esc(slug)}'>Move to {target.title()}</button>"
        for target in targets
    )
    menu_key = candidate.row_key if candidate else key
    return (f"<article class='person-detail' data-person-slug='{esc(slug)}'>"
            f"<div class='person-detail-actions'>{actions}{_person_menu(menu_key, slug)}</div>"
            f"{_profile(parent, candidate)}{_guidance_form(candidate, slug)}"
            f"<section class='directory-dossier'>{dossier}</section></article>")


def directory_page_html(parents: list[ParentViewRow], params: dict[str, list[str]],
                        *, handoff: bool = False) -> bytes:
    entries = [
        {"slug": parent.slug, "name": parent.name,
         "worth": parent.worth_row.effective.lower()}
        for parent in sorted(parents, key=lambda parent: parent.name.lower())
        if parent.slug
    ]
    selected = str((params.get("person") or [""])[0]).lower()
    parent = next((item for item in parents if item.slug.lower() == selected), None)
    detail = render_person_detail(parent) if parent else _empty_state(f"{len(entries)} people")
    payload = json.dumps(entries, ensure_ascii=False).replace("<", "\\u003c")
    counts = {
        decision: sum(entry["worth"] == decision for entry in entries)
        for decision in ("yes", "maybe", "no")
    }
    tabs = "".join(
        f"<button type='button' class='decision-tab{' active' if decision == 'yes' else ''}' "
        f"data-directory-tab='{decision}'>{decision.title()}<span>{counts[decision]}</span></button>"
        for decision in ("yes", "maybe", "no")
        if decision != "maybe" or counts[decision]
    )
    search = (
        "<div class='worth-search' data-directory-search>"
        "<input class='worth-search-input' type='search' placeholder='Search people…' "
        "aria-label='Search people by name' autocomplete='off' spellcheck='false'>"
        "<span class='worth-search-count' data-search-count hidden></span></div>"
    )
    retargets = (
        "<section class='retarget-panel' data-retarget-panel hidden><h3>Retargeting</h3>"
        "<ul data-retarget-items></ul><div class='retarget-feedback-alert' "
        "data-feedback-alert></div></section>"
    )
    content = (
        f"{GO_BACK_HTML if handoff else ''}<div class='directory-layout' data-directory>"
        f"<aside>{search}<nav class='decision-tabs directory-tabs'>{tabs}</nav>{retargets}"
        "<nav data-directory-list></nav></aside>"
        f"<section data-directory-detail>{detail}</section></div><script type='application/json' data-directory-people>{payload}</script>"
    )
    return page_html("Directory", "directory", content)


def page_html(title: str, stage: str, content: str, *, preview: bool = False,
              external_updates: bool = False, state_token: str = "",
              enrichment_status: str = "", stepper: str = "") -> bytes:
    document = REVIEW_HTML.read_text(encoding="utf-8")
    fields = ("TITLE", "STAGE", "PREVIEW", "EXTERNAL_UPDATES", "STATE_TOKEN", "ENRICHMENT_STATUS", "STEPPER", "CONTENT")
    values = (esc(title), stage, str(preview).lower(), str(external_updates).lower(), state_token,
              enrichment_status, stepper, content)
    for field, value in zip(fields, values, strict=True):
        document = document.replace("{{" + field + "}}", value)
    return document.encode()
