"""Direct, conservative projection of saved synthesis, profiles and human tags."""
from __future__ import annotations

import json
from collections.abc import Iterator

from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.primitives.enrich.profile_transforms import _explicit_current_value
from packs.memory.models import Claim, Kind, object_dict, string, strings


def facts(content: str) -> SynthesizedFacts:
    return SynthesizedFacts.from_payload(object_dict(json.loads(content)))


def fact_claims(saved: SynthesizedFacts) -> Iterator[Claim]:
    def claim(text: str, kind: Kind, topic: str, scope: str = "", obj: str = "", quote: str = "") -> Claim:
        return Claim(text, kind, topic, scope, obj, "", "", quote or text)

    for topic in saved.topics:
        if topic.strip():
            yield claim(topic, Kind.MENTION, topic)
    for name in ("title", "school", "field_of_study", "location"):
        text = getattr(saved, name)
        if text.strip():
            yield claim(text, Kind.STATEMENT, name)
    if saved.relationship_to_owner.strip():
        yield claim(saved.relationship_to_owner, Kind.RELATIONSHIP, "relationship", "owner")
    for employer in saved.employers:
        if employer.name.strip():
            yield claim(f"{employer.role} @ {employer.name} ({employer.status})", Kind.STATEMENT,
                        "employment", obj=employer.name, quote=employer.name)
    for event in saved.notable_events:
        if event.summary.strip():
            yield claim(event.date + " " + event.summary, Kind.STATEMENT, "event", quote=event.summary)
    for context in saved.shared_context:
        if context.detail.strip():
            yield claim(context.detail, Kind.STATEMENT, context.overlap or "shared context", "owner")


def _date(value: object) -> str:
    if value is None:
        return ""
    row = object_dict(value)
    if not row.get("year"):
        return ""
    text = f"{int(str(row['year'])):04d}"
    if row.get("month"):
        text += f"-{int(str(row['month'])):02d}"
        if row.get("day"):
            text += f"-{int(str(row['day'])):02d}"
    return text


def project(key: str, content: str) -> tuple[Claim, ...]:
    if key.startswith("facts:"):
        return tuple(fact_claims(facts(content)))
    row = object_dict(json.loads(content)) if key.startswith(("profile:", "tags:")) else {}
    result: list[Claim] = []
    if key.startswith("tags:"):
        for tag in strings(row["human_tags"]):
            if tag.strip():
                result.append(Claim(tag, Kind.TAG, tag, "", "", "", "", tag))
        note = string(row["human_note"])
        if note.strip():
            result.append(Claim(note, Kind.STATEMENT, "human note", "owner", "", "", "", note))
    if key.startswith("profile:"):
        for field in ("headline", "summary", "location_str"):
            text = string(row.get(field) or "")
            if text.strip():
                result.append(Claim(text, Kind.STATEMENT, field, "", "", "", "", text))
        experiences = row.get("experiences") or []
        if not isinstance(experiences, list):
            raise ValueError("expected experiences list")
        for value in experiences:
            experience = object_dict(value)
            company = string(experience.get("company_name") or "")
            if not company:
                continue
            title = string(experience.get("title") or "")
            start, end = _date(experience.get("starts_at")), _date(experience.get("ends_at"))
            if not end and _explicit_current_value(experience) is True:
                end = "present"
            text = f"{title} @ {company}"
            claim = Claim(text, Kind.TENURE, "employment", "", company, start, end, company)
            # Validate partial dates at the external boundary, retaining original precision.
            result.append(Claim.parse(json.loads(claim.serialized())))
        education = row.get("education") or []
        if not isinstance(education, list):
            raise ValueError("expected education list")
        for value in education:
            school = object_dict(value)
            for field in ("schoolName", "degree", "fieldOfStudy"):
                text = string(school.get(field) or "")
                if text.strip():
                    result.append(Claim(text, Kind.STATEMENT, field, "", "", "", "", text))
    return tuple(result)
