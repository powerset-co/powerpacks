"""Server-side HTML rendering for saved deep-search results."""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Iterable, Sequence

from . import RESULTS_HTML
from packs.search.primitives.shared.human_ratings import LEGACY_SCORES, RUBRIC
from .model import (
    Candidate, Education, PersonAttribution, Pond, PondCandidate, Position, SearchResult,
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
PIN_SVG = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' "
           "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
           "<path d='M16 9V4l1-1V2H7v1l1 1v5l-3 3v2h14v-2zM12 14v8'/></svg>")
_SOURCE_LABELS = {'gmail': 'Email', 'email': 'Email', 'messages': 'Messages',
                  'imessage': 'iMessage', 'whatsapp': 'WhatsApp', 'phone': 'Phone',
                  'linkedin': 'LinkedIn', 'linkedin_connections': 'Connections',
                  'csv_import': 'Contacts Export', 'x': 'X', 'twitter': 'X'}
_MESSAGE_CHANNELS = {'imessage', 'whatsapp', 'phone', 'messages'}
_SOURCE_ICONS = {
    'gmail': '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    'messages': '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    'linkedin': '<path d="M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z"/>',
    'x': '<path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>',
    'csv_import': '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7ZM14 2v4a2 2 0 0 0 2 2h4M8 13h2M14 13h2M8 17h2M14 17h2"/>',
    'linkedin_connections': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
}


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
    return "".join(part[0].upper() for part in (words[:1] + (words[-1:] if len(words) > 1 else []))) or "?"


def _network_popover(trigger: str, label: str, content: str, attributes: str = '') -> str:
    return (f"<span class='network-attribution'><button type='button' class='network-trigger' "
            f"data-network-trigger {attributes} aria-expanded='false' aria-label='{_e(label)}'>{trigger}</button>"
            f"<span class='network-popover' popover='auto' role='region' aria-label='{_e(label)}'>"
            f"{content}</span></span>")


def _network_sources(attribution: PersonAttribution | None, name: str) -> str:
    """App compact source badges and separate operator initials popover."""
    if attribution is None or not (attribution.sources or attribution.operators):
        return ''
    channels: dict[str, int] = {}
    for source in attribution.sources:
        channel = 'messages' if source.channel in _MESSAGE_CHANNELS else source.channel
        channel = 'x' if channel == 'twitter' else channel
        channels[channel] = channels.get(channel, 0) + source.total_interactions
    segments = []
    for channel, count in sorted(channels.items(), key=lambda item: -item[1]):
        if channel not in _SOURCE_ICONS:
            continue
        icon = _SOURCE_ICONS[channel]
        label = _SOURCE_LABELS.get(channel, channel)
        number = ''
        if count > 0 and channel in {'gmail', 'messages'}:
            number = f'~{int(count / 1000 + .5)}k' if count >= 1000 else str(count)
        badge = (f"<span class='network-source' data-channel='{channel}'><svg viewBox='0 0 24 24' "
                 f"fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' "
                 f"stroke-linejoin='round' aria-hidden='true'>{icon}</svg>{number}</span>")
        source_channels = (_MESSAGE_CHANNELS if channel == 'messages' else
                           {'x', 'twitter'} if channel == 'x' else {channel})
        operators = ''.join(
            f"<li class='network-operator'><span class='operator-initials'>{_e(_initials(op.operator_name))}</span>"
            f"<span><strong>{_e(op.operator_name)}</strong>"
            f"{f'<small>{op.gmail_interactions:,} emails</small>' if channel == 'gmail' and op.gmail_interactions else ''}</span></li>"
            for op in attribution.operators if source_channels.intersection(op.channels))
        breakdown = ''.join(f"<li><span>{_e(_SOURCE_LABELS.get(s.channel, s.channel))}</span>"
                            f"<span>{s.total_interactions:,}</span></li>"
                            for s in attribution.sources if s.channel in source_channels)
        content = (f"<strong>{_e(label)}</strong><small class='network-total'>{count:,} interactions</small>"
                   f"<ul class='network-counts'>{breakdown}</ul>"
                   f"<strong class='network-heading'>Connected via</strong><ul>{operators}</ul>")
        segments.append(_network_popover(badge, f'{label} sources for {name}', content,
                                         f'data-source="{channel}"'))
    initials = ''.join(f"<span class='operator-initials'>{_e(_initials(op.operator_name))}</span>"
                       for op in attribution.operators[:3])
    if len(attribution.operators) > 3:
        initials += f"<span class='operator-initials'>+{len(attribution.operators) - 3}</span>"
    operators = []
    for op in attribution.operators:
        labels = dict.fromkeys(_SOURCE_LABELS.get(channel, channel) for channel in op.channels)
        email = f' · {op.gmail_interactions:,} emails' if op.gmail_interactions else ''
        operators.append(f"<li class='network-operator'><span class='operator-initials'>{_e(_initials(op.operator_name))}</span>"
                         f"<span><strong>{_e(op.operator_name)}</strong><small>{_e(' · '.join(labels))}{email}</small></span></li>")
    operator_popover = (_network_popover(f"<span class='operator-stack'>{initials}</span>",
                         f'Source operators for {name}',
                         f"<strong class='network-heading'>Connected via</strong><ul>{''.join(operators)}</ul>",
                         'data-network-operators') if attribution.operators else '')
    return (f"<span class='network-context'><span class='network-sources'>{''.join(segments)}</span>"
            f"{operator_popover}</span>")


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


def _person_details(pond_candidate: PondCandidate) -> str:
    sources = "".join(f"<b class='source-chip'>{_e(source.capitalize())}</b>"
                      for source in pond_candidate.vertical_sources)
    sources = (f"<div class='details-section'><p class='details-label'>Sources</p>"
               f"<div class='details-chips'>{sources}</div></div>" if sources else "")
    reasoning = (f"<div class='details-reasoning'><p class='details-label'>Why they match</p>"
                 f"<p>{_e(pond_candidate.reasoning)}</p></div>"
                 if pond_candidate.reasoning else "")
    traits = "".join(_trait_indicator(trait, mark_core=False) for trait in pond_candidate.traits)
    reasoning += f"<div class='trait-indicators'>{traits}</div>" if traits else ""
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
            f"{sources}{reasoning}{location}{about}{experience}{education}</div></div>")


def _pond(pond: Pond, panel_id: str, *, selected: bool) -> str:
    diagnosis = pond.diagnosis or "final pond"
    count = (f"<strong>{pond.reviewed_count:,}</strong> annotated "
             f"<span>·</span> {pond.result_count:,} retrieved" if pond.reviewed_count else
             f"<strong>{len(pond.candidates):,}</strong> results "
             f"<span>·</span> {pond.result_count:,} retrieved")
    tag = "button" if panel_id else "div"
    control = (f"type='button' role='tab' aria-selected='{str(selected).lower()}' "
               f"aria-controls='{panel_id}' data-pond-tab='{_e(pond.run_id)}:{pond.pond_n}'"
               if panel_id else "")
    return f"""
      <li>
        <{tag} class='pond-row' {control}>
          <span class='pond-number'>{pond.pond_n}</span>
          <span class='pond-copy'>
            <span class='pond-query'>{_e(pond.query)}<i class='query-copy' title='Copy query' data-copy-query='{_e(pond.query)}'>⧉</i></span>
            <span class='pond-meta'>{_e(diagnosis)} <span>→</span> {_e(pond.move)}</span>
            <span class='pond-count'>{count}</span>
          </span>
        </{tag}>
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


def _overall_score(row: PondCandidate, candidate: Candidate | None) -> int | None:
    judgment = candidate.candidate_judgment if candidate else None
    if judgment and judgment.overall_score is not None:
        return judgment.overall_score
    ce = row.cross_encoder_score_1_to_5
    if ce is not None and ce < 3:
        return 2 if ce >= 2 else 1
    return None


def _candidate_row(pond_candidate: PondCandidate, run_id: str,
                   graded: Candidate | None, *, lazy: bool = False,
                   cross_encoder: bool = False, readonly: bool = False) -> str:
    """Main results show traits; the beta view shows the combined judge result."""
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
    overall = None
    reason = pond_candidate.reasoning
    if cross_encoder:
        judgment = graded.candidate_judgment if graded else None
        overall = _overall_score(pond_candidate, graded)
        if judgment and judgment.overall_score is not None:
            reason = (judgment.opportunity_reason
                      if judgment.opportunity_cap < judgment.domain_score else judgment.domain_reason)
        else:
            reason = "Did not pass screen" if overall is not None else "Not judged"
        if overall is not None:
            indicators = (
                f"<div class='trait-indicator'><b class='trait-score-badge "
                f"trait-score-{_score_band(overall / 5)}'>{overall}/5</b>"
                f"<p>{_e(reason)}</p></div>")
        else:
            indicators = '<p class="no-traits">Not judged</p>'
    score = graded.human_score if graded else None
    note = "" if readonly else f"data-feedback-note='{_e(graded.human_note if graded else '')}' "
    score_label = f'{"Saved" if readonly else "Your"} score: {score}/5' if score is not None else "Score"
    score_button = (
        f"<button type='button' class='score-trigger' "
        f"data-feedback-run='{_e(run_id)}' data-feedback-person='{_e(pond_candidate.person_id)}' "
        f"data-feedback-score='{score if score is not None else ''}' "
        f"{note}"
        f"aria-label='Score {_e(pond_candidate.name)}'>"
        f"{score_label}</button>")
    return f"""
    <tr class='candidate-row' data-person-id='{_e(pond_candidate.person_id)}'
        data-person-name='{_e(pond_candidate.name)}'
        data-person-linkedin='{_e(pond_candidate.linkedin_url)}'
        data-person-title='{_e(pond_candidate.title)}'
        data-person-company='{_e(pond_candidate.company)}'
        data-person-location='{_e(pond_candidate.location)}'
        data-person-source='{_e(pond_candidate.source_channel)}'
        data-person-network='{_e(pond_candidate.source_operator)}'
        data-person-reasoning='{_e(reason)}'
        data-person-overall='{overall if overall is not None else ''}'
        data-person-score='{pond_candidate.cross_encoder_score_1_to_5 if cross_encoder else pond_candidate.final_score}'{' hidden data-lazy' if lazy else ''}>
      <td class='candidate-person-cell'>
        <div class='candidate-tags'><button type='button' class='tag-trigger' data-tag-person='{_e(pond_candidate.person_id)}'
                aria-label='Add tag to {_e(pond_candidate.name)}' title='Add tag'>
          <span class='person-tags' data-person-tags></span>{PLUS_SVG}
        </button><button type='button' class='pin-trigger' data-pin-person='{_e(pond_candidate.person_id)}'
          aria-label='Pin {_e(pond_candidate.name)}' aria-pressed='false' title='Pin to shortlist'>{PIN_SVG}</button></div>
        <div class='candidate-person'>
          <span class='avatar'>{avatar}<span>{_e(_initials(pond_candidate.name))}</span></span>
          <span class='candidate-identity'>
            <span class='candidate-name'>{name}</span>
            <span>{_e(pond_candidate.title) or 'Current role unknown'}</span>
            <small>{_e(pond_candidate.company) or 'Company unknown'}{(' · ' + _e(pond_candidate.location)) if pond_candidate.location else ''}</small>
            {_network_sources(graded.network_attribution if graded else None, pond_candidate.name)}
          </span>
        </div>
      </td>
      <td class='candidate-indicators'>
        <span class='person-actions'>{score_button}{_details_button(pond_candidate.name)}</span>
        <div class='trait-indicators'>{indicators or '<p class="no-traits">No trait scores</p>'}</div>
        {_person_details(pond_candidate)}
      </td>
    </tr>"""


def _results_table(body: Sequence[str], *, heading: str = "Trait scores and reasoning") -> str:
    sentinel = ("<tr class='lazy-sentinel'><td colspan='2'></td></tr>"
                if len(body) > VISIBLE_ROWS else "")
    return (f"<table class='results-table' data-results-table><thead><tr><th>Candidate</th>"
            f"<th>{_e(heading)}</th></tr></thead>"
            f"<tbody>{''.join(body)}{sentinel}</tbody></table>")


def _pond_table(search: SearchResult, pond: Pond, *, readonly: bool = False) -> str:
    if not pond.candidates and not pond.reviewed_count:
        return (f"<p class='empty-pond'>0 of {pond.result_count:,} retrieved candidates scored "
                f"\u2265 0.7 \u2014 nothing cleared the review threshold in this pond.</p>")
    rows = sorted(pond.candidates, key=lambda row: row.final_score, reverse=True)
    body = []
    for index, row in enumerate(rows):
        graded = search.candidate(row.person_id)
        body.append(_candidate_row(row, search.run_id, graded, lazy=index >= VISIBLE_ROWS, readonly=readonly))
    return _results_toolbar(len(rows)) + _results_table(body)


def _results_toolbar(count: int, *, scored: bool = False) -> str:
    scores = ("<span class='score-filters' role='group' aria-label='Overall score filter'>"
              "<span>Overall:</span>"
              "<button type='button' class='result-filter selected' data-score-filter='all' "
              "aria-pressed='true'>All scores</button>" + "".join(
                  f"<button type='button' class='result-filter' data-score-filter='{score}' "
                  f"aria-label='Overall score {score}' aria-pressed='false'>{score}</button>"
                  for score in range(1, 6)) + "</span>" if scored else "")
    return (f"<div class='results-toolbar' data-results-toolbar data-tag-filter='all'>"
               f"<span class='result-filters'>"
               f"<button type='button' class='result-filter selected' data-result-filter='all' "
               f"aria-pressed='true'>All results ({count:,})</button>"
               f"<button type='button' class='result-filter' data-result-filter='tagged' "
               f"aria-pressed='false' hidden>Tagged (<span data-tagged-count>0</span>)</button>"
               f"</span>{scores}<span class='tag-filters' data-tag-filters hidden></span>"
               f"<span class='result-actions'>"
               f"<span data-result-count aria-live='polite'></span>"
               f"<button type='button' data-untag-all hidden>Untag all on page</button>"
               f"<button type='button' data-copy-results>Copy</button>"
               f"<button type='button' data-export-csv>CSV</button>"
               f"<button type='button' data-clear-tags hidden>Clear all</button>"
               f"<span class='clear-tags-confirm' data-clear-tags-confirm hidden>Clear all? "
               f"<button type='button' data-confirm-clear-tags>Confirm</button>"
               f"<button type='button' data-cancel-clear-tags>Cancel</button></span>"
               f"</span></div>")


def _cross_encoder_table(search: SearchResult, *, readonly: bool = False) -> str:
    """Deduplicate by CE, then rank by overall with CE breaking ties."""
    rows = sorted((row for pond in search.ponds for row in pond.candidates
                   if row.cross_encoder_score is not None),
                  key=lambda row: row.cross_encoder_score_1_to_5, reverse=True)
    best = {}
    for row in rows:
        best.setdefault(row.person_id, row)
    if not best:
        if any(row.cross_encoder_status for pond in search.ponds for row in pond.candidates):
            return "<p class='empty-pond'>CE scores are unavailable for this run. Main search results are unchanged.</p>"
        return ""
    ranked = sorted(best.values(), key=lambda row: (
        _overall_score(row, search.candidate(row.person_id)) or 0,
        row.cross_encoder_score_1_to_5), reverse=True)
    body = [_candidate_row(row, search.run_id, search.candidate(row.person_id),
                           lazy=index >= VISIBLE_ROWS, cross_encoder=True, readonly=readonly)
            for index, row in enumerate(ranked)]
    return (f"<div data-pond-panel='{_e(search.run_id)}:overall'>"
            + _results_toolbar(len(ranked), scored=True)
            + _results_table(body, heading="Overall score and reasoning") + "</div>")


def _search(search: SearchResult, *, readonly: bool = False, feedback_enabled: bool = False) -> str:
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
          <span>{_e(_date(search.created_at))} · {_e(search.run_id)}</span>
        </span>
      </header>
      {jd}
      <div class='search-body' data-search-body='{_e(search.run_id)}' data-search-title='{_e(search.title)}'>{render_search_body(search, readonly=not feedback_enabled) if readonly else "<p class='loading-results'>Loading results…</p>"}</div>
    </article>"""


def render_search_body(search: SearchResult, *, readonly: bool = False) -> str:
    tabs = []
    panels = []
    fit_table = _cross_encoder_table(search, readonly=readonly)
    for index, pond in enumerate(search.ponds):
        panel_id = f"pond-results-{_e(search.run_id)}-{pond.pond_n}"
        tabs.append(_pond(pond, "" if fit_table else panel_id, selected=index == 0))
        if fit_table:
            continue
        panels.append(
            f"<div id='{panel_id}' class='pond-panel' role='tabpanel' "
            f"data-pond-panel='{_e(pond.run_id)}:{pond.pond_n}'{' hidden' if index else ''}>"
            f"{_pond_table(search, pond, readonly=readonly)}</div>")
    chain_role = "" if fit_table else "role='tablist'"
    return (f"<section class='pond-section'><h2>Search chain</h2>"
            f"<ol {chain_role} aria-label='Pond results'>{''.join(tabs)}</ol></section>"
            f"<section class='groups-section'>"
            f"{fit_table or ''.join(panels)}"
            f"</section>")


def render_page(searches: Iterable[SearchResult], *, readonly: bool = False,
                tags: dict | None = None, feedback_enabled: bool = False) -> str:
    items = tuple(searches)
    body = "".join(_search(search, readonly=readonly, feedback_enabled=feedback_enabled) for search in items)
    if not body:
        body = "<section class='empty-state'><h2>No completed searches</h2><p>No results.json with a summary block was found.</p></section>"
    template = RESULTS_HTML.read_text(encoding="utf-8")
    if readonly:
        template = template.replace("<html lang='en'>", "<html lang='en' data-readonly='true'>")
        if feedback_enabled:
            template = template.replace("data-readonly='true'", "data-readonly='true' data-hosted-feedback='true'")
        saved_tags = json.dumps(tags, ensure_ascii=False).replace("<", "\\u003c")
        template = template.replace("<script src=", f"<script id='snapshot-tags' type='application/json'>{saved_tags}</script><script src=")
    ratings = json.dumps({"rubric": RUBRIC, "legacy": LEGACY_SCORES}, ensure_ascii=False)
    return template.replace("{{CONTENT}}", body).replace("{{HUMAN_RATINGS}}", ratings)
