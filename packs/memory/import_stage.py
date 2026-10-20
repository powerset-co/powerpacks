"""Stream and project saved sources; only a complete scan marks missing data absent."""
from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from packs.memory.models import ImportReport
from packs.memory.progress import Progress, Step, retry_first
from packs.memory.sources import V2

if TYPE_CHECKING:
    from packs.memory.store import Store


def import_progress(store: Store, adapter: V2, *, first: tuple[str, ...] = ()) -> Generator[Progress, None, ImportReport]:
    namespace = adapter.namespace
    other = store.conn.execute("SELECT 1 FROM identities WHERE namespace<>? LIMIT 1", (namespace,)).fetchone()
    if other:
        raise ValueError("one source corpus per derived database; use a separate database")
    report = ImportReport()
    store.conn.execute("DELETE FROM context")
    store.conn.execute("INSERT INTO context VALUES(?)", (adapter.owner_context(),))
    store.conn.executescript("DROP TABLE IF EXISTS temp.seen_identities; DROP TABLE IF EXISTS temp.seen_sources; "
                           "CREATE TEMP TABLE seen_identities(member TEXT PRIMARY KEY); "
                           "CREATE TEMP TABLE seen_sources(key TEXT PRIMARY KEY);")
    try:
        for identity in adapter.identities():
            store._identity(namespace, identity)
            store.conn.execute("INSERT INTO seen_identities VALUES(?)", (identity.member,))
            report.members += 1
            if report.members % 250 == 0:
                store.conn.commit()
        store.conn.commit()
        counts = adapter.inventory()["counts"]
        assert isinstance(counts, dict)
        total = sum(int(counts[name]) for name in ("candidates", "facts", "person_tags", "current_profile"))
        for source in retry_first(adapter.sources, lambda source: source.key, first):
            report.sources += 1
            try:
                changed, error = store._import_source(namespace, source)
            except Exception as exc:
                store.conn.rollback()
                store.conn.execute("INSERT INTO seen_sources VALUES(?)", (source.key,))
                report.failed_reads += 1
                yield Progress(Step.IMPORT, report.sources, total, source.key, str(exc))
                continue
            report.failed_reads += bool(error)
            report.changed += changed
            store.conn.execute("INSERT INTO seen_sources VALUES(?)", (source.key,))
            store.conn.commit()
            yield Progress(Step.IMPORT, report.sources, total, source.key, error)
        # Only a complete enumeration is evidence that a prior source/member disappeared.
        store.conn.execute("UPDATE identities SET present=0 WHERE namespace=? AND member NOT IN (SELECT member FROM seen_identities)", (namespace,))
        store.conn.execute("UPDATE sources SET present=0 WHERE namespace=? AND key NOT IN (SELECT key FROM seen_sources)", (namespace,))
        store.conn.execute("UPDATE claims SET active=0 WHERE human=0 AND source_id IN (SELECT id FROM sources WHERE present=0)")
        store.conn.commit()
        store._families()
        report.parents = store.conn.execute("SELECT count(DISTINCT parent) FROM identities WHERE present=1").fetchone()[0]
        without = store.conn.execute("SELECT count(*) FROM (SELECT DISTINCT parent FROM identities WHERE present=1 "
                                    "EXCEPT SELECT parent FROM active_claims)").fetchone()[0]
        report.unavailable.append(f"{without} canonical identities have no searchable facts")
        yield Progress(Step.IMPORT, report.sources, report.sources, "")
    except BaseException:
        store.conn.rollback()
        raise
    return report
