"""Server-side HTML rendering for saved deep-search results."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable

from . import RESULTS_HTML
from .model import Candidate, CandidateGroup, Pond, PondCandidate, SearchResult, TraitScore


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
        f"aria-label='Send feedback about {_e(label)}' title='Send feedback'>…</button>"
    )


def _pond(pond: Pond, panel_id: str, *, selected: bool) -> str:
    diagnosis = pond.diagnosis or "final pond"
    return f"""
      <li>
        <button type='button' class='pond-row' role='tab' aria-selected='{'true' if selected else 'false'}'
                aria-controls='{panel_id}' data-pond-tab='{pond.run_id}:{pond.pond_n}'>
          <span class='pond-number'>{pond.pond_n}</span>
          <span class='pond-copy'>
            <span class='pond-query'>{_e(pond.query)}</span>
            <span class='pond-meta'>{_e(diagnosis)} <span>→</span> {_e(pond.move)}</span>
            <span class='pond-count'><strong>{pond.reviewed_count:,}</strong> scored ≥ 0.7 <span>/</span> {pond.result_count:,} retrieved</span>
          </span>
        </button>
      </li>"""


def _trait_indicator(trait: TraitScore, *, mark_core: bool) -> str:
    band = "high" if trait.score >= .8 else "medium" if trait.score >= .5 else "low"
    core = mark_core and trait.meaning == "core"
    marker = "<em>Core</em>" if core else ""
    return f"""
      <div class='trait-indicator{' trait-indicator-core' if core else ''}'>
        <b class='trait-score-badge trait-score-{band}'>{_percent(trait.score)}</b>
        <p>{marker}<strong>{_e(trait.name)}:</strong> {_e(trait.reason) or 'No evidence reason recorded.'}</p>
      </div>"""


def _candidate_row(candidate: Candidate, pond_candidate: PondCandidate, run_id: str) -> str:
    avatar = (
        f"<img src='{_e(pond_candidate.avatar_url)}' alt='' loading='lazy' referrerpolicy='no-referrer'>"
        if pond_candidate.avatar_url else ""
    )
    indicators = "".join(_trait_indicator(trait, mark_core=False)
                         for trait in pond_candidate.traits)
    name = _e(candidate.name)
    name = (f"<a class='candidate-profile-link' href='{_e(candidate.linkedin_url)}' "
            f"target='_blank' rel='noreferrer' title='Open {_e(candidate.name)} on LinkedIn'>"
            f"{name}<svg class='linkedin-icon' viewBox='0 0 24 24' aria-label='LinkedIn'>"
            f"<path d='M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z'/></svg></a>"
            if candidate.linkedin_url else
            f"<strong>{name}</strong>")
    fit_reason = (f"<p class='candidate-fit-reason'>{_e(candidate.why)}</p>"
                  if candidate.why else "")
    return f"""
    <tr class='candidate-row'>
      <td class='candidate-person-cell'>
        <div class='candidate-person'>
          <span class='avatar'>{avatar}<span>{_e(_initials(candidate.name))}</span></span>
          <span class='candidate-identity'>
            <span class='candidate-name'>{name}</span>
            <span>{_e(pond_candidate.title) or 'Current role unknown'}</span>
            <small>{_e(pond_candidate.company) or 'Company unknown'}{(' · ' + _e(pond_candidate.location)) if pond_candidate.location else ''}</small>
          </span>
        </div>
      </td>
      <td class='candidate-indicators'>
        {_feedback_button(run_id, candidate.person_id, candidate.name)}
        <div class='trait-indicators'>{indicators or '<p class="no-traits">No trait scores</p>'}</div>
        {fit_reason}
      </td>
    </tr>"""


def _group_rows(group: CandidateGroup, run_id: str, pond: Pond) -> str:
    candidates = [(candidate, candidate.in_pond(pond.run_id, pond.pond_n))
                  for candidate in group.candidates]
    candidates = sorted(((candidate, row) for candidate, row in candidates if row),
                        key=lambda item: item[1].final_score, reverse=True)
    rows = "".join(_candidate_row(candidate, row, run_id) for candidate, row in candidates)
    empty = "<tr><td class='empty-group' colspan='2'>No candidates in this group.</td></tr>" if not rows else ""
    collapsed = " group-collapsed" if group.key in ("wrong_timing_relationship", "passed") else ""
    return f"""
      <tbody class='result-group result-group-{_e(group.key)}{collapsed}'>
        <tr class='group-band'><th colspan='2'><button type='button' class='group-toggle'><span>{_e(group.label)}</span><b>{len(candidates)}</b><i class='group-chevron' aria-hidden='true'>⌄</i></button></th></tr>
        {rows}{empty}
      </tbody>"""


def _pond_table(search: SearchResult, pond: Pond) -> str:
    if not pond.reviewed_count:
        return (f"<p class='empty-pond'>0 of {pond.result_count:,} retrieved candidates scored "
                f"\u2265 0.7 \u2014 nothing cleared the review threshold in this pond.</p>")
    groups = "".join(_group_rows(group, search.run_id, pond)
                     for group in search.groups)
    return (f"<table class='results-table'><thead><tr><th>Candidate</th>"
            f"<th>Trait scores and reasoning</th></tr></thead>{groups}</table>")


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
      <div class='search-body' data-search-body='{_e(search.run_id)}'><p class='loading-results'>Loading results…</p></div>
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
            f"<section class='groups-section'><h2>Results from selected search</h2>"
            f"{''.join(panels)}</section>")


def render_page(searches: Iterable[SearchResult]) -> str:
    items = tuple(searches)
    body = "".join(_search(search) for search in items)
    if not body:
        body = "<section class='empty-state'><h2>No completed searches</h2><p>No results.json with a summary block was found.</p></section>"
    template = RESULTS_HTML.read_text(encoding="utf-8")
    return template.replace("{{CONTENT}}", body)
