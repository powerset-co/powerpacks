"""Render message-derived identity evidence from canonical SQLite snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.common import owner_background_block
from packs.ingestion.primitives.deep_context.dossier.facts import merge_facts
from packs.ingestion.primitives.deep_context.db.models import CanonicalSnapshot


def _object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sample(messages: Iterable[dict[str, Any]], direction: str) -> tuple[str, ...]:
    selected = [
        str(message.get("text") or "").strip()[:200]
        for message in sorted(messages, key=lambda item: item.get("at") or "", reverse=True)
        if message.get("direction") == direction and str(message.get("text") or "").strip()
    ]
    return tuple(selected[:4])


@dataclass(frozen=True)
class DossierEvidence:
    """Frozen evidence packet shared by research, identity judging, and healing."""

    relationship: str = ""
    title: str = ""
    employers: tuple[str, ...] = ()
    school: str = ""
    location: str = ""
    topics: tuple[str, ...] = ()
    shared_context: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    from_me: tuple[str, ...] = ()
    from_them: tuple[str, ...] = ()
    has_messages: bool = False

    @classmethod
    def from_snapshot(
        cls,
        person_ids: Iterable[str],
        snapshot: CanonicalSnapshot,
    ) -> DossierEvidence:
        """Hydrate facts and message samples from their projected payloads."""
        wanted = {str(person_id).strip().lower() for person_id in person_ids}
        records = [
            {"facts": payload}
            for row in snapshot.facts
            if row.person_id in wanted and (payload := _object(row.facts_json))
        ]
        messages: list[dict[str, Any]] = []
        for artifact in snapshot.artifacts:
            if (
                artifact.kind != "source_bundle"
                or artifact.status != "projected"
                or artifact.person_id not in wanted
            ):
                continue
            payload = _object(artifact.payload_json)
            messages.extend(
                row for row in payload.get("messages") or [] if isinstance(row, dict)
            )
        merged = merge_facts(records) if records else {}
        return cls.from_facts(merged, messages)

    @classmethod
    def from_facts(
        cls,
        facts: dict[str, Any],
        messages: Iterable[dict[str, Any]] = (),
    ) -> DossierEvidence:
        message_rows = tuple(messages)
        return cls(
            relationship=str(facts.get("relationship_to_owner") or ""),
            title=str(facts.get("title") or ""),
            employers=tuple(
                str(row.get("name") or "")
                for row in facts.get("employers") or []
                if isinstance(row, dict) and row.get("name")
            ),
            school=str(facts.get("school") or ""),
            location=str(facts.get("location") or ""),
            topics=tuple(facts.get("topics") or ())[:10],
            shared_context=tuple(
                f"{row.get('overlap', 'other')}: {row.get('detail', '')}"
                for row in facts.get("shared_context") or []
                if isinstance(row, dict) and row.get("detail")
            ),
            aliases=tuple(
                str(value).strip()
                for value in facts.get("aliases") or []
                if str(value).strip()
            )[:8],
            from_me=_sample(message_rows, "from_me"),
            from_them=_sample(message_rows, "from_them"),
            has_messages=bool(message_rows),
        )

    def as_judge_dict(self) -> dict[str, Any]:
        return {
            "relationship": self.relationship,
            "title": self.title,
            "employers": list(self.employers),
            "school": self.school,
            "location": self.location,
            "topics": list(self.topics),
            "shared_context": list(self.shared_context),
            "from_me": list(self.from_me),
            "from_them": list(self.from_them),
            "has_messages": self.has_messages,
        }

    def research_bio(self) -> str:
        parts = []
        if self.aliases:
            parts.append(f"Also known as: {', '.join(self.aliases)}")
        if self.relationship:
            parts.append(f"My relationship: {self.relationship}")
        if self.employers:
            parts.append(f"Employers (from our messages): {', '.join(self.employers)}")
        if self.school:
            parts.append(f"School: {self.school}")
        if self.location:
            parts.append(f"Location: {self.location}")
        if self.topics:
            parts.append(f"We discuss: {', '.join(self.topics[:8])}")
        if self.shared_context:
            parts.append(f"Shared context with me: {'; '.join(self.shared_context[:8])}")
        return ". ".join(parts)


def owner_background(snapshot: CanonicalSnapshot) -> str:
    """Render the canonical owner payload with the existing prompt policy."""
    return owner_background_block(snapshot.owner) if snapshot.owner else ""
