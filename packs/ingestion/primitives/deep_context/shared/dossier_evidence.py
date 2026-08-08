"""Render message-derived identity evidence from narrow canonical SQLite reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import (
    MessageDirection,
    MessageEntry,
)
from packs.ingestion.primitives.deep_context.shared.common import owner_background_block
from packs.ingestion.primitives.deep_context.synthesis.facts import merge_fact_records
from packs.ingestion.primitives.deep_context.synthesis.models import (
    FactRecord,
    SynthesizedFacts,
)
from packs.ingestion.primitives.deep_context.db import context_queries, queries
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import DossierEvidenceRows


def _sample(
    messages: Iterable[MessageEntry],
    direction: MessageDirection,
) -> tuple[str, ...]:
    """Newest-first, 4 messages, 200 chars each — this is the tone sample a human/judge sees.

    Not the same cap as collection's SAFETY_CHAR_CAP/CHAT_MESSAGE_CAP; this is a
    display-time excerpt over whatever collection already bounded, not a second
    collection policy.
    """
    selected = [
        (message.text or "").strip()[:200]
        for message in sorted(messages, key=lambda item: item.at or "", reverse=True)
        if message.direction == direction and (message.text or "").strip()
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
    def from_db(
        cls,
        db: Db,
        person_ids: Iterable[str],
    ) -> DossierEvidence:
        """Hydrate one evidence packet from only its parent-family rows."""
        subject_ids = tuple(person_ids)
        return cls.from_rows(
            subject_ids,
            context_queries.dossier_evidence_rows(db, subject_ids),
        )

    @classmethod
    def from_rows(
        cls,
        person_ids: Iterable[str],
        rows: DossierEvidenceRows,
    ) -> DossierEvidence:
        """Apply the pinned fact and source-bundle election to typed query rows."""
        wanted = {str(person_id).strip().lower() for person_id in person_ids}
        people = {row.person_id: row.parent_id for row in rows.people}
        known_parents = {row.parent_id for row in rows.parents}
        parent_ids = {people[value] for value in wanted if value in people}
        parent_ids.update(wanted & known_parents)
        selected_people = wanted | {person_id for person_id, parent_id in people.items() if parent_id in parent_ids}
        # Election: a parent-owned fact row (person_id is None) means that parent has
        # already been synthesized at the merged level — use only that row. Otherwise
        # fall back to merging its still-unmerged children's individual fact rows.
        parent_fact_owners = {
            row.parent_id for row in rows.facts if row.parent_id in parent_ids and row.person_id is None
        }
        records = [
            record
            for row in rows.facts
            if row.parent_id in parent_ids
            and (
                row.person_id is None or (row.parent_id not in parent_fact_owners and row.person_id in selected_people)
            )
            and (
                record := FactRecord.from_payload(
                    {
                        "facts": parse_json_object(row.facts_json),
                    }
                )
            )
            is not None
        ]
        parent_names = {
            row.parent_id: str(row.display_name or row.public_identifier or "")
            for row in rows.parents
            if row.parent_id in parent_ids
        }
        # Same election as parent_fact_owners above, applied to raw message bundles.
        parent_bundle_owners = {
            artifact.parent_id
            for artifact in rows.source_bundles
            if artifact.parent_id in parent_ids and artifact.person_id is None
        }
        messages: list[MessageEntry] = []
        for artifact in rows.source_bundles:
            if artifact.parent_id not in parent_ids or (
                artifact.person_id is not None
                and (artifact.parent_id in parent_bundle_owners or artifact.person_id not in selected_people)
            ):
                continue
            payload = parse_json_object(artifact.payload_json)
            messages.extend(
                message
                for row in payload.get("messages") or []
                if (message := MessageEntry.from_payload(row)) is not None
            )
        merged: SynthesizedFacts | None = merge_fact_records(records) if records else None
        if merged is None:
            merged = SynthesizedFacts()
        return cls.from_facts(
            merged,
            messages,
            name=next(iter(parent_names.values()), "") if len(parent_names) == 1 else "",
        )

    @classmethod
    def from_parent_db(cls, db: Db, parent_id: str) -> DossierEvidence:
        """Hydrate one parent-owned packet through the narrow database query."""
        return cls.from_db(db, (parent_id,))

    @classmethod
    def from_facts(
        cls,
        facts: SynthesizedFacts,
        messages: Iterable[MessageEntry] = (),
        *,
        name: str = "",
    ) -> DossierEvidence:
        message_rows = tuple(messages)
        return cls(
            name=name,
            relationship=facts.relationship_to_owner,
            title=facts.title,
            employers=tuple(row.name for row in facts.employers if row.name),
            school=facts.school,
            location=facts.location,
            topics=facts.topics[:10],
            shared_context=tuple(f"{row.overlap}: {row.detail}" for row in facts.shared_context if row.detail),
            aliases=tuple(value.strip() for value in facts.aliases if value.strip())[:8],
            from_me=_sample(message_rows, MessageDirection.FROM_ME),
            from_them=_sample(message_rows, MessageDirection.FROM_THEM),
            has_messages=bool(message_rows),
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
        mine = "\n".join(f"  me→them: {text}" for text in self.from_me) or "  (no messages from me — tone unavailable)"
        theirs = "\n".join(f"  them→me: {text}" for text in self.from_them) or "  (no messages from them)"
        email_text = ", ".join(emails) or "none"
        extra = ", ".join(extra_emails)
        extra_line = f"  [owned identifier seen in messages: {extra}]\n" if extra else ""
        return (
            f"CONTACT {label} — {name}  [emails: {email_text}]\n{extra_line}{facts_block}\nMessages:\n{mine}\n{theirs}"
        )


def owner_background(db: Db) -> str:
    """Render the canonical owner payload with the existing prompt policy.

    The identity-judge anchor: every enrich reconciliation/healing prompt
    (reconcile_linkedin, identity_reconcile, research_reconcile) calls this,
    not owner_background_block directly, so a missing owner silently renders "".
    """
    owner = queries.owner_profile(db)
    return owner_background_block(owner) if owner else ""
