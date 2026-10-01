"""Restore original ownership in contradictory or historically mixed parents.

Facts and their artifact keys retain original parent identities after absorption.
Recovery restores those fact owners and proven premerge source joins. A child
without a proven join gets an independent parent; ambiguous fact or candidate
ownership leaves the whole family untouched. Delete this recovery once no
install predates this merge fix.
"""
from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import combinations

from packs.ingestion.primitives.common.contact_fields import normalize_email, canonicalize_phone
from packs.ingestion.primitives.deep_context.db.store import Db, DbMaintenance, StoreError
from packs.ingestion.primitives.deep_context.db.models import HUMAN_DECISION_SOURCES, IdentityMachineProjection
from packs.ingestion.primitives.deep_context.db.identity_policy import SETTLING_HUMAN_ACTIONS
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory
from packs.ingestion.primitives.deep_context.shared.common import slugify
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import connected_components, names_compatible


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


def _repair_merged_parents(db: Db) -> MergeRepairReport:
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


HISTORICAL_MERGE_MIGRATION = 3


def _repair_historical_merges(db: Db, histories: dict[str, FactHistory]) -> MergeRepairReport:
    """Restore separate original evidence; corrected merging can reassess it.

    Only competing profile families with incompatible names or preserved
    original fact parents qualify. Missing child evidence or ambiguous candidate
    ownership leaves the entire family unchanged.
    """
    version = db.query("SELECT value FROM meta WHERE key='data_migration_version'")
    if version and int(version[0]['value']) >= HISTORICAL_MERGE_MIGRATION:
        return MergeRepairReport()
    plans = []
    unresolved = []
    for family in db.query("SELECT parent_id FROM links WHERE kind='pub' GROUP BY parent_id HAVING COUNT(DISTINCT public_identifier)>1"):
        parent = family['parent_id']
        children = db.query('SELECT person_id,display_name FROM people WHERE parent_id=?', (parent,))
        owners = {row['person_id']: mint_parent_id((row['person_id'],)) for row in children}
        originals = {row['subject_key'] for row in db.query('SELECT subject_key FROM facts WHERE parent_id=? AND person_id IS NULL', (parent,))}
        incompatible = any(not names_compatible(a['display_name'], b['display_name']) for a, b in combinations(children, 2))
        if not incompatible and len(originals.intersection(owners.values())) < 2:
            continue
        if any(person not in histories or not histories[person].records for person in owners):
            unresolved.append((parent, 'original child facts missing'))
            continue
        links = {}
        for row in db.query('SELECT row_key FROM links WHERE parent_id=?', (parent,)):
            matches = {owners[r['person_id']] for r in db.query('SELECT person_id FROM candidate_people WHERE row_key=?', (row['row_key'],)) if r['person_id'] in owners}
            if len(matches) != 1:
                break
            links[row['row_key']] = matches.pop()
        else:
            artifacts = db.query('SELECT * FROM artifacts WHERE parent_id=?', (parent,))
            research = db.query('SELECT handle,candidate_key FROM research WHERE parent_id=?', (parent,))
            guidance = db.query('SELECT handle,candidate_key FROM guidance WHERE parent_id=?', (parent,))
            if any(row['candidate_key'] not in links for row in (*research, *guidance)):
                unresolved.append((parent, 'research or guidance has no unique original owner'))
                continue
            plans.append((parent, children, owners, links, artifacts, research, guidance))
            continue
        unresolved.append((parent, 'candidate has no unique child owner'))
    backup = db.db_path.with_name(db.db_path.name + '.pre-historical-merge-repair.bkup')
    if (plans or unresolved) and not backup.exists():
        DbMaintenance(db).backup_to(backup)
    machine_cleared = siblings_cleared = 0
    with db.transaction() as conn:
        conn.execute('BEGIN DEFERRED')
        conn.execute('PRAGMA defer_foreign_keys=ON')
        for parent, children, owners, links, artifacts, research, guidance in plans:
            for child in children:
                owner = owners[child['person_id']]
                name = histories[child['person_id']].facts.canonical_name or child['display_name'] or child['person_id']
                conn.execute('INSERT OR IGNORE INTO parents(parent_id,public_identifier,display_name,display_slug) VALUES (?,?,?,?)', (owner, owner, name, slugify(name, owner)))
                conn.execute('UPDATE parents SET display_name=?,display_slug=? WHERE parent_id=?', (name, slugify(name, owner), owner))
            # The paid payload remains in artifacts and the backup. Its mixed
            # facts projection cannot safely identify any individual child.
            conn.execute('DELETE FROM facts WHERE parent_id=?', (parent,))
            for row in artifacts:
                owner = owners.get(row['person_id']) or links.get(row['candidate_key'])
                if owner:
                    conn.execute('UPDATE artifacts SET parent_id=? WHERE artifact_key=?', (owner, row['artifact_key']))
                else:
                    conn.execute("UPDATE artifacts SET status='failed',error='mixed ownership before merge repair',input_fingerprint=NULL WHERE artifact_key=?", (row['artifact_key'],))
            for person, owner in owners.items():
                conn.execute('UPDATE people SET parent_id=?,parent_slug=(SELECT display_slug FROM parents WHERE parent_id=?),display_name=? WHERE person_id=?', (owner, owner, histories[person].facts.canonical_name or next(row['display_name'] for row in children if row['person_id'] == person), person))
            for key, owner in links.items():
                conn.execute('UPDATE links SET parent_id=? WHERE row_key=?', (owner, key))
                conn.execute('UPDATE candidate_people SET parent_id=? WHERE row_key=?', (owner, key))
            for table, rows in (('research', research), ('guidance', guidance)):
                for row in rows:
                    conn.execute(f'UPDATE {table} SET parent_id=? WHERE handle=?', (links[row['candidate_key']], row['handle']))
            conn.execute('UPDATE merge_verdicts SET accepted=0 WHERE person_a IN (SELECT person_id FROM people WHERE parent_id IN (' + ','.join('?' for _ in owners) + ')) AND person_b IN (SELECT person_id FROM people WHERE parent_id IN (' + ','.join('?' for _ in owners) + '))', (*owners.values(), *owners.values()))
            direct = [row for row in conn.execute('SELECT * FROM links WHERE row_key IN (SELECT value FROM json_each(?))', (json.dumps(tuple(links)),))
                      if row['decision_source'] in HUMAN_DECISION_SOURCES
                      and row['decision_action'] in SETTLING_HUMAN_ACTIONS
                      and row['decision_approved'] in ('yes', 'auto')]
            for key, owner in links.items():
                row = conn.execute('SELECT decision_source,decided_at FROM links WHERE row_key=?', (key,)).fetchone()
                if row['decision_source'] == 'sibling-settle' and not any(links[other['row_key']] == owner and other['decided_at'] == row['decided_at'] for other in direct):
                    siblings_cleared += conn.execute('UPDATE links SET decision_action=NULL,decision_approved=NULL,decision_source=NULL,decision_note=NULL,decided_at=NULL,replacement_url=NULL,replacement_public_identifier=NULL WHERE row_key=?', (key,)).rowcount
                machine_cleared += conn.execute('UPDATE links SET ' + ','.join(f'{field}=NULL' for field in _MACHINE_FIELDS) + ',authoritative_detach=0 WHERE row_key=? AND (decision_source IS NULL OR decision_source NOT IN (?,?))', (key, *sorted(HUMAN_DECISION_SOURCES))).rowcount
            shared = defaultdict(set)
            for person in owners:
                facts = histories[person].facts
                for kind, normalizer in (('emails', normalize_email), ('phones', canonicalize_phone)):
                    for value in getattr(facts.owned_identifiers, kind):
                        shared[kind, normalizer(value)].add(person)
            for person, owner in owners.items():
                history = histories[person]
                payload = history.payload()
                facts = history.facts
                owned = replace(facts.owned_identifiers, **{kind: tuple(value for value in getattr(facts.owned_identifiers, kind) if len(shared[kind, normalizer(value)]) == 1) for kind, normalizer in (('emails', normalize_email), ('phones', canonicalize_phone))})
                facts = replace(facts, owned_identifiers=owned)
                # Raw records stay in their original files; the active artifact
                # carries only identifiers whose ownership remains unambiguous.
                records = [item.payload() for item in history.records]
                for record in records:
                    if record.get('facts'):
                        record['facts']['owned_identifiers'] = owned.to_payload()
                payload['records'] = records
                payload['facts'] = facts.to_payload()
                key = 'facts:' + owner
                conn.execute('UPDATE artifacts SET artifact_key=? WHERE artifact_key=?', (key + ':pre-merge-repair', key))
                serialized = json.dumps(payload)
                fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
                path = str(db.db_path.parent / 'facts' / (person + '.jsonl'))
                conn.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,person_id,path,content_fingerprint,status,payload_json,projected_at) VALUES (?,'facts',?,NULL,?,?,'projected',?,?)", (key, owner, path, fingerprint, serialized, history.records[-1].record.updated_at))
                worth = facts.network_worth
                conn.execute('INSERT INTO facts(subject_key,parent_id,person_id,artifact_key,machine_worth,machine_worth_reason,confidence,is_owner,facts_json,projected_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (owner, owner, None, key, worth.decision if worth else None, worth.reason if worth else None, facts.confidence, bool(facts.is_owner), json.dumps(facts.to_payload()), history.records[-1].record.updated_at))
        conn.execute("INSERT INTO meta(key,value) VALUES ('data_migration_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(HISTORICAL_MERGE_MIGRATION),))
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise StoreError('historical merge repair violates foreign keys')
    return MergeRepairReport(tuple(plan[0] for plan in plans), tuple(unresolved), str(backup) if plans or unresolved else '', machine_cleared, siblings_cleared)
