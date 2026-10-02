"""Settle accepted lookup profiles, then parent worth, using SQLite evidence.

Flow::

    accepted identities + profiles -> machine detach empty lookups
    unsure LinkedIns + profiles -> machine detach the ones fetched with nothing on them
    fact worth + non-owner imported messages + real profiles -> parent worth

A parent has a real profile when it is one of the owner's own LinkedIn
connections, when a human kept its LinkedIn, or when its accepted LinkedIn's
fetched profile has content.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import approved_identities, linkedin_queue
from packs.ingestion.primitives.deep_context.db.models import HumanWorth, ParentRow, SourceChannel
from packs.ingestion.primitives.deep_context.db.queries import parents, people, sources
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.worth_views import fact_worth
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import settle_machine_identities
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import profile_payloads
from packs.ingestion.primitives.deep_context.enrich.settle_policy import (
    empty_profile_decision, nothing_to_show, worth_decision,
)
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import stored_imported_people


@dataclass(frozen=True)
class SettleEnrichment:
    db: Db

    def run(self) -> None:
        members = {person.person_id: person for person in people(self.db) if not person.is_owner}
        own_connections = {
            members[source.person_id].parent_id for source in sources(self.db)
            if source.person_id in members and source.source == SourceChannel.LINKEDIN
        }
        messages: dict[str, int] = defaultdict(int)
        for person in stored_imported_people(self.db):
            member = members.get(person.person_id)
            if member is not None:
                messages[member.parent_id] += sum(person.interaction_counts.values())

        accepted = approved_identities(self.db)
        accepted_links = {link.row_key: link for link in links(self.db, row_keys=tuple(row.row_key for row in accepted))}
        profiles = profile_payloads(self.db, candidate_keys=accepted_links)
        real_profiles = set(own_connections)
        settlements = []
        for identity in accepted:
            link = accepted_links[identity.row_key]
            profile = profiles.get(identity.row_key)
            decision = empty_profile_decision(
                link, own_connection=link.parent_id in own_connections, profile=profile,
            )
            if decision is not None:
                settlements.append(decision)
            # Kept: an own connection, a LinkedIn the human kept, or a profile with content.
            else:
                real_profiles.add(link.parent_id)

        # A LinkedIn the machine is unsure of is only worth a person's check when there is a
        # profile to look at: one that was fetched and has nothing on it is detached instead.
        unsure = links(self.db, row_keys=tuple(
            candidate.row_key for row in linkedin_queue(self.db) for candidate in row.candidates
            if candidate.pending and not candidate.synthetic
        ))
        unsure_profiles = profile_payloads(self.db, candidate_keys=tuple(link.row_key for link in unsure))
        for link in unsure:
            profile = unsure_profiles.get(link.row_key)
            # The LinkedIn a person was imported with stays, whatever its profile holds.
            if link.raw_import or not nothing_to_show(profile):
                continue
            decision = empty_profile_decision(link, own_connection=False, profile=profile)
            if decision is not None:
                settlements.append(decision)
        settle_machine_identities(self.db, settlements)

        verdicts = fact_worth(self.db)
        projections = []
        for parent in parents(self.db):
            if parent.parent_id not in verdicts:
                continue
            decision = worth_decision(
                human_worth=HumanWorth(parent.human_worth) if parent.human_worth else None,
                worth=verdicts[parent.parent_id],
                own_connection=parent.parent_id in own_connections,
                real_profile=parent.parent_id in real_profiles,
                messages=messages[parent.parent_id],
            )
            worth = decision.decision if decision else None
            reason = decision.reason if decision else None
            if (worth, reason) == (parent.machine_worth, parent.machine_worth_reason):
                continue
            projections.append(ParentRow(
                parent.parent_id, parent.public_identifier, parent.display_name,
                parent.display_slug, worth, reason, parent.source, parent.updated_at,
            ))
        self.db.project_rows(tuple(projections))
