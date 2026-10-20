"""Exact selection actions reuse the source People and human-tag owners."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context_v2.db.queries_share import delete_parents, write_tags
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.memory.directory import Directory, Origin, Selection, Selector
from packs.memory.models import normalized
from packs.memory.sources import V2, readonly
from packs.memory.store import Store


@dataclass(frozen=True)
class ActionResult:
    selection: Selection
    applied: bool


def act(store: Store, source: V2, selection: Selection, *, selector: Selector | None = None,
        confirm: bool = False) -> ActionResult:
    directory = Directory(store)
    if selection.corpus != source.namespace or directory.corpus != source.namespace:
        raise ValueError('selection belongs to a different source corpus')
    ids = tuple(person.parent for person in selection.people)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('select unique explicit parent IDs')
    if selector is not None and selector.origin == Origin.SOURCE_NAME:
        raise ValueError('literal source-name editing is unsupported; edit the original contact with its source owner')
    with closing(readonly(source.database)) as conn:
        for person in selection.people:
            members = tuple(row[0] for row in conn.execute('SELECT candidate_id FROM current_parent WHERE parent_id=? ORDER BY candidate_id', (person.parent,)))
            if not members or members != person.members:
                raise ValueError('unknown or stale selection; enumerate again')
    if not confirm:
        return ActionResult(selection, False)
    if selector is not None and selector.origin in (Origin.TOPIC, Origin.SAVED_VALUE):
        sql, params = directory.matching_sql(selector)
        claims: list[int] = []
        for person in selection.people:
            claims.extend(row[0] for row in store.conn.execute('SELECT claim_id FROM (' + sql + ') WHERE parent=?', (*params, source.namespace + '#' + person.parent)))
        with store.conn:
            for claim_id in claims:
                if store.conn.execute('SELECT 1 FROM active_claims WHERE id=?', (claim_id,)).fetchone():
                    store.correct(claim_id, None, 'Human removed label')
    else:
        with closing(open_store(source.database)) as conn:
            if selector is None:
                delete_parents(conn, ids, now_iso())
            else:
                values = {normalized(selector.value), *(row[0] for row in store.conn.execute('SELECT alias FROM aliases WHERE topic=? AND confirmed=1', (normalized(selector.value),)))}
                tags = []
                for person in selection.people:
                    for row in conn.execute('SELECT t.candidate_id,t.tags,t.note FROM person_tags t JOIN current_parent p USING(candidate_id) WHERE p.parent_id=?', (person.parent,)):
                        kept = '|'.join(tag for tag in row['tags'].split('|') if normalized(tag) not in values)
                        tags.append((row['candidate_id'], kept, row['note'], now_iso()))
                write_tags(conn, tags)
        store.refresh(source, tuple(member for person in selection.people for member in person.members))
    return ActionResult(selection, True)
