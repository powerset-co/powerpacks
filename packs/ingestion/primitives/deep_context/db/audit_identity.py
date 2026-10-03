"""Read canonical identity ownership and report defects or review signals.

Reads SQLite in read-only mode; emits JSON without projections or provider calls.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum
from itertools import combinations
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.merge_repair import _given_names_differ
from packs.ingestion.primitives.deep_context.db.context_queries import aggregate_people_from_rows
from packs.ingestion.primitives.deep_context.db.models import (
    PersonRow, PersonIdentifierRow, MergeVerdictRow, MESSAGE_CHANNELS,
)
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import connected_components
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class FindingClass(StrEnum):
    STRUCTURAL_DEFECT = 'structural_defect'
    NEEDS_IDENTITY_REVIEW = 'needs_identity_review'
    NO_EVIDENCE = 'no_evidence'


class NextAction(StrEnum):
    REPAIR_OWNERSHIP = 'repair_ownership'
    REVIEW_IDENTITY = 'review_identity'
    COLLECT_CONTACT_FACTS = 'collect_contact_facts'
    REVIEW_CONTACT_HISTORY = 'review_contact_history'


class AuditCategory(StrEnum):
    MISSING_LINK_OWNER = 'missing_link_owner'
    AMBIGUOUS_LINK_OWNER = 'ambiguous_link_owner'
    CANDIDATE_OWNER_MISMATCH = 'candidate_owner_mismatch'
    ACCEPTED_MERGE_CONFLICTS_WITH_NEGATIVE = 'accepted_merge_conflicts_with_negative'
    MISSING_CONTACT_FACTS = 'missing_contact_facts'
    MIXED_CONTACT_FACTS = 'mixed_contact_facts'
    INCOMPATIBLE_CHILD_NAMES = 'incompatible_child_names'
    FACT_OWNER_MISMATCH = 'fact_owner_mismatch'
    ARTIFACT_OWNER_MISMATCH = 'artifact_owner_mismatch'
    UNASSIGNED_PARENT_HISTORY = 'unassigned_parent_history'

    @property
    def classification(self) -> FindingClass:
        if self in {self.MISSING_CONTACT_FACTS, self.MIXED_CONTACT_FACTS}:
            return FindingClass.NO_EVIDENCE
        if self in {self.AMBIGUOUS_LINK_OWNER, self.ACCEPTED_MERGE_CONFLICTS_WITH_NEGATIVE,
                    self.INCOMPATIBLE_CHILD_NAMES, self.UNASSIGNED_PARENT_HISTORY}:
            return FindingClass.NEEDS_IDENTITY_REVIEW
        return FindingClass.STRUCTURAL_DEFECT

    @property
    def next_action(self) -> NextAction:
        if self is self.UNASSIGNED_PARENT_HISTORY:
            return NextAction.REVIEW_CONTACT_HISTORY
        return {
            FindingClass.STRUCTURAL_DEFECT: NextAction.REPAIR_OWNERSHIP,
            FindingClass.NEEDS_IDENTITY_REVIEW: NextAction.REVIEW_IDENTITY,
            FindingClass.NO_EVIDENCE: NextAction.COLLECT_CONTACT_FACTS,
        }[self.classification]


@dataclass(frozen=True)
class AuditFinding:
    category: AuditCategory
    detail: str
    child_ids: tuple[str, ...] = ()
    row_keys: tuple[str, ...] = ()
    unassigned_messages: int = 0
    unassigned_records: int = 0

    @property
    def classification(self) -> FindingClass:
        return self.category.classification

    @property
    def next_action(self) -> NextAction:
        return self.category.next_action

    def payload(self) -> dict:
        return {**asdict(self), 'classification': self.classification,
                'next_action': self.next_action}


@dataclass(frozen=True)
class ParentAudit:
    parent_id: str
    display_name: str | None
    findings: tuple[AuditFinding, ...]


@dataclass(frozen=True)
class AuditReport:
    parents_scanned: int
    parents: tuple[ParentAudit, ...]

    def payload(self) -> dict:
        findings = [finding for parent in self.parents for finding in parent.findings]
        return {
            'status': 'complete', 'read_only': True,
            'counts': {
                'parents_scanned': self.parents_scanned,
                'parents_with_findings': len(self.parents), 'findings': len(findings),
                'by_category': dict(sorted(Counter(f.category.value for f in findings).items())),
                'by_classification': {kind.value: sum(f.classification == kind for f in findings)
                                      for kind in FindingClass},
            },
            'parents': [
                {'parent_id': parent.parent_id, 'display_name': parent.display_name,
                 'findings': [finding.payload() for finding in parent.findings]}
                for parent in self.parents
            ],
        }


@dataclass(frozen=True)
class _OwnershipIssue:
    parent_id: str
    category: AuditCategory
    row_key: str
    person_id: str | None


@dataclass(frozen=True)
class _FactCoverage:
    subject_key: str
    parent_id: str
    person_id: str | None
    synthesized_at: str | None
    aligned: bool


@dataclass(frozen=True)
class _ArtifactHistory:
    artifact_key: str
    parent_id: str
    person_id: str | None
    history: FactHistory


_OWNERSHIP_SQL = """
SELECT l.parent_id, 'missing_link_owner' category, l.row_key, NULL person_id
FROM links l WHERE NOT EXISTS (SELECT 1 FROM candidate_people cp WHERE cp.row_key=l.row_key)
UNION ALL
SELECT l.parent_id, 'ambiguous_link_owner', l.row_key, NULL
FROM links l JOIN candidate_people cp USING(row_key)
GROUP BY l.row_key HAVING count(DISTINCT cp.person_id)>1
UNION ALL
SELECT cp.parent_id, 'candidate_owner_mismatch', cp.row_key, cp.person_id
FROM candidate_people cp LEFT JOIN links l USING(row_key) LEFT JOIN people p USING(person_id)
WHERE l.row_key IS NULL OR p.person_id IS NULL OR cp.parent_id!=l.parent_id OR cp.parent_id!=p.parent_id
UNION ALL
SELECT f.parent_id, 'fact_owner_mismatch', f.subject_key, f.person_id
FROM facts f LEFT JOIN artifacts a USING(artifact_key) LEFT JOIN people p ON p.person_id=f.person_id
WHERE a.artifact_key IS NULL OR a.parent_id!=f.parent_id OR a.kind!='facts'
 OR a.person_id IS NOT f.person_id OR a.candidate_key IS NOT NULL
 OR (f.person_id IS NOT NULL AND (p.person_id IS NULL OR p.parent_id!=f.parent_id))
UNION ALL
SELECT a.parent_id, 'artifact_owner_mismatch', a.artifact_key, a.person_id
FROM artifacts a LEFT JOIN people p USING(person_id) LEFT JOIN links l ON l.row_key=a.candidate_key
WHERE (a.person_id IS NOT NULL AND (p.person_id IS NULL OR p.parent_id!=a.parent_id))
 OR (a.candidate_key IS NOT NULL AND (l.row_key IS NULL OR l.parent_id!=a.parent_id))
ORDER BY parent_id, category, row_key
"""

_COVERAGE_SQL = """
SELECT f.subject_key,f.parent_id,f.person_id,
 CASE WHEN json_type(a.payload_json,'$.records')='array' THEN
   (SELECT max(json_extract(value,'$.updated_at')) FROM json_each(a.payload_json,'$.records'))
 ELSE json_extract(a.payload_json,'$.updated_at') END synthesized_at,
 (a.parent_id=f.parent_id AND a.person_id IS f.person_id AND a.kind='facts'
  AND a.status='projected' AND a.candidate_key IS NULL) aligned
FROM facts f LEFT JOIN artifacts a USING(artifact_key)
WHERE f.facts_json IS NOT NULL AND f.facts_json!='{}'
ORDER BY f.parent_id,f.subject_key
"""

_HISTORY_SQL = """
SELECT a.artifact_key,a.parent_id,a.person_id,a.payload_json
FROM artifacts a
WHERE a.kind='facts' AND a.candidate_key IS NULL
 AND (a.person_id IS NULL OR a.status='projected' OR a.person_id IN (SELECT value FROM json_each(?)))
 AND a.artifact_key NOT LIKE 'parent-facts:%'
 AND EXISTS (SELECT 1 FROM artifacts contact WHERE contact.parent_id=a.parent_id
             AND contact.kind='facts' AND contact.status='projected' AND contact.person_id IS NOT NULL)
ORDER BY a.parent_id,a.artifact_key
"""


class IdentityAudit:
    def __init__(self, *, db_path: Path = CANONICAL_DB):
        self.db_path = Path(db_path)

    def run(self) -> AuditReport:
        if not self.db_path.is_file():
            raise SystemExit(f'Deep Context database is missing: {self.db_path}')
        # Db.__init__ migrates older stores; auditing must never construct it.
        conn = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            names = {row['parent_id']: row['display_name'] for row in conn.execute(
                'SELECT parent_id,display_name FROM parents ORDER BY parent_id')}
            people = tuple(PersonRow(**dict(row)) for row in conn.execute('SELECT * FROM people'))
            verdicts = tuple(MergeVerdictRow(**dict(row)) for row in conn.execute('SELECT * FROM merge_verdicts'))
            issues = tuple(_OwnershipIssue(
                row['parent_id'], AuditCategory(row['category']), row['row_key'], row['person_id']
            ) for row in conn.execute(_OWNERSHIP_SQL))
            coverage = tuple(_FactCoverage(**dict(row)) for row in conn.execute(_COVERAGE_SQL))
            identifiers = tuple(PersonIdentifierRow(**dict(row))
                                for row in conn.execute('SELECT * FROM person_identifiers'))
            imported = tuple(PeopleRow.model_validate(json.loads(row['row_json']))
                             for row in conn.execute('SELECT row_json FROM imported_people'))
            aggregates = aggregate_people_from_rows(people, identifiers, imported)
            eligible_contacts = {row['person_id'] for row in conn.execute(
                "SELECT DISTINCT pi.person_id FROM person_identifiers pi "
                "JOIN person_sources ps USING(person_id) "
                "WHERE pi.kind IN ('email','phone') AND ps.source IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(MESSAGE_CHANNELS)),))}
            histories = tuple(_ArtifactHistory(
                row['artifact_key'], row['parent_id'], row['person_id'],
                FactHistory.from_payload(json.loads(row['payload_json'] or '{}'))
            ) for row in conn.execute(_HISTORY_SQL, (json.dumps(sorted(aggregates)),)))
        except sqlite3.Error as exc:
            raise SystemExit(f'Cannot audit Deep Context database: {exc}') from exc
        finally:
            conn.close()
        findings: dict[str, list[AuditFinding]] = defaultdict(list)
        for issue in issues:
            findings[issue.parent_id].append(AuditFinding(
                issue.category, _DETAILS[issue.category],
                (issue.person_id,) if issue.person_id else (), (issue.row_key,)))
        self._merge_findings(people, verdicts, findings)
        self._contact_findings(people, verdicts, coverage, eligible_contacts, findings)
        self._history_findings(histories, aggregates, findings)
        return AuditReport(len(names), tuple(
            ParentAudit(parent, names.get(parent), tuple(found))
            for parent, found in sorted(findings.items())
        ))

    @staticmethod
    def _merge_findings(
        people: tuple[PersonRow, ...], verdicts: tuple[MergeVerdictRow, ...],
        findings: dict[str, list[AuditFinding]],
    ) -> None:
        parents = {person.person_id: person.parent_id for person in people}
        edges = [(v.person_a, v.person_b) for v in verdicts if v.accepted]
        components = connected_components(sorted({person for edge in edges for person in edge}), edges)
        component = {person: index for index, members in enumerate(components) for person in members}
        for verdict in verdicts:
            if verdict.same_person or verdict.person_a not in component:
                continue
            if component[verdict.person_a] != component.get(verdict.person_b):
                continue
            for parent in sorted({parents[verdict.person_a], parents[verdict.person_b]}):
                findings[parent].append(AuditFinding(
                    AuditCategory.ACCEPTED_MERGE_CONFLICTS_WITH_NEGATIVE,
                    'An accepted merge path connects children with a negative verdict; review the evidence.',
                    (verdict.person_a, verdict.person_b)))

    @staticmethod
    def _contact_findings(
        people: tuple[PersonRow, ...], verdicts: tuple[MergeVerdictRow, ...],
        coverage: tuple[_FactCoverage, ...], eligible_contacts: set[str],
        findings: dict[str, list[AuditFinding]],
    ) -> None:
        children = defaultdict(list)
        for person in people:
            if not person.is_owner and not person.is_ghost:
                children[person.parent_id].append(person)
        facts = defaultdict(list)
        for fact in coverage:
            if fact.aligned:
                facts[fact.parent_id].append(fact)
        merged_at = defaultdict(list)
        for verdict in verdicts:
            if verdict.accepted:
                merged_at[verdict.person_a].append(verdict.updated_at)
                merged_at[verdict.person_b].append(verdict.updated_at)
        for parent, members in children.items():
            if len(members) < 2:
                continue
            owned = facts[parent]
            covered = {fact.person_id for fact in owned if fact.person_id}
            for person in members:
                times = merged_at[person.person_id]
                if any(fact.subject_key == mint_parent_id((person.person_id,))
                       and fact.person_id is None and times and all(times) and fact.synthesized_at
                       and fact.synthesized_at < min(times) for fact in owned):
                    covered.add(person.person_id)
            contacts = [person for person in members if person.person_id in eligible_contacts]
            missing = tuple(sorted(person.person_id for person in contacts if person.person_id not in covered))
            if len(contacts) > 1 and missing:
                category = AuditCategory.MIXED_CONTACT_FACTS if owned else AuditCategory.MISSING_CONTACT_FACTS
                findings[parent].append(AuditFinding(category, _DETAILS[category], missing))
            incompatible = sorted({person.person_id for first, second in combinations(members, 2)
                                   if _given_names_differ(first.display_name or '', second.display_name or '')
                                   for person in (first, second)})
            if incompatible:
                category = AuditCategory.INCOMPATIBLE_CHILD_NAMES
                findings[parent].append(AuditFinding(category, _DETAILS[category], tuple(incompatible)))

    @staticmethod
    def _history_findings(
        histories: tuple[_ArtifactHistory, ...], aggregates: frozenset[str],
        findings: dict[str, list[AuditFinding]],
    ) -> None:
        covered_messages: dict[str, set[str]] = defaultdict(set)
        covered_records: dict[str, set[str]] = defaultdict(set)
        for artifact in histories:
            if artifact.person_id is None or artifact.person_id in aggregates:
                continue
            for extraction in artifact.history.records:
                if extraction.record.facts is None or not extraction.record.facts.to_payload():
                    continue
                covered_messages[artifact.parent_id].update(m.fingerprint for m in extraction.record.messages)
                covered_records[artifact.parent_id].add(json.dumps(extraction.payload(), sort_keys=True))
        for artifact in histories:
            if artifact.person_id is not None and artifact.person_id not in aggregates:
                continue
            unassigned_messages = set()
            unassigned_records = 0
            for extraction in artifact.history.records:
                if extraction.record.facts is None or not extraction.record.facts.to_payload():
                    continue
                if json.dumps(extraction.payload(), sort_keys=True) in covered_records[artifact.parent_id]:
                    continue
                messages = {m.fingerprint for m in extraction.record.messages}
                unassigned_messages.update(messages - covered_messages[artifact.parent_id])
                unassigned_records += int(not messages)
            if unassigned_messages or unassigned_records:
                findings[artifact.parent_id].append(AuditFinding(
                    AuditCategory.UNASSIGNED_PARENT_HISTORY,
                    'Preserved parent history has unproved contact attribution; review or recollect its sources.',
                    row_keys=(artifact.artifact_key,), unassigned_messages=len(unassigned_messages),
                    unassigned_records=unassigned_records))


_DETAILS = {
    AuditCategory.MISSING_LINK_OWNER: 'Link has no candidate_people child owner.',
    AuditCategory.AMBIGUOUS_LINK_OWNER: 'Link has multiple child owners; this does not prove different people.',
    AuditCategory.CANDIDATE_OWNER_MISMATCH: 'Candidate owner disagrees with its link or person parent, or references a missing row.',
    AuditCategory.FACT_OWNER_MISMATCH: 'Fact owner disagrees with its artifact or person owner.',
    AuditCategory.ARTIFACT_OWNER_MISMATCH: 'Artifact owner disagrees with its person or link parent, or references a missing row.',
    AuditCategory.MISSING_CONTACT_FACTS: 'Multiple original contacts lack independent facts; identity is unproved.',
    AuditCategory.MIXED_CONTACT_FACTS: 'Existing facts do not prove independent coverage for these original contacts.',
    AuditCategory.INCOMPATIBLE_CHILD_NAMES: 'Different child given names require review; names alone do not prove a wrong merge.',
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only Deep Context identity and ownership audit.')
    parser.add_argument('--db', type=Path, default=CANONICAL_DB)
    args = parser.parse_args(argv)
    print(json.dumps(IdentityAudit(db_path=args.db).run().payload(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
