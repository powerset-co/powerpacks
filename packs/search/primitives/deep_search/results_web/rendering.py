"""Server-side HTML rendering for saved deep-search results."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable

from . import RESULTS_HTML
from .model import Candidate, CandidateGroup, Pond, SearchResult, TraitScore


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


def _pond(pond: Pond) -> str:
    diagnosis = pond.diagnosis or "final pond"
    return f"""
      <li class='pond-row'>
        <div class='pond-number'>{pond.pond_n}</div>
        <div class='pond-copy'>
          <p class='pond-query'>{_e(pond.query)}</p>
          <p class='pond-meta'>{_e(diagnosis)} <span>→</span> {_e(pond.move)}</p>
          <p class='pond-count'><strong>{pond.good_count:,}</strong> good <span>/</span> {pond.result_count:,} total</p>
        </div>
        <strong class='pond-cost'>${pond.cost_usd:.3f}</strong>
      </li>"""


def _trait_indicator(trait: TraitScore, *, mark_core: bool) -> str:
    band = "high" if trait.score >= .8 else "medium" if trait.score >= .5 else "low"
    confidence = (f"<small>{_percent(trait.confidence)} confidence</small>"
                  if trait.confidence else "")
    core = mark_core and trait.meaning == "core"
    marker = "<em>Core</em>" if core else ""
    return f"""
      <div class='trait-indicator{' trait-indicator-core' if core else ''}'>
        <span class='trait-score-column'>
          <b class='trait-score-badge trait-score-{band}'>{_percent(trait.score)}</b>
          {confidence}
        </span>
        <p>{marker}<strong>{_e(trait.name)}:</strong> {_e(trait.reason) or 'No evidence reason recorded.'}</p>
      </div>"""


def _fit(label: str, value: str) -> str:
    return f"<span class='fit-chip'><b>{_e(label)}</b>{_e(value) or 'Unknown'}</span>"


def _candidate_row(candidate: Candidate, run_id: str, *, ranking: str) -> str:
    avatar = (
        f"<img src='{_e(candidate.avatar_url)}' alt='' loading='lazy' referrerpolicy='no-referrer'>"
        if candidate.avatar_url else ""
    )
    jd_ranking = ranking == "jd"
    traits = candidate.jd_traits if jd_ranking else candidate.pond_traits
    score = candidate.jd_score if jd_ranking else candidate.pond_score
    indicators = "".join(_trait_indicator(trait, mark_core=jd_ranking) for trait in traits)
    source = f"Pond {candidate.found_pond}"
    if candidate.found_run and candidate.found_run != run_id:
        source += f" · {candidate.found_run}"
    if candidate.found_query:
        source += f" · {candidate.found_query}"
    linkedin = (
        f"<a class='linkedin-link' href='{_e(candidate.linkedin_url)}' target='_blank' rel='noreferrer'>LinkedIn ↗</a>"
        if candidate.linkedin_url else "<span class='linkedin-link muted'>No LinkedIn URL</span>"
    )
    return f"""
    <tr class='candidate-row'>
      <td class='candidate-person-cell'>
        <div class='candidate-person'>
          <span class='avatar'>{avatar}<span>{_e(_initials(candidate.name))}</span></span>
          <span class='candidate-identity'>
            <span class='candidate-name'><strong>{_e(candidate.name)}</strong><b>{_percent(score)} overall</b></span>
            <span>{_e(candidate.title) or 'Current role unknown'}</span>
            <small>{_e(candidate.company) or 'Company unknown'}{(' · ' + _e(candidate.location)) if candidate.location else ''}</small>
          </span>
        </div>
        <p class='found-note'>{_e(source)}</p>
        <div class='fit-labels'>
          {_fit('Level', candidate.level)}
          {_fit('Timing', candidate.timing)}
          {_fit('Pedigree', candidate.pedigree)}
          {_fit('Move', candidate.move)}
        </div>
        <p class='candidate-why'>{_e(candidate.why)}</p>
        <div class='candidate-actions'>{linkedin}{_feedback_button(run_id, candidate.person_id, candidate.name)}</div>
      </td>
      <td class='candidate-indicators'>
        <div class='trait-indicators'>{indicators or '<p class="no-traits">No trait scores</p>'}</div>
      </td>
    </tr>"""


def _group_rows(group: CandidateGroup, run_id: str, *, ranking: str) -> str:
    candidates = (sorted(group.candidates, key=lambda row: row.jd_score, reverse=True)
                  if ranking == "jd" else group.candidates)
    rows = "".join(_candidate_row(candidate, run_id, ranking=ranking)
                   for candidate in candidates)
    empty = "<tr><td class='empty-group' colspan='2'>No candidates in this group.</td></tr>" if not rows else ""
    return f"""
      <tbody class='result-group result-group-{_e(group.key)}'>
        <tr class='group-band'><th colspan='2'><span>{_e(group.label)}</span><b>{len(group.candidates)}</b></th></tr>
        {rows}{empty}
      </tbody>"""


def _ranking_table(search: SearchResult, ranking: str) -> str:
    groups = "".join(_group_rows(group, search.run_id, ranking=ranking)
                     for group in search.groups)
    return (f"<table class='results-table'><thead><tr><th>Candidate</th>"
            f"<th>Trait scores and reasoning</th></tr></thead>{groups}</table>")


def _search(search: SearchResult, *, opened: bool) -> str:
    candidate_count = sum(len(group.candidates) for group in search.groups)
    return f"""
    <article class='search-card'>
      {_feedback_button(search.run_id, label=search.title)}
      <details class='search-details'{' open' if opened else ''}>
        <summary class='search-summary'>
          <span class='search-identity'>
            <small>{_e(search.company) or 'Company unknown'}</small>
            <strong>{_e(search.title)}</strong>
            <span>{_date(search.created_at)} · {_e(search.run_id)}</span>
          </span>
          <span class='search-stat'><b>{candidate_count:,}</b><small>results</small></span>
          <span class='search-stat'><b>${search.total_cost_usd:.2f}</b><small>total cost</small></span>
          <span class='search-chevron' aria-hidden='true'>⌄</span>
        </summary>
        <div class='search-body' data-search-body='{_e(search.run_id)}'><p class='loading-results'>Loading results…</p></div>
      </details>
    </article>"""


def render_search_body(search: SearchResult) -> str:
    ponds = "".join(_pond(pond) for pond in search.ponds)
    pond_panel = f"pond-ranking-{_e(search.run_id)}"
    jd_panel = f"jd-ranking-{_e(search.run_id)}"
    jd_tab = ""
    jd_content = ""
    if search.has_jd_ranking:
        jd_tab = (f"<button type='button' role='tab' aria-selected='false' "
                  f"aria-controls='{jd_panel}' data-ranking-tab='jd'>"
                  f"JD Ranking <span>Beta</span></button>")
        jd_content = (f"<div id='{jd_panel}' class='ranking-panel' role='tabpanel' "
                      f"data-ranking-panel='jd' hidden>{_ranking_table(search, 'jd')}</div>")
    return (f"<section class='pond-section'><h2>Pond chain</h2><ol>{ponds}</ol></section>"
            f"<section class='groups-section'><h2>Grouped results</h2>"
            f"<div class='ranking-tabs' role='tablist' aria-label='Ranking view'>"
            f"<button type='button' role='tab' aria-selected='true' aria-controls='{pond_panel}' "
            f"data-ranking-tab='pond'>Pond Ranking</button>"
            f"{jd_tab}</div>"
            f"<div id='{pond_panel}' class='ranking-panel' role='tabpanel' data-ranking-panel='pond'>"
            f"{_ranking_table(search, 'pond')}</div>"
            f"{jd_content}</section>")


def render_page(searches: Iterable[SearchResult]) -> str:
    items = tuple(searches)
    body = "".join(_search(search, opened=index == 0) for index, search in enumerate(items))
    if not body:
        body = "<section class='empty-state'><h2>No completed searches</h2><p>No results.json with a summary block was found.</p></section>"
    total_cost = sum(search.total_cost_usd for search in items)
    template = RESULTS_HTML.read_text(encoding="utf-8")
    return (template
            .replace("{{SEARCH_COUNT}}", str(len(items)))
            .replace("{{SEARCH_LABEL}}", "search" if len(items) == 1 else "searches")
            .replace("{{TOTAL_COST}}", f"${total_cost:.2f}")
            .replace("{{CONTENT}}", body))
