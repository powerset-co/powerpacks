"""Estimate and answer one worth-plus-label request using the standard JEV cache."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tiktoken

from packs.ingestion.primitives.deep_context.jev_worth.model import predict, supporting_features
from packs.ingestion.primitives.deep_context.jev_worth.questions import REQUEST_VERSION, build_request
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION, answer_requests, cache_path, request_digest,
)

# Positive/negative observations are phrased from the answer, never the verdict.
_REASON_PHRASES = {
    'is_family': ('a family connection', 'a family connection'),
    'is_close_friend': ('a close friendship', 'a close friendship'),
    'is_founder': ('a founder background', 'a founder background'),
    'is_investor': ('an investing background', 'an investing background'),
    'is_coworker_current': ('a current coworker relationship', 'a current coworker relationship'),
    'is_coworker_past': ('a former coworker relationship', 'a former coworker relationship'),
    'is_classmate': ('a shared school background', 'a shared school background'),
    'is_mentor_or_advisor': ('a mentoring or advisory relationship', 'a mentoring or advisory relationship'),
    'is_client': ('a client relationship', 'a client relationship'),
    'is_recruiter': ('recruiting activity', 'recruiting activity'),
    'is_service_provider': ('a service-provider relationship', 'a service-provider relationship'),
    'is_automated_sender': ('automated messages', 'automated messages'),
    'is_stranger': ('unsolicited contact', 'unsolicited contact'),
    'is_transactional': ('mainly transactional contact', 'transactional contact'),
    'transactional_only': ('mainly transactional contact', 'transactional contact'),
    'is_professional': ('work-related contact', 'work-related context'),
    'work_signal': ('work-related contact', 'work-related context'),
    'is_personal': ('a personal relationship', 'a personal relationship'),
    'is_vendor_or_partner': ('a vendor or business partnership', 'a vendor or business partnership'),
    'is_mentee_or_report': ('someone you mentor or manage', 'a mentoring or reporting relationship'),
    'is_neighbor_or_local': ('a connection through your local community', 'a local connection'),
    'met_in_person': ('having met in person', 'having met in person'),
    'owner_would_intro': ('someone you would introduce', 'comfort making an introduction'),
    'they_would_take_owner_call': ('someone who would take your call', 'a connection strong enough for a call'),
    'notable': ('public recognition in their field', 'public recognition in their field'),
    'sensitive_context': ('sensitive topics', 'sensitive topics'),
    'is_healthcare_legal_or_financial_provider': ('medical, legal, or financial services', 'medical, legal, or financial services'),
    'confidential_dealings': ('confidential matters', 'confidential matters'),
    'is_minor': ('someone under 18', 'the person being under 18'),
    'real_relationship': ('direct correspondence', 'an established relationship'),
    'professional_standing': ('an established professional background', 'an established professional background'),
    'noise': ('unsolicited outreach or broadcasts', 'unsolicited outreach or broadcasts'),
    'evidence_incomplete': ('limited context about the relationship', 'gaps in the relationship context'),
}
_CHOICE_PHRASES = {
    'family': 'a family connection', 'romantic_partner': 'a romantic relationship',
    'close_friend': 'a close friendship', 'friend': 'a friendship', 'acquaintance': 'a casual acquaintance',
    'colleague': 'a coworker relationship', 'business_contact': 'a work connection',
    'service_provider': 'a service-provider relationship', 'community': 'a community connection',
    'stranger': 'unsolicited contact',
    'professional_only': 'work-only correspondence', 'personal_only': 'personal-only correspondence',
    'mixed': 'both personal and work-related correspondence',
    'cold_outreach': 'an unsolicited introduction', 'mutual_friend': 'an introduction through a mutual friend',
    'work': 'meeting through work', 'school': 'meeting through school', 'online': 'meeting online',
    'event': 'meeting at an event', 'manager': 'someone who managed you', 'peer': 'a peer relationship',
    'report': 'someone you managed', 'none': 'no shared reporting line',
    'executive': 'an executive role', 'senior': 'a senior role', 'mid': 'an established career',
    'junior': 'an early career', 'student': 'current studies', 'retired': 'retirement',
}
_REASON_LIMIT = 3
_CLEAR_HIGH = 0.6
_CLEAR_LOW = 0.4


def _phrase(name: str, option: str | None, probability: float) -> str:
    if option == 'unknown':
        description = 'unclear ' + {'relationship_kind': 'relationship context', 'mode': 'personal or work context',
                            'hierarchy': 'reporting relationship', 'intro_source': 'how you met',
                            'seniority': 'career stage', 'function': 'work background'}[name]
        if probability >= _CLEAR_HIGH:
            return description
        return ('little indication of ' if probability <= _CLEAR_LOW else 'some uncertainty about ') + description
    if name == 'warmth':
        positive = ('little relationship beyond automated contact', 'a distant acquaintance',
                    'a friendly connection', 'a close connection', 'an inner-circle connection')[int(option)]
        negative = positive
    elif option is not None:
        positive = _CHOICE_PHRASES.get(option, option.replace('_', ' ') + ' work')
        negative = positive
    else:
        positive, negative = _REASON_PHRASES[name]
    if probability >= _CLEAR_HIGH:
        return positive
    if probability <= _CLEAR_LOW:
        return 'little indication of ' + negative
    return 'uncertain evidence of ' + positive


def estimate(request: dict, *, output_dir: Path | None = None) -> dict:
    cached = output_dir is not None and cache_path(output_dir, request_digest(request)).exists()
    tokens = 0 if cached else len(tiktoken.get_encoding('o200k_base').encode(json.dumps(request, ensure_ascii=False, sort_keys=True)))
    return {'input_tokens': tokens, 'cost_usd': tokens * INPUT_PRICE_PER_MILLION / 1_000_000, 'cached': cached}


def _labels(answers: dict[str, dict]) -> dict[str, str | float | int]:
    result = {}
    for name, answer in answers.items():
        if answer['type'] == 'noul':
            result[name] = float(answer['noul'])
            continue
        probabilities = answer['probabilities']
        best = max(probabilities, key=probabilities.__getitem__)
        result[name] = int(best) if answer['type'] == 'score' else best
        if answer['type'] == 'choice':
            result[name + '_p'] = float(probabilities[best])
    return result


def _reason(answers: dict[str, dict], *, decision: str) -> str:
    phrases = []
    for name, option, probability in supporting_features(answers, decision=decision):
        phrase = _phrase(name, option, probability)
        if phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) == _REASON_LIMIT:
            break
    if not phrases:
        return 'The combined signals give no clear explanation to single out.'
    # More specific outreach wording already covers the generic contact signal.
    phrases = [phrase for phrase in phrases
               if not (phrase.endswith('unsolicited contact')
                       and phrase.replace('unsolicited contact', 'unsolicited outreach or broadcasts') in phrases)]
    groups = {}
    for phrase in phrases:
        prefix = next((prefix for prefix in ('little indication of ', 'uncertain evidence of ',
                                             'some uncertainty about ') if phrase.startswith(prefix)), '')
        groups.setdefault(prefix, []).append(phrase[len(prefix):])
    sentences = []
    for prefix, parts in groups.items():
        conjunction = ', or ' if prefix == 'little indication of ' else ' and '
        joined = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + conjunction + parts[-1]
        if prefix == 'little indication of ':
            sentences.append("There's little evidence of " + joined + '.')
        elif prefix:
            sentences.append('The context is less clear about ' + joined + '.')
        else:
            sentences.append('Looks like ' + joined + '.')
    return ' '.join(sentences)


async def classify(
    *, facts: dict[str, Any], bundle: dict[str, Any], owner: dict[str, Any],
    reference_date: str, output_dir: Path, api_key: str | None = None, client: Any | None = None,
) -> dict:
    request = build_request(facts=facts, bundle=bundle, owner=owner, reference_date=reference_date)
    digest = request_digest(request)
    answered = await answer_requests(
        {digest: request}, output_dir=output_dir, api_key=api_key, client=client,
        concurrency=1, request_version=REQUEST_VERSION, question_version=REQUEST_VERSION,
    )
    answer = answered[digest]
    answers = answer.response['answers']
    decision = predict(answers)
    return {
        'network_worth': {'decision': decision, 'reason': _reason(answers, decision=decision)},
        'labels': _labels(answers),
        'usage': {**answer.response['usage'], 'cached': answer.cached},
    }
