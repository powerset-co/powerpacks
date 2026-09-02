"""Server-side HTML rendering for saved deep-search results."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable, Sequence

from ..fit_contract import (
    FIT_DIMENSION_NAMES, TRAIT_STATUS_NAMES, TRAIT_STATUS_VALUE, fit_label_name,
)
from . import RESULTS_HTML
from .model import (
    Candidate, Education, JdFit, JdTrait, Pond, PondCandidate, Position, SearchResult,
    TraitScore,
)
# Rows rendered immediately; the rest are hidden and revealed on scroll.
VISIBLE_ROWS = 100

FLAG_SVG = ("<svg class='flag-icon' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
            "stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
            "<path d='M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z'/>"
            "<line x1='4' x2='4' y1='22' y2='15'/></svg>")
PLUS_SVG = ("<svg class='tag-plus-icon' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
            "stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
            "<path d='M12 5v14M5 12h14'/></svg>")


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _date(value: str) -> str:
    if not value:
        return "Unknown date"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def _percent(value: float) -> str:
    return f"{round(value * 100):d}%"


def _initials(name: str) -> str:
    words = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in (words[:1] + words[-1:]))[:2] or "?"


def _feedback_button(run_id: str, person_id: str = "", label: str = "search") -> str:
    return (
        f"<button type='button' class='person-menu-toggle feedback-trigger' "
        f"data-feedback-run='{_e(run_id)}' data-feedback-person='{_e(person_id)}' "
        f"aria-label='Send feedback about {_e(label)}' title='Send feedback'>{FLAG_SVG}</button>"
    )


def _details_button(label: str) -> str:
    return (f"<button type='button' class='person-menu-toggle details-trigger' "
            f"aria-label='Show profile details for {_e(label)}' title='Profile details'>…</button>")


def _month_year(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%b %Y")
    except ValueError:
        return value


def _company_note(position: Position) -> str:
    facts = []
    if position.headcount:
        facts.append(f"{position.headcount:,} people")
    if position.stage:
        facts.append(position.stage)
    if position.funding:
        facts.append(f"${position.funding / 1e9:.1f}B raised" if position.funding >= 1e9
                     else f"${position.funding / 1e6:.0f}M raised")
    return " · ".join(facts)


_MATCHED_CHIP = "<b class='matched-chip'>Matched</b>"


def _position_item(position: Position, index: int, *, matched: bool) -> str:
    company = (f"<a href='{_e(position.company_url)}' target='_blank' rel='noreferrer'>"
               f"{_e(position.company)}</a>" if position.company_url else _e(position.company))
    note = _company_note(position)
    dates = (f"{_month_year(position.start_date)} – "
             f"{'Present' if position.is_current else _month_year(position.end_date)}")
    description = (f"<p class='position-description'>{_e(position.description)}</p>"
                   if position.description else "")
    return f"""
      <div class='position-item'>
        <div class='position-head'>
          <span class='position-title'>{_e(position.title)}{_MATCHED_CHIP if matched else ''}</span>
          <span class='position-index'>#{index}{"<b class='current-chip'>Current</b>" if position.is_current else ''}</span>
        </div>
        <p class='position-company'>{company}</p>
        {f"<p class='position-note'>{_e(note)}</p>" if note else ''}
        <p class='position-dates'>{_e(dates)}</p>
        {description}
      </div>"""


def _education_item(education: Education) -> str:
    course = " in ".join(part for part in (education.degree, education.field_of_study) if part)
    years = (f"{education.start_year} – {education.end_year}"
             if education.start_year and education.end_year else
             str(education.end_year or education.start_year or ""))
    return f"""
      <div class='education-item'>
        <div class='position-head'>
          <span class='position-title'>{_e(education.school)}</span>
          <span class='position-index'>{_e(years)}</span>
        </div>
        {f"<p class='position-company'>{_e(course)}</p>" if course else ''}
      </div>"""


def _person_details(pond_candidate: PondCandidate, run_id: str, *, feedback: bool) -> str:
    feedback_button = (f"<div class='details-feedback'><button type='button' "
                       f"class='details-feedback-button feedback-trigger' "
                       f"data-feedback-run='{_e(run_id)}' data-feedback-person='{_e(pond_candidate.person_id)}' "
                       f"aria-label='Send feedback about {_e(pond_candidate.name)}'>{FLAG_SVG} Feedback</button></div>"
                       if feedback else "")
    sources = "".join(f"<b class='source-chip'>{_e(source.capitalize())}</b>"
                      for source in pond_candidate.vertical_sources)
    sources = (f"<div class='details-section'><p class='details-label'>Sources</p>"
               f"<div class='details-chips'>{sources}</div></div>" if sources else "")
    reasoning = (f"<div class='details-reasoning'><p class='details-label'>Why they match</p>"
                 f"<p>{_e(pond_candidate.reasoning)}</p></div>"
                 if pond_candidate.reasoning else "")
    location_matched = "location" in pond_candidate.vertical_sources
    location = (f"<div class='details-section'><p class='details-label'>Location"
                f"{_MATCHED_CHIP if location_matched else ''}</p>"
                f"<p class='details-text'>{_e(pond_candidate.profile_location)}</p></div>"
                if pond_candidate.profile_location and location_matched else "")
    about = ""
    if pond_candidate.summary:
        clamp = len(pond_candidate.summary) > 200
        show_more = ("<button type='button' class='show-more'>Show more</button>"
                     if clamp else "")
        about = (f"<div class='details-section'><p class='details-label'>About"
                 f"{_MATCHED_CHIP if 'summary' in pond_candidate.vertical_sources else ''}</p>"
                 f"<p class='details-text about-text{' about-clamped' if clamp else ''}'>"
                 f"{_e(pond_candidate.summary)}</p>{show_more}</div>")
    matched = pond_candidate.matched_positions
    matched_note = (f"<span class='matched-note'>matched: [{', '.join(str(i) for i in matched)}]</span>"
                    if matched else "")
    experience = "".join(_position_item(position, index, matched=index in matched)
                         for index, position in enumerate(pond_candidate.positions))
    experience = (f"<div class='details-section'><p class='details-label'>Work Experience"
                  f"{matched_note}</p><div class='details-list'>{experience}</div></div>"
                  if experience else "")
    education = "".join(_education_item(entry) for entry in pond_candidate.education)
    education = (f"<div class='details-section'><p class='details-label'>Education</p>"
                 f"<div class='details-list'>{education}</div></div>" if education else "")
    if not (reasoning or about or experience or education):
        return ""
    return (f"<div class='person-details' hidden><div class='details-scroll'>"
            f"{feedback_button}{sources}{reasoning}{location}{about}{experience}{education}</div></div>")


def _pond(pond: Pond, panel_id: str, *, selected: bool) -> str:
    diagnosis = pond.diagnosis or "final pond"
    count = (f"<strong>{pond.reviewed_count:,}</strong> annotated "
             f"<span>·</span> {pond.result_count:,} retrieved")
    return f"""
      <li>
        <button type='button' class='pond-row' role='tab' aria-selected='{'true' if selected else 'false'}'
                aria-controls='{panel_id}' data-pond-tab='{pond.run_id}:{pond.pond_n}'>
          <span class='pond-number'>{pond.pond_n}</span>
          <span class='pond-copy'>
            <span class='pond-query'>{_e(pond.query)}<i class='query-copy' title='Copy query' data-copy-query='{_e(pond.query)}'>⧉</i></span>
            <span class='pond-meta'>{_e(diagnosis)} <span>→</span> {_e(pond.move)}</span>
            <span class='pond-count'>{count}</span>
          </span>
        </button>
      </li>"""


def _score_band(score: float) -> str:
    return "high" if score >= .8 else "medium" if score >= .5 else "low"


def _trait_indicator(trait: TraitScore, *, mark_core: bool) -> str:
    core = mark_core and trait.meaning == "core"
    marker = "<em>Core</em>" if core else ""
    return f"""
      <div class='trait-indicator{' trait-indicator-core' if core else ''}'>
        <b class='trait-score-badge trait-score-{_score_band(trait.score)}'>{_percent(trait.score)}</b>
        <p>{marker}<strong>{_e(trait.name)}:</strong> {_e(trait.reason) or 'No evidence reason recorded.'}</p>
      </div>"""


def _jd_trait_indicator(trait: JdTrait) -> str:
    value = TRAIT_STATUS_VALUE[trait.status]
    return f"""
      <div class='trait-indicator jd-trait' tabindex='0'>
        <b class='trait-score-badge trait-score-{_score_band(value)}'>{_percent(value)}</b>
        <p><strong>{_e(trait.trait)}:</strong> {_e(TRAIT_STATUS_NAMES[trait.status])}<span class='badge-note' role='tooltip'>{_e(trait.evidence) or 'No evidence recorded.'}</span></p>
      </div>"""


def _jd_fit_list(fit: JdFit) -> str:
    """The panel's per-trait ladder, shaped like the trait list above it."""
    rows = "".join(_jd_trait_indicator(trait) for trait in fit.traits)
    return (f"<div class='jd-fit-list'><p class='jd-fit-label'>"
            f"<span class='badges-label'>JD traits (beta)</span>"
            f"<b class='jd-fit-chip'>JD fit {_percent(fit.coverage)}</b></p>"
            f"<div class='trait-indicators'>{rows}</div></div>")


def _badge(text: str, note: str) -> str:
    return (f"<span class='badge' tabindex='0'>{_e(text)}"
            f"<span class='badge-note' role='tooltip'>{_e(note)}</span></span>")


def _badges(candidate: Candidate) -> str:
    pills = [_badge(
        f"{FIT_DIMENSION_NAMES[expert.dimension]} · "
        f"{fit_label_name(expert.dimension, expert.label)}",
        expert.why or "No reasoning recorded.",
    ) for expert in candidate.fit_experts]
    return f"<div class='candidate-badges'>{''.join(pills)}</div>" if pills else ""


def _candidate_row(pond_candidate: PondCandidate, run_id: str,
                   graded: Candidate | None, *, lazy: bool = False) -> str:
    avatar = (
        f"<img src='{_e(pond_candidate.avatar_url)}' alt='' loading='lazy' referrerpolicy='no-referrer'>"
        if pond_candidate.avatar_url else ""
    )
    indicators = "".join(_trait_indicator(trait, mark_core=False)
                         for trait in pond_candidate.traits)
    name = _e(pond_candidate.name)
    name = (f"<a class='candidate-profile-link' href='{_e(pond_candidate.linkedin_url)}' "
            f"target='_blank' rel='noreferrer' title='Open {_e(pond_candidate.name)} on LinkedIn'>"
            f"{name}<svg class='linkedin-icon' viewBox='0 0 24 24' aria-label='LinkedIn'>"
            f"<path d='M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z'/></svg></a>"
            if pond_candidate.linkedin_url else
            f"<strong>{name}</strong>")
    badges = _badges(graded) if graded else ""
    jd_traits = _jd_fit_list(graded.jd_fit) if graded and graded.jd_fit else ""
    return f"""
    <tr class='candidate-row' data-person-id='{_e(pond_candidate.person_id)}'
        data-person-name='{_e(pond_candidate.name)}'
        data-person-linkedin='{_e(pond_candidate.linkedin_url)}'
        data-person-title='{_e(pond_candidate.title)}'
        data-person-company='{_e(pond_candidate.company)}'
        data-person-location='{_e(pond_candidate.location)}'
        data-person-source='{_e(pond_candidate.source_channel)}'
        data-person-network='{_e(pond_candidate.source_operator)}'
        data-person-reasoning='{_e(pond_candidate.reasoning)}'
        data-person-score='{pond_candidate.final_score}'{' hidden data-lazy' if lazy else ''}>
      <td class='candidate-person-cell'>
        <button type='button' class='tag-trigger' data-tag-person='{_e(pond_candidate.person_id)}'
                aria-label='Add tag to {_e(pond_candidate.name)}' title='Add tag'>
          <span class='person-tags' data-person-tags></span>{PLUS_SVG}
        </button>
        <div class='candidate-person'>
          <span class='avatar'>{avatar}<span>{_e(_initials(pond_candidate.name))}</span></span>
          <span class='candidate-identity'>
            <span class='candidate-name'>{name}</span>
            <span>{_e(pond_candidate.title) or 'Current role unknown'}</span>
            <small>{_e(pond_candidate.company) or 'Company unknown'}{(' · ' + _e(pond_candidate.location)) if pond_candidate.location else ''}</small>
          </span>
        </div>
      </td>
      <td class='candidate-indicators'>
        <span class='person-actions'>{_details_button(pond_candidate.name)}</span>
        <div class='trait-indicators'>{indicators or '<p class="no-traits">No trait scores</p>'}</div>
        {jd_traits}
        {badges}
        {_person_details(pond_candidate, run_id, feedback=graded is not None)}
      </td>
    </tr>"""


def _results_table(body: Sequence[str]) -> str:
    sentinel = ("<tr class='lazy-sentinel'><td colspan='2'></td></tr>"
                if len(body) > VISIBLE_ROWS else "")
    return (f"<table class='results-table' data-results-table><thead><tr><th>Candidate</th>"
            f"<th>Trait scores and reasoning</th></tr></thead>"
            f"<tbody>{''.join(body)}{sentinel}</tbody></table>")


def _pond_table(search: SearchResult, pond: Pond) -> str:
    if not pond.candidates and not pond.reviewed_count:
        return (f"<p class='empty-pond'>0 of {pond.result_count:,} retrieved candidates scored "
                f"\u2265 0.7 \u2014 nothing cleared the review threshold in this pond.</p>")
    rows = sorted(pond.candidates, key=lambda row: row.final_score, reverse=True)
    body = []
    for index, row in enumerate(rows):
        graded = search.candidate(row.person_id)
        body.append(_candidate_row(row, search.run_id, graded, lazy=index >= VISIBLE_ROWS))
    toolbar = (f"<div class='results-toolbar' data-results-toolbar data-tag-filter='all'>"
               f"<span class='result-filters'>"
               f"<button type='button' class='result-filter selected' data-result-filter='all' "
               f"aria-pressed='true'>All results ({len(rows):,})</button>"
               f"<button type='button' class='result-filter' data-result-filter='tagged' "
               f"aria-pressed='false' hidden>Tagged (<span data-tagged-count>0</span>)</button>"
               f"</span><span class='tag-filters' data-tag-filters hidden></span>"
               f"<span class='result-actions'>"
               f"<button type='button' data-untag-all hidden>Untag all on page</button>"
               f"<button type='button' data-copy-results hidden>Copy</button>"
               f"<button type='button' data-export-csv hidden>CSV</button>"
               f"<button type='button' data-clear-tags hidden>Clear all</button>"
               f"<span class='clear-tags-confirm' data-clear-tags-confirm hidden>Clear all? "
               f"<button type='button' data-confirm-clear-tags>Confirm</button>"
               f"<button type='button' data-cancel-clear-tags>Cancel</button></span>"
               f"</span></div>")
    return toolbar + _results_table(body)


def _jd_fit_table(search: SearchResult) -> str:
    """The beta panel: every graded candidate in `summary.jd_fit_order`, same row renderer."""
    body = []
    for person_id in search.jd_fit_order:
        graded = search.candidate(person_id)
        pond_row = graded.in_pond(graded.found_run, graded.found_pond)
        if pond_row is None:
            continue
        body.append(_candidate_row(pond_row, search.run_id, graded, lazy=len(body) >= VISIBLE_ROWS))
    if not body:
        return "<p class='empty-pond'>No JD fit annotations in this run.</p>"
    return _results_table(body)


def _search(search: SearchResult) -> str:
    jd = (f"<details class='jd-details'><summary>Job description</summary>"
          f"<div class='jd-content'>{_e(search.jd_text)}</div></details>"
          if search.jd_text else "")
    return f"""
    <article class='search-card'>
      {_feedback_button(search.run_id, label=search.title)}
      <header class='search-summary'>
        <span class='search-identity'>
          <small>{_e(search.company) or 'Company unknown'}</small>
          <strong>{_e(search.title)}</strong>
          <span>{_date(search.created_at)} · {_e(search.run_id)}</span>
        </span>
      </header>
      {jd}
      <div class='search-body' data-search-body='{_e(search.run_id)}' data-search-title='{_e(search.title)}'><p class='loading-results'>Loading results…</p></div>
    </article>"""


def render_search_body(search: SearchResult) -> str:
    tabs = []
    panels = []
    for index, pond in enumerate(search.ponds):
        panel_id = f"pond-results-{_e(search.run_id)}-{pond.pond_n}"
        tabs.append(_pond(pond, panel_id, selected=index == 0))
        panels.append(
            f"<div id='{panel_id}' class='pond-panel' role='tabpanel' "
            f"data-pond-panel='{_e(pond.run_id)}:{pond.pond_n}'{' hidden' if index else ''}>"
            f"{_pond_table(search, pond)}</div>")
    return (f"<section class='pond-section'><h2>Search chain</h2>"
            f"<ol role='tablist' aria-label='Pond results'>{''.join(tabs)}</ol></section>"
            f"<section class='groups-section'>"
            f"<div class='view-tabs' role='tablist' aria-label='Result views'>"
            f"<button type='button' class='view-tab' role='tab' aria-selected='true' "
            f"data-view-tab='main'>Results from selected search</button>"
            f"<button type='button' class='view-tab' role='tab' aria-selected='false' "
            f"data-view-tab='jd-fit'>JD fit (beta)</button></div>"
            f"<div data-view-panel='main' role='tabpanel'>{''.join(panels)}</div>"
            f"<div data-view-panel='jd-fit' role='tabpanel' hidden>{_jd_fit_table(search)}</div>"
            f"</section>")


def render_page(searches: Iterable[SearchResult]) -> str:
    items = tuple(searches)
    body = "".join(_search(search) for search in items)
    if not body:
        body = "<section class='empty-state'><h2>No completed searches</h2><p>No results.json with a summary block was found.</p></section>"
    template = RESULTS_HTML.read_text(encoding="utf-8")
    return template.replace("{{CONTENT}}", body)
