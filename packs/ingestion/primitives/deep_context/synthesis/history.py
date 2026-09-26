"""Accumulate immutable extraction records without truncating historical facts."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.collection.models import MessageObservation
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts, OwnedIdentifiers, SynthesisRecord

_LIST_FIELDS = ('aliases', 'employers', 'topics', 'notable_events', 'identifiers', 'shared_context')
_SCALAR_FIELDS = ('canonical_name', 'title', 'school', 'field_of_study', 'location',
                  'relationship_to_owner', 'relationship_category', 'is_owner')


def _unique(values: Iterable[Any]) -> tuple:
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class Extraction:
    record: SynthesisRecord
    serialized: str

    def payload(self) -> dict[str, Any]:
        return json.loads(self.serialized)


@dataclass(frozen=True)
class FactHistory:
    records: tuple[Extraction, ...] = ()

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]]) -> FactHistory:
        unique = {json.dumps(record, sort_keys=True, ensure_ascii=False): record for record in records}
        parsed = [Extraction(record, json.dumps(payload, ensure_ascii=False))
                  for payload in unique.values()
                  if (record := SynthesisRecord.from_payload(payload)) is not None]
        return cls(tuple(sorted(parsed, key=lambda item: item.record.updated_at)))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FactHistory:
        return cls.from_records(payload.get('records', [payload]))

    @property
    def processed(self) -> frozenset[str]:
        return frozenset(message.fingerprint for message in self.messages)

    @property
    def messages(self) -> tuple[MessageObservation, ...]:
        return tuple({message.fingerprint: message for item in self.records for message in item.record.messages}.values())

    @property
    def groups(self) -> tuple[str, ...]:
        return _unique(group for item in self.records for group in item.record.groups)

    @property
    def source_channels(self) -> tuple[str, ...]:
        return _unique(source for item in self.records for source in item.record.source_channels)

    @property
    def facts(self) -> SynthesizedFacts:
        ordered = sorted(self.records, key=lambda item: (
            max((message.at for message in item.record.messages), default=''), item.record.updated_at))
        facts = [item.record.facts for item in ordered if item.record.facts is not None]
        if not facts:
            return SynthesizedFacts()
        if len(facts) == 1:
            return facts[0]
        latest = facts[-1]
        values = {field: _unique(value for fact in facts for value in getattr(fact, field))
                  for field in _LIST_FIELDS}
        values['employers'] = _unique(employer for fact in reversed(facts) for employer in fact.employers)
        for field in _SCALAR_FIELDS:
            candidates = [getattr(fact, field) for fact in facts if getattr(fact, field) not in (None, '')]
            values[field] = candidates[-1] if candidates else getattr(latest, field)
        values['owned_identifiers'] = OwnedIdentifiers(*(
            _unique(value for fact in facts for value in getattr(fact.owned_identifiers, field))
            for field in ('emails', 'phones', 'urls')
        ))
        tagged = self.records[-1].record.facts
        return replace(latest, **values, network_worth=tagged.network_worth if tagged else None,
                       labels=tagged.labels if tagged else {}, present=frozenset().union(*(fact.present for fact in facts)))

    def payload(self) -> dict[str, Any]:
        if not self.records:
            return {}
        if len(self.records) == 1:
            return self.records[0].payload()
        return {**self.records[-1].payload(), 'facts': self.facts.to_payload(),
                'records': [item.payload() for item in self.records]}

