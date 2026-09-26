"""Joint worth and share questions over synthesized facts and message counts."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from dataclasses import asdict

from packs.ingestion.primitives.share.questions import build_questions as share_questions
from packs.ingestion.primitives.share.questions import build_request as share_request

from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle, MessageDirection
from packs.ingestion.primitives.deep_context.db.models import OwnerProfile
from packs.ingestion.primitives.deep_context.jev_worth.models import WorthFacts
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory

REQUEST_VERSION = 'deep-context-worth-labels-v1-20260924'
_WORTH_QUESTIONS = json.loads(Path(__file__).with_name('worth_questions.json').read_text())
WORTH_SIGNALS = tuple(name for name in _WORTH_QUESTIONS if name != 'worth')


def _channel_policy(sources: frozenset[str]) -> str:
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


def build_questions(sources: frozenset[str]) -> dict[str, dict]:
    worth = copy.deepcopy(_WORTH_QUESTIONS)
    worth['worth']['instructions'] += _channel_policy(sources)
    return {**share_questions(), **worth}


def build_request(
    *, facts: WorthFacts, bundle: CollectionBundle | None, owner: OwnerProfile | None, reference_date: str,
    history: FactHistory | None = None,
) -> dict:
    """Serialize the pinned request; policy consumes typed facts and messages."""
    payload = {key: value for key, value in json.loads(facts.serialized).items()
               if key not in ('network_worth', 'labels', 'owned_identifiers')}
    messages = history.messages if history and history.messages else bundle.messages if bundle else ()
    timestamps = [message.at for message in messages if message.at]
    sources = sorted(history.source_channels) if history and history.messages else sorted(bundle.source_channels) if bundle else []
    counts = dict(Counter(message.channel for message in messages)) if messages else None
    channels = {
        'source_channels': sources,
        'interaction_counts': counts,
        'first_message_at': min(timestamps, default=None),
        'last_message_at': max(timestamps, default=None),
        'last_interaction': max(timestamps, default=None),
        'from_me': sum(message.direction == MessageDirection.FROM_ME for message in messages) if messages else None,
        'from_them': sum(message.direction == MessageDirection.FROM_THEM for message in messages) if messages else None,
        'group_count': len(history.groups if history and history.messages else bundle.groups) if messages else None,
    }
    current = next((employer for employer in facts.facts.employers if employer.status == 'current'), None)
    owner_payload = asdict(owner) if owner else {}
    request = share_request(
        dossier='\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in payload.items() if value),
        facts=payload,
        profile={'name': facts.facts.canonical_name if 'canonical_name' in payload else None,
                 'title': facts.facts.title if 'title' in payload else None,
                 'company': current.name if current else None,
                 'location': facts.facts.location if 'location' in payload else None, 'headline': None},
        channels=channels,
        owner={key: owner_payload.get(key) for key in ('name', 'work', 'education', 'locations')},
        reference_date=reference_date,
    )
    request['questions'] = build_questions(frozenset(source.strip().lower() for source in sources) | frozenset(counts or ()))
    return request
