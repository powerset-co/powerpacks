"""Restore provable original ownership in machine merges that contain a rejection.

Facts and their artifact keys retain original parent identities after absorption.
Recovery restores those fact owners and proven premerge source joins. A child
without a proven join gets an independent parent; ambiguous fact or candidate
ownership leaves the whole family untouched. Delete this recovery once no
install predates this merge fix.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from packs.ingestion.primitives.common.contact_fields import normalize_email, canonicalize_phone
from packs.ingestion.primitives.deep_context.db.store import Db, DbMaintenance, StoreError
from packs.ingestion.primitives.deep_context.db.models import HUMAN_DECISION_SOURCES, IdentityMachineProjection
from packs.ingestion.primitives.deep_context.db.identity_policy import SETTLING_HUMAN_ACTIONS
from packs.ingestion.primitives.deep_context.shared.common import slugify
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import connected_components


DATA_MIGRATION_VERSION = 1
_MACHINE_FIELDS = tuple(name for name in IdentityMachineProjection.__dataclass_fields__
                        if name.startswith(("machine_", "judgment_")))


@dataclass(frozen=True)
class MergeRepairReport:
    repaired: tuple[str, ...] = ()
    unresolved: tuple[tuple[str, str], ...] = ()
    backup_path: str = ''
    machine_verdicts_cleared: int = 0
    sibling_decisions_cleared: int = 0


@dataclass(frozen=True)
class _Repair:
    parent_id: str
    parents: tuple[tuple[str, str], ...]
    updates: tuple[tuple[str, str, str, str], ...]
    disabled: tuple[tuple[str, str], ...]
    sibling_decisions: tuple[str, ...]


def _artifact_owner(row, child_owner, link_owner, original_owner):
    if row['person_id'] is not None:
        return child_owner[row['person_id']]
    if row['candidate_key'] is not None:
        return link_owner[row['candidate_key']]
    original = row['artifact_key'].rsplit(':', 1)[-1]
    if original not in original_owner:
        raise ValueError('artifact has no original owner')
    return original_owner[original]


def _plan(conn, parent_id, verdicts):
    children = list(conn.execute('SELECT * FROM people WHERE parent_id=?', (parent_id,)))
    facts = list(conn.execute('SELECT * FROM facts WHERE parent_id=?', (parent_id,)))
    accepted_at = [row['updated_at'] for row in verdicts if row['accepted'] and row['updated_at']]
    if not accepted_at or any(not row['projected_at'] or row['projected_at'] >= min(accepted_at)
                              for row in facts if row['person_id'] is None):
        raise ValueError('parent facts are not proved to predate the merge')
    originals = {row['subject_key'] for row in facts if row['person_id'] is None}
    child_owner = {row['person_id']: minted for row in children
                   if (minted := mint_parent_id((row['person_id'],))) in originals}
    if set(child_owner.values()) != originals or parent_id not in originals:
        raise ValueError('original fact parents do not match founding children')
    tokens = defaultdict(set)
    for row in facts:
        if row['person_id'] is not None:
            continue
        owned = json.loads(row['facts_json'] or '{}').get('owned_identifiers', {})
        for kind, field, normalizer in [('email', 'emails', normalize_email), ('phone', 'phones', canonicalize_phone)]:
            for value in owned.get(field, []):
                normalized = normalizer(value)
                if normalized:
                    tokens[kind, normalized].add(row['subject_key'])
    for child in children:
        person = child['person_id']
        if person in child_owner:
            continue
        matches = set()
        for row in conn.execute('SELECT kind,normalized_value FROM person_identifiers WHERE person_id=?', (person,)):
            normalize = canonicalize_phone if row['kind'] == 'phone' else normalize_email
            matches.update(tokens.get((row['kind'], normalize(row['normalized_value'])), ()))
        if len(matches) != 1:
            source_owners = set()
            for row in conn.execute(
                'SELECT DISTINCT cp.row_key FROM candidate_people cp JOIN artifacts a '
                'ON a.candidate_key=cp.row_key WHERE cp.person_id=? '
                'AND a.projected_at<?', (person, min(accepted_at)),
            ):
                known = {child_owner[item['person_id']] for item in conn.execute(
                    'SELECT person_id FROM candidate_people WHERE row_key=?', (row['row_key'],),
                ) if item['person_id'] in child_owner}
                if len(known) == 1:
                    source_owners.update(known)
            matches = source_owners
        child_owner[person] = matches.pop() if len(matches) == 1 else mint_parent_id((person,))
    owner_by_original = {original: original for original in originals}
    if len(set(child_owner.values())) < 2:
        raise ValueError('original evidence does not permit separate parents')
    identifier_owners = defaultdict(set)
    for person, owner in child_owner.items():
        for row in conn.execute('SELECT kind,normalized_value FROM person_identifiers WHERE person_id=?', (person,)):
            normalize = canonicalize_phone if row['kind'] == 'phone' else normalize_email
            identifier_owners[row['kind'], normalize(row['normalized_value'])].add(owner)
    link_owner = {}
    for link in conn.execute('SELECT row_key FROM links WHERE parent_id=?', (parent_id,)):
        owners = {child_owner[row['person_id']] for row in conn.execute('SELECT person_id FROM candidate_people WHERE row_key=?', (link['row_key'],))}
        if not owners and link['row_key'].startswith(('candidate:email:', 'candidate:phone:')):
            _, kind, value = link['row_key'].split(':', 2)
            normalize = canonicalize_phone if kind == 'phone' else normalize_email
            owners = identifier_owners.get((kind, normalize(value)), set()).copy()
        if len(owners) != 1:
            raise ValueError('candidate has no unique child owner')
        link_owner[link['row_key']] = owners.pop()
    updates = [('people', 'person_id', person, owner) for person, owner in child_owner.items()]
    updates.extend(('links', 'row_key', key, owner) for key, owner in link_owner.items())
    updates.extend(('candidate_people', 'row_key', key, owner) for key, owner in link_owner.items())
    artifact_owners = {}
    for row in conn.execute('SELECT * FROM artifacts WHERE parent_id=?', (parent_id,)):
        owner = _artifact_owner(row, child_owner, link_owner, owner_by_original)
        artifact_owners[row['artifact_key']] = owner
        updates.append(('artifacts', 'artifact_key', row['artifact_key'], owner))
    for row in facts:
        if row['person_id']:
            owner = child_owner[row['person_id']]
        else:
            owner = owner_by_original[row['subject_key']]
        if artifact_owners.get(row['artifact_key']) != owner:
            raise ValueError('fact and artifact original owners disagree')
        updates.append(('facts', 'subject_key', row['subject_key'], owner))
    for table in ('research', 'guidance'):
        for row in conn.execute(f'SELECT * FROM {table} WHERE parent_id=?', (parent_id,)):
            evidence = set()
            if row['candidate_key'] in link_owner:
                evidence.add(link_owner[row['candidate_key']])
            if table == 'research' and row['artifact_key'] in artifact_owners:
                evidence.add(artifact_owners[row['artifact_key']])
            if len(evidence) != 1:
                raise ValueError(f'{table} has no unique original owner')
            updates.append((table, 'handle', row['handle'], evidence.pop()))
    names = {child_owner[row['person_id']]: row['display_name'] or row['person_id'] for row in children}
    disabled = tuple((r['person_a'], r['person_b']) for r in verdicts
                     if r['accepted'])
    links = list(conn.execute('SELECT * FROM links WHERE parent_id=?', (parent_id,)))
    direct = [row for row in links if row['decision_source'] in HUMAN_DECISION_SOURCES
              and row['decision_action'] in SETTLING_HUMAN_ACTIONS
              and row['decision_approved'] in ('yes', 'auto')]
    siblings = tuple(row['row_key'] for row in links if row['decision_source'] == 'sibling-settle'
                     and not any(link_owner[row['row_key']] == link_owner[other['row_key']]
                                 and row['decided_at'] == other['decided_at'] for other in direct))
    return _Repair(parent_id, tuple(sorted(names.items())), tuple(updates), disabled, siblings)


def repair_merged_parents(db: Db) -> MergeRepairReport:
    """Back up once, then restore only families whose ownership is fully proved."""
    plans = []
    unresolved = []
    with db.transaction() as conn:
        version = conn.execute("SELECT value FROM meta WHERE key='data_migration_version'").fetchone()
        if version and int(version['value']) >= DATA_MIGRATION_VERSION:
            return MergeRepairReport()
        parent_by_person = {r['person_id']: r['parent_id'] for r in conn.execute('SELECT person_id,parent_id FROM people')}
        verdicts = list(conn.execute('SELECT * FROM merge_verdicts'))
        accepted = [(r['person_a'], r['person_b']) for r in verdicts if r['accepted']]
        components = connected_components(sorted({p for edge in accepted for p in edge}), accepted)
        component_by_person = {person: index for index, group in enumerate(components) for person in group}
        contradictory = {parent_by_person[r['person_a']] for r in verdicts
                         if not r['same_person'] and parent_by_person[r['person_a']] == parent_by_person[r['person_b']]
                         and r['person_a'] in component_by_person
                         and component_by_person.get(r['person_b']) == component_by_person[r['person_a']]}
        for parent_id in sorted(contradictory):
            family_verdicts = [r for r in verdicts if parent_by_person[r['person_a']] == parent_id
                               and parent_by_person[r['person_b']] == parent_id]
            try:
                plans.append(_plan(conn, parent_id, family_verdicts))
            except ValueError as exc:
                unresolved.append((parent_id, str(exc)))
    backup = db.db_path.with_name(db.db_path.name + '.pre-merge-repair.bkup')
    if not backup.exists():
        DbMaintenance(db).backup_to(backup)
    machine_cleared = 0
    siblings_cleared = 0
    with db.transaction() as conn:
        conn.execute('BEGIN DEFERRED')
        conn.execute('PRAGMA defer_foreign_keys=ON')
        for plan in plans:
            for parent_id, name in plan.parents:
                conn.execute('INSERT OR IGNORE INTO parents(parent_id,public_identifier,display_name,display_slug) VALUES (?,?,?,?)', (parent_id, parent_id, name, slugify(name, parent_id)))
            for table, key, value, owner in plan.updates:
                conn.execute(f'UPDATE {table} SET parent_id=? WHERE {key}=?', (owner, value))
            conn.executemany('UPDATE merge_verdicts SET accepted=0 WHERE person_a=? AND person_b=?', plan.disabled)
            for key in plan.sibling_decisions:
                siblings_cleared += conn.execute(
                    'UPDATE links SET decision_action=NULL,decision_approved=NULL,decision_source=NULL,'
                    'decision_note=NULL,decided_at=NULL,replacement_url=NULL,replacement_public_identifier=NULL '
                    'WHERE row_key=?', (key,),
                ).rowcount
            for parent_id, _ in plan.parents:
                machine_cleared += conn.execute(
                    'UPDATE links SET ' + ','.join(f'{field}=NULL' for field in _MACHINE_FIELDS)
                    + ' WHERE parent_id=? AND (decision_source IS NULL OR decision_source NOT IN (?,?)) AND ('
                    + ' OR '.join(f'{field} IS NOT NULL' for field in _MACHINE_FIELDS) + ')',
                    (parent_id, *sorted(HUMAN_DECISION_SOURCES)),
                ).rowcount
                conn.execute('UPDATE people SET parent_slug=(SELECT display_slug FROM parents WHERE parents.parent_id=people.parent_id) WHERE parent_id=?', (parent_id,))
                conn.execute("UPDATE artifacts SET input_fingerprint=NULL WHERE parent_id=? AND artifact_key=?", (parent_id, 'dossier-parent:'+parent_id))
        conn.execute("INSERT INTO meta(key,value) VALUES ('data_migration_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(DATA_MIGRATION_VERSION),))
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise StoreError('merge recovery violates foreign keys')
    return MergeRepairReport(tuple(plan.parent_id for plan in plans), tuple(unresolved), str(backup), machine_cleared, siblings_cleared)
