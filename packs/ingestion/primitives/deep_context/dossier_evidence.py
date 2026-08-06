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

    name: str = ""
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
        people = {row.person_id: row.parent_id for row in snapshot.people}
        known_parents = {row.parent_id for row in snapshot.parents}
        parent_ids = {people[value] for value in wanted if value in people}
        parent_ids.update(wanted & known_parents)
        selected_people = wanted | {
            person_id for person_id, parent_id in people.items()
            if parent_id in parent_ids
        }
        parent_fact_owners = {
            row.parent_id for row in snapshot.facts
            if row.parent_id in parent_ids and row.person_id is None
        }
        records = [
            {"facts": payload}
            for row in snapshot.facts
            if row.parent_id in parent_ids
            and (
                row.person_id is None
                or (
                    row.parent_id not in parent_fact_owners
                    and row.person_id in selected_people
                )
            )
            and (payload := _object(row.facts_json))
        ]
        parent_names = {
            row.parent_id: str(row.display_name or row.public_identifier or "")
            for row in snapshot.parents
            if row.parent_id in parent_ids
        }
        parent_bundle_owners = {
            artifact.parent_id for artifact in snapshot.artifacts
            if artifact.kind == "source_bundle"
            and artifact.status == "projected"
            and artifact.parent_id in parent_ids
            and artifact.person_id is None
        }
        messages: list[dict[str, Any]] = []
        for artifact in snapshot.artifacts:
            if (
                artifact.kind != "source_bundle"
                or artifact.status != "projected"
                or artifact.parent_id not in parent_ids
                or (
                    artifact.person_id is not None
                    and (
                        artifact.parent_id in parent_bundle_owners
                        or artifact.person_id not in selected_people
                    )
                )
            ):
                continue
            payload = _object(artifact.payload_json)
            messages.extend(
                row for row in payload.get("messages") or [] if isinstance(row, dict)
            )
        merged = merge_facts(records) if records else {}
        return cls.from_facts(
            merged,
            messages,
            name=next(iter(parent_names.values()), "") if len(parent_names) == 1 else "",
        )

    @classmethod
    def from_parent(
        cls,
        parent_id: str,
        snapshot: CanonicalSnapshot,
    ) -> DossierEvidence:
        """Hydrate the one parent-owned evidence packet."""
        return cls.from_snapshot((parent_id,), snapshot)

    @classmethod
    def from_facts(
        cls,
        facts: dict[str, Any],
        messages: Iterable[dict[str, Any]] = (),
        *,
        name: str = "",
    ) -> DossierEvidence:
        message_rows = tuple(messages)
        return cls(
            name=name,
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

    @classmethod
    def from_judge_dict(
        cls,
        payload: dict[str, Any],
        *,
        name: str = "",
    ) -> DossierEvidence:
        """Parse the historical task-dict boundary into the one evidence type."""
        return cls(
            name=name or str(payload.get("name") or ""),
            relationship=str(payload.get("relationship") or ""),
            title=str(payload.get("title") or ""),
            employers=tuple(str(value) for value in payload.get("employers") or ()),
            school=str(payload.get("school") or ""),
            location=str(payload.get("location") or ""),
            topics=tuple(str(value) for value in payload.get("topics") or ()),
            shared_context=tuple(
                str(value) for value in payload.get("shared_context") or ()
            ),
            aliases=tuple(str(value) for value in payload.get("aliases") or ()),
            from_me=tuple(str(value) for value in payload.get("from_me") or ()),
            from_them=tuple(str(value) for value in payload.get("from_them") or ()),
            has_messages=bool(payload.get("has_messages")),
        )

    def as_judge_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
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

    def render_identity_side(
        self,
        label: str,
        name: str,
        emails: Iterable[str],
        extra_emails: Iterable[str] = (),
    ) -> str:
        """Render the pinned merge-judge side from this evidence packet."""
        facts = []
        if self.relationship:
            facts.append(f"relationship: {self.relationship}")
        if self.title or self.employers:
            employers = f"@ {', '.join(self.employers)}" if self.employers else ""
            facts.append(f"work: {self.title} {employers}".strip())
        for field_name, value in (("school", self.school), ("location", self.location)):
            if value:
                facts.append(f"{field_name}: {value}")
        if self.topics:
            facts.append(f"we discuss: {', '.join(self.topics)}")
        facts_block = "\n".join(f"  {fact}" for fact in facts) or "  (no extracted facts)"
        mine = "\n".join(
            f"  me→them: {text}" for text in self.from_me
        ) or "  (no messages from me — tone unavailable)"
        theirs = "\n".join(
            f"  them→me: {text}" for text in self.from_them
        ) or "  (no messages from them)"
        email_text = ", ".join(emails) or "none"
        extra = ", ".join(extra_emails)
        extra_line = f"  [owned identifier seen in messages: {extra}]\n" if extra else ""
        return (
            f"CONTACT {label} — {name}  [emails: {email_text}]\n{extra_line}"
            f"{facts_block}\nMessages:\n{mine}\n{theirs}"
        )


def owner_background(snapshot: CanonicalSnapshot) -> str:
    """Render the canonical owner payload with the existing prompt policy."""
    return owner_background_block(snapshot.owner) if snapshot.owner else ""
