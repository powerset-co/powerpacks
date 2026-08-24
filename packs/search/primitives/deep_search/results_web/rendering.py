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


def _histogram(pond: Pond) -> str:
    total = sum(count for _, count in pond.histogram) or 1
    bars = "".join(
        f"<span class='hist-band band-{index}' style='--share:{count / total:.4f}' "
        f"title='{_e(band)}: {count}'><i></i><small>{_e(band)} · {count}</small></span>"
        for index, (band, count) in enumerate(pond.histogram)
    )
    return f"<div class='score-histogram' aria-label='Score histogram'>{bars}</div>"


def _pond(pond: Pond) -> str:
    diagnosis = pond.diagnosis or "final pond"
    run_note = f" · {_e(pond.run_id)}" if pond.run_id else ""
    return f"""
      <li class='pond-row'>
        <div class='pond-number'>{pond.pond_n}</div>
        <div class='pond-copy'>
          <p class='pond-query'>{_e(pond.query)}</p>
          <p class='pond-meta'>{_e(diagnosis)} <span>→</span> {_e(pond.move)} · {pond.result_count:,} results{run_note}</p>
          {_histogram(pond)}
        </div>
        <strong class='pond-cost'>${pond.cost_usd:.3f}</strong>
      </li>"""


def _trait_chip(trait: TraitScore) -> str:
    return (
        f"<span class='trait-chip score-{round(trait.score * 10)}' "
        f"title='{_e(trait.name)}'><span>{_e(trait.name)}</span><b>{_percent(trait.score)}</b></span>"
    )


def _trait_evidence(trait: TraitScore) -> str:
    confidence = (f" · confidence {_percent(trait.confidence)}" if trait.confidence else "")
    return f"""
      <li>
        <div><strong>{_e(trait.name)}</strong><span>{_percent(trait.score)}{confidence}</span></div>
        <p>{_e(trait.reason) or 'No evidence reason recorded.'}</p>
      </li>"""


def _fit(label: str, value: str) -> str:
    return f"<span class='fit-chip'><b>{_e(label)}</b>{_e(value) or 'Unknown'}</span>"


def _candidate(candidate: Candidate, run_id: str) -> str:
    avatar = (
        f"<img src='{_e(candidate.avatar_url)}' alt='' loading='lazy' referrerpolicy='no-referrer'>"
        if candidate.avatar_url else ""
    )
    traits = "".join(_trait_chip(trait) for trait in candidate.traits)
    evidence = "".join(_trait_evidence(trait) for trait in candidate.traits)
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
    <details class='candidate-card'>
      <summary class='candidate-summary'>
        <span class='candidate-person'>
          <span class='avatar'>{avatar}<span>{_e(_initials(candidate.name))}</span></span>
          <span class='candidate-identity'>
            <strong>{_e(candidate.name)}</strong>
            <span>{_e(candidate.title) or 'Current role unknown'}</span>
            <small>{_e(candidate.company) or 'Company unknown'}{(' · ' + _e(candidate.location)) if candidate.location else ''}</small>
          </span>
        </span>
        <span class='overall-score'><b>{_percent(candidate.score)}</b><small>overall</small></span>
        <span class='trait-strip' aria-label='Trait scores'>{traits or '<span class="no-traits">No trait scores</span>'}</span>
        <span class='candidate-chevron' aria-hidden='true'>⌄</span>
      </summary>
      <div class='candidate-detail'>
        <p class='found-note'>{_e(source)}</p>
        <ul class='trait-evidence'>{evidence or '<li><p>No pond trait evidence was found.</p></li>'}</ul>
        <div class='fit-labels'>
          {_fit('Level', candidate.level)}
          {_fit('Timing', candidate.timing)}
          {_fit('Pedigree', candidate.pedigree)}
          {_fit('Move', candidate.move)}
        </div>
        <p class='candidate-why'>{_e(candidate.why)}</p>
        <div class='candidate-actions'>{linkedin}{_feedback_button(run_id, candidate.person_id, candidate.name)}</div>
      </div>
    </details>"""


def _group(group: CandidateGroup, run_id: str) -> str:
    rows = "".join(_candidate(candidate, run_id) for candidate in group.candidates)
    open_attr = " open" if group.key in {"send_worthy", "chat_worthy"} else ""
    empty = "<p class='empty-group'>No candidates in this group.</p>" if not rows else ""
    return f"""
      <details class='result-group'{open_attr}>
        <summary><span>{_e(group.label)}</span><b>{len(group.candidates)}</b></summary>
        <div class='candidate-list'>{rows}{empty}</div>
      </details>"""


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
    groups = "".join(_group(group, search.run_id) for group in search.groups)
    return (f"<section class='pond-section'><h2>Pond chain</h2><ol>{ponds}</ol></section>"
            f"<section class='groups-section'><h2>Grouped results</h2>{groups}</section>")


def render_page(searches: Iterable[SearchResult]) -> str:
    items = tuple(searches)
    body = "".join(_search(search, opened=index == 0) for index, search in enumerate(items))
    if not body:
        body = "<section class='empty-state'><h2>No completed searches</h2><p>No results.json with a summary block was found.</p></section>"
    total_cost = sum(search.total_cost_usd for search in items)
    template = RESULTS_HTML.read_text(encoding="utf-8")
    return (template
            .replace("{{SEARCH_COUNT}}", str(len(items)))
            .replace("{{TOTAL_COST}}", f"${total_cost:.2f}")
            .replace("{{CONTENT}}", body))
