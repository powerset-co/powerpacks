"""Joint worth and share questions over synthesized facts and message counts."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.share.questions import build_questions as share_questions
from packs.ingestion.primitives.share.questions import build_request as share_request

REQUEST_VERSION = 'deep-context-worth-labels-v1-20260924'
_WORTH_QUESTIONS = json.loads(Path(__file__).with_name('worth_questions.json').read_text())
WORTH_SIGNALS = tuple(name for name in _WORTH_QUESTIONS if name != 'worth')


def _channel_policy(channels: dict[str, Any]) -> str:
    sources = {source.strip().lower() for source in channels.get('source_channels', [])}
    sources.update(channels.get('interaction_counts') or {})
    email = bool(sources & {'gmail', 'gmail_msgvault', 'email'})
    phone = bool(sources & {'imessage', 'whatsapp', 'sms', 'phone'})
    if email and phone:
        rule = (
            'This dossier has both email and phone-message context. Bias toward yes when '
            'either channel shows a genuine human relationship; automated noise in one '
            'channel must not erase real correspondence in the other. Use maybe only when '
            'both channels remain genuinely ambiguous.'
        )
    elif email:
        rule = (
            'This is an email-backed dossier. Bias toward yes for clearly human, '
            'person-directed correspondence, including sparse, one-off, old, academic, '
            'or plausibly important professional contacts. Use no only for clear automated '
            'mail, broadcast/transactional noise, or unengaged cold spam. Maybe should be rare.'
        )
    elif phone:
        rule = (
            'This is a phone-message-backed dossier. Repeated or clearly two-way personal '
            'or professional conversation is yes. Sparse context, a bare number, or an '
            'uncertain one-sided exchange may be maybe; automated service traffic or obvious '
            'spam is no. A name or area code is weak context only.'
        )
    else:
        rule = (
            'The source is unclear. Judge only the supplied message context and identifiers; '
            'prefer maybe over inventing a relationship when the evidence is truly sparse.'
        )
    return '\n\nWORTH SOURCE POLICY:\n' + rule


def build_questions(channels: dict[str, Any]) -> dict[str, dict]:
    worth = copy.deepcopy(_WORTH_QUESTIONS)
    worth['worth']['instructions'] += _channel_policy(channels)
    return {**share_questions(), **worth}


def build_request(
    *, facts: dict[str, Any], bundle: dict[str, Any], owner: dict[str, Any], reference_date: str,
) -> dict:
    """Use facts for content; read raw messages only for cadence metadata."""
    facts = {key: value for key, value in facts.items() if key not in ('network_worth', 'labels', 'owned_identifiers')}
    messages = bundle.get('messages') or []
    timestamps = [message['at'] for message in messages if message.get('at')]
    channels = {
        'source_channels': sorted(bundle.get('source_channels') or []),
        'interaction_counts': dict(Counter(message['channel'] for message in messages)) if messages else None,
        'first_message_at': min(timestamps, default=None),
        'last_message_at': max(timestamps, default=None),
        'last_interaction': max(timestamps, default=None),
        'from_me': sum(message.get('direction') == 'from_me' for message in messages) if messages else None,
        'from_them': sum(message.get('direction') == 'from_them' for message in messages) if messages else None,
        'group_count': len(bundle.get('groups') or []) if messages else None,
    }
    current = next((employer for employer in facts.get('employers') or [] if employer.get('status') == 'current'), {})
    request = share_request(
        dossier='\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in facts.items() if value),
        facts=facts,
        profile={'name': facts.get('canonical_name'), 'title': facts.get('title'),
                 'company': current.get('name'), 'location': facts.get('location'), 'headline': None},
        channels=channels,
        owner={key: owner.get(key) for key in ('name', 'work', 'education', 'locations')},
        reference_date=reference_date,
    )
    request['questions'] = build_questions(channels)
    return request
