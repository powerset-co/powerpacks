"""Separate derived SQLite: source revisions, vocabulary, claims and human corrections.

`claims.human` marks a claim a person wrote as a correction. A human *tag* from the source is an
ordinary projected claim (`human=0`, source key `tags:<member>`): the origin lives in the source key.
"""
from __future__ import annotations

import json
import sqlite3
import re
from collections.abc import Generator, Iterator
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso, short_hash
from packs.ingestion.primitives.common.contact_fields import identifier_phones
from packs.memory.models import Claim, Identity, IdentityMatch, ImportReport, OpenedClaim, Source, SourceRevision, normalized, date_bound, document
from packs.memory.progress import Progress, consume
from packs.memory.import_stage import import_progress
from packs.memory.projection import project, facts, fact_claims
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import collapse
from packs.memory.sources import V2, identity_key

_APPLICATION_ID = 0x50504D31
# Shared evidence does not equate different topics or scopes.
_SAME_CLAIM = """(old.fingerprint={claim}.fingerprint OR (
 old.kind={claim}.kind AND old.topic={claim}.topic AND old.scope={claim}.scope
 AND old.object={claim}.object AND old.start={claim}.start AND old.end={claim}.end
 AND json_extract(old.payload,'$.quote')<>''
 AND json_extract(old.payload,'$.quote')=json_extract({claim}.payload,'$.quote')))"""
# Rejecting an inferred label applies to that label, regardless of later wording or quote.
_CORRECTED_CLAIM = "(" + _SAME_CLAIM + """ OR (
 old.human=0 AND old.kind='tag' AND old.chunk_id IS NOT NULL
 AND {claim}.kind='tag' AND {claim}.chunk_id IS NOT NULL AND old.topic={claim}.topic))"""
_DDL = """
CREATE TABLE context(content TEXT NOT NULL);
CREATE TABLE phrases(source_id INTEGER NOT NULL REFERENCES sources(id), phrase TEXT NOT NULL, PRIMARY KEY(phrase,source_id));
CREATE TABLE discovery(key TEXT PRIMARY KEY, chunk_id INTEGER NOT NULL REFERENCES chunks(id));
CREATE TABLE identities(
 namespace TEXT NOT NULL, member TEXT NOT NULL, parent TEXT NOT NULL, name TEXT NOT NULL,
 present INTEGER NOT NULL CHECK(present IN (0,1)), PRIMARY KEY(namespace,member));
CREATE INDEX identities_parent ON identities(parent);
CREATE TABLE identifiers(
 namespace TEXT NOT NULL, member TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(namespace,member,value), FOREIGN KEY(namespace,member) REFERENCES identities(namespace,member));
CREATE INDEX identifiers_value ON identifiers(value);
CREATE TABLE sources(
 id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, key TEXT NOT NULL, member TEXT NOT NULL,
 citation TEXT NOT NULL, hash TEXT NOT NULL, error TEXT NOT NULL, present INTEGER NOT NULL CHECK(present IN (0,1)),
 UNIQUE(namespace,key), FOREIGN KEY(namespace,member) REFERENCES identities(namespace,member));
CREATE INDEX sources_member ON sources(namespace,member);
CREATE TABLE revisions(
 source_id INTEGER NOT NULL REFERENCES sources(id), hash TEXT NOT NULL, content TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(source_id,hash));
CREATE TABLE reads(
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), error TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE topics(name TEXT PRIMARY KEY NOT NULL);
CREATE TABLE aliases(
 alias TEXT NOT NULL, topic TEXT NOT NULL REFERENCES topics(name), confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
 PRIMARY KEY(alias,topic));
CREATE TABLE chunks(
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), source_hash TEXT NOT NULL,
 position INTEGER NOT NULL, fingerprint TEXT NOT NULL, response TEXT NOT NULL, error TEXT NOT NULL, UNIQUE(source_id,source_hash,position,fingerprint),
 FOREIGN KEY(source_id,source_hash) REFERENCES revisions(source_id,hash));
CREATE TABLE claims(
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, source_hash TEXT NOT NULL, chunk_id INTEGER REFERENCES chunks(id),
 fingerprint TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)), text TEXT NOT NULL, document_hash TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('statement','mention','discussion','work','relationship','lp','preference','tenure','tag')),
 topic TEXT NOT NULL REFERENCES topics(name), scope TEXT NOT NULL, object TEXT NOT NULL,
 start TEXT NOT NULL, end TEXT NOT NULL, human INTEGER NOT NULL CHECK(human IN (0,1)),
 active INTEGER NOT NULL CHECK(active IN (0,1)),
 FOREIGN KEY(source_id,source_hash) REFERENCES revisions(source_id,hash));
CREATE UNIQUE INDEX claims_identity ON claims(source_id,source_hash,ifnull(chunk_id,0),fingerprint,human);
CREATE INDEX claims_source ON claims(source_id,active);
CREATE INDEX claims_fingerprint ON claims(fingerprint);
CREATE TABLE corrections(
 id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL REFERENCES claims(id), replacement_id INTEGER REFERENCES claims(id),
 reason TEXT NOT NULL CHECK(length(trim(reason))>0), created_at TEXT NOT NULL);
CREATE TABLE embeddings(
 model TEXT NOT NULL, hash TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(model,hash));
CREATE VIRTUAL TABLE lexical USING fts5(text,topic,scope,object);
CREATE TRIGGER claims_insert AFTER INSERT ON claims WHEN new.active=1 BEGIN
 INSERT INTO lexical(rowid,text,topic,scope,object) VALUES(new.id,new.text,new.topic,new.scope,new.object); END;
CREATE TRIGGER claims_deactivate AFTER UPDATE OF active ON claims WHEN new.active=0 BEGIN
 DELETE FROM lexical WHERE rowid=new.id; END;
CREATE TRIGGER claims_activate AFTER UPDATE OF active ON claims WHEN new.active=1 AND old.active=0 BEGIN
 INSERT INTO lexical(rowid,text,topic,scope,object) VALUES(new.id,new.text,new.topic,new.scope,new.object); END;
"""
_ACTIVE_CLAIMS = """
CREATE TEMP VIEW active_claims AS
 SELECT c.*,i.parent,i.name,s.citation,s.error,s.key source_key FROM claims c JOIN sources s ON s.id=c.source_id
 JOIN identities i ON i.namespace=s.namespace AND i.member=s.member
 WHERE c.active=1 AND s.present=1 AND i.present=1 AND NOT EXISTS(
 SELECT 1 FROM corrections r JOIN claims old ON old.id=r.claim_id JOIN sources os ON os.id=old.source_id
 JOIN identities oi ON oi.namespace=os.namespace AND oi.member=os.member
 WHERE c.human=0 AND oi.parent=i.parent AND """ + _CORRECTED_CLAIM.format(claim="c") + ");"


class Store:
    def __init__(self, path: Path, *, create: bool = False) -> None:
        path = path.resolve()
        if not create and not path.is_file():
            raise ValueError("derived memory does not exist; import first")
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("normalized", 1, normalized, deterministic=True)
        self.conn.create_function("date_lower", 1, date_bound, deterministic=True)
        self.conn.create_function("date_upper", 1, lambda value: date_bound(value, upper=True), deterministic=True)
        application_id = self.conn.execute("PRAGMA application_id").fetchone()[0]
        tables = self.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        if application_id != _APPLICATION_ID and tables:
            self.conn.close()
            raise ValueError("refusing to modify a non-memory SQLite database")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA cache_size=-8192")
        self.conn.execute("PRAGMA temp_store=FILE")
        if not tables:
            self.conn.executescript(_DDL)
            self.conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            self.conn.commit()
        if not any(row['name'] == 'grouping_value' for row in self.conn.execute('PRAGMA table_xinfo(claims)')):
            self.conn.execute("ALTER TABLE claims ADD COLUMN grouping_value TEXT GENERATED ALWAYS AS "
                              "(normalized(CASE WHEN object<>'' THEN object ELSE text END)) VIRTUAL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS claims_grouping ON claims(grouping_value,source_id,id) "
                          "WHERE active=1 AND chunk_id IS NULL AND human=0")
        self.conn.commit()
        self.conn.executescript(_ACTIVE_CLAIMS)

    def close(self) -> None:
        self.conn.close()

    def import_source(self, adapter: V2) -> ImportReport:
        return consume(self.import_progress(adapter))

    def import_progress(self, adapter: V2, *, first: tuple[str, ...] = ()) -> Generator[Progress, None, ImportReport]:
        return import_progress(self, adapter, first=first)

    def refresh(self, adapter: V2, members: tuple[str, ...]) -> None:
        """Re-read just these members after an action; a member no longer in the source is marked absent.
        A full import streams the whole store again, which an action on two people does not need."""
        namespace = adapter.namespace
        seen = set()
        try:
            for identity in adapter.identities(members):
                self._identity(namespace, identity)
                seen.add(identity.member)
            for source in adapter.sources(members):
                self._import_source(namespace, source)
            gone = tuple(member for member in members if member not in seen)
            if gone:
                marks = ",".join("?" for _ in gone)
                self.conn.execute("UPDATE identities SET present=0 WHERE namespace=? AND member IN (" + marks + ")", (namespace, *gone))
                self.conn.execute("UPDATE sources SET present=0 WHERE namespace=? AND member IN (" + marks + ")", (namespace, *gone))
                self.conn.execute("UPDATE claims SET active=0 WHERE human=0 AND source_id IN (SELECT id FROM sources WHERE present=0)")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _import_source(self, namespace: str, source: Source) -> tuple[bool, str]:
        """Project and store one saved artifact; returns (changed, read error)."""
        projected: tuple[Claim, ...] = ()
        if not source.error:
            try:
                projected = project(source.key, source.content)
            except (ValueError, KeyError, TypeError, AttributeError):
                source = replace(source, error="invalid saved structured artifact")
        return self._source(namespace, source, projected), source.error

    def _families(self) -> None:
        # Reuse synthesis collapse semantics per family, independently of source hashes/merges.
        for parent in self.conn.execute(
            "SELECT i.parent FROM identities i JOIN sources s USING(namespace,member) "
            "JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash "
            "WHERE i.present=1 AND s.present=1 AND s.key LIKE 'facts:%' GROUP BY i.parent HAVING count(*)>1"
        ):
            rows = list(self.conn.execute(
                "SELECT s.id,r.content FROM sources s JOIN identities i USING(namespace,member) "
                "JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash "
                "WHERE i.parent=? AND i.present=1 AND s.present=1 AND s.key LIKE 'facts:%' ORDER BY i.member", (parent[0],)))
            allowed = {replace(claim, topic=normalized(claim.topic)).fingerprint()
                       for claim in fact_claims(collapse([facts(row['content']) for row in rows]))}
            with self.conn:
                for row in rows:
                    for claim in self.conn.execute("SELECT id,fingerprint FROM claims WHERE source_id=? AND active=1 AND human=0 AND chunk_id IS NULL", (row['id'],)):
                        if claim['fingerprint'] not in allowed:
                            self.conn.execute("UPDATE claims SET active=0 WHERE id=?", (claim['id'],))

    def _identity(self, namespace: str, identity: Identity) -> None:
        parent = namespace + "#" + identity.parent
        self.conn.execute(
            "INSERT INTO identities VALUES(?,?,?,?,1) ON CONFLICT(namespace,member) "
            "DO UPDATE SET parent=excluded.parent,name=excluded.name,present=1",
            (namespace, identity.member, parent, identity.name))
        self.conn.execute("DELETE FROM identifiers WHERE namespace=? AND member=?", (namespace, identity.member))
        keys = {*identity.identifiers, identity_key(identity.member), identity_key(identity.parent), identity_key(parent)}
        for key in keys:
            self.conn.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?,?)", (namespace, identity.member, key))

    def _source(self, namespace: str, source: Source, projected: tuple[Claim, ...]) -> bool:
        old = self.conn.execute("SELECT id,hash FROM sources WHERE namespace=? AND key=?", (namespace, source.key)).fetchone()
        fingerprint = short_hash(source.content, n=64) if not source.error else (old["hash"] if old else "")
        self.conn.execute(
            "INSERT INTO sources(namespace,key,member,citation,hash,error,present) VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(namespace,key) DO UPDATE SET member=excluded.member,citation=excluded.citation,"
            "hash=excluded.hash,error=excluded.error,present=1",
            (namespace, source.key, source.member, source.citation, fingerprint, source.error))
        source_id = self.conn.execute("SELECT id FROM sources WHERE namespace=? AND key=?", (namespace, source.key)).fetchone()[0]
        if source.key.startswith("name:") and not source.error:
            self.conn.execute("DELETE FROM phrases WHERE source_id=?", (source_id,))
            tokens = re.findall(r"[^\W_]+", source.content.casefold())
            # Every 1-3 token run of the name, from either end: "SoF Jordan" and "Jordan SoF" both resolve.
            for start in range(len(tokens)):
                for end in range(start + 1, min(len(tokens), start + 3) + 1):
                    phrase = " ".join(tokens[start:end])
                    if not all(token.isnumeric() for token in tokens[start:end]):
                        self.conn.execute("INSERT OR IGNORE INTO phrases VALUES(?,?)", (source_id, phrase))
        if source.error:
            self.conn.execute("INSERT INTO reads(source_id,error,created_at) VALUES(?,?,?)", (source_id, source.error, now_iso()))
            return False
        self.conn.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", (source_id, fingerprint, source.content, now_iso()))
        changed = old is None or old["hash"] != fingerprint
        if changed:
            self.conn.execute("UPDATE claims SET active=0 WHERE source_id=? AND human=0", (source_id,))
        for claim in projected:
            self.add_claim(source_id, fingerprint, None, claim, human=False)
        return changed

    def lookup(self, value: str) -> list[IdentityMatch]:
        keys = [identity_key(value)]
        keys.extend("phone:" + key for key in identifier_phones([value]))
        return [IdentityMatch(row["parent"], row["name"]) for row in self.conn.execute(
            "SELECT i.parent,min(i.name) AS name FROM identifiers k JOIN identities i USING(namespace,member) "
            "WHERE k.value IN (" + ",".join("?" for _ in keys) + ") AND i.present=1 GROUP BY i.parent ORDER BY i.parent", keys)]

    def topic(self, value: str) -> str:
        name = normalized(value)
        matches = {row[0] for row in self.conn.execute(
            "SELECT topic FROM aliases WHERE alias=? AND confirmed=1", (name,))}
        if len(matches) > 1:
            raise ValueError("ambiguous topic alias")
        return next(iter(matches), name)

    def confirm_alias(self, alias: str, topic: str) -> None:
        alias, topic = normalized(alias), normalized(topic)
        old = self.conn.execute("SELECT topic FROM aliases WHERE alias=? AND confirmed=1", (alias,)).fetchone()
        if old is not None and old[0] != topic:
            raise ValueError("conflicting confirmed topic alias")
        with self.conn:
            self.conn.execute("INSERT INTO aliases VALUES(?,?,1) ON CONFLICT(alias,topic) DO UPDATE SET confirmed=1",
                              (normalized(alias), normalized(topic)))

    def add_claim(self, source_id: int, source_hash: str, chunk_id: int | None, claim: Claim, *, human: bool) -> int:
        topic = normalized(claim.topic)
        claim = replace(claim, topic=topic)
        self.conn.execute("INSERT OR IGNORE INTO topics VALUES(?)", (topic,))
        for alias in claim.aliases:
            self.conn.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,0)", (normalized(alias), topic))
        # Aliases are suggestions only. No semantic merge runs here.
        fingerprint = claim.fingerprint()
        self.conn.execute(
            "INSERT OR IGNORE INTO claims(source_id,source_hash,chunk_id,fingerprint,payload,text,document_hash,kind,topic,scope,object,start,end,human,active) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (source_id, source_hash, chunk_id, fingerprint, claim.serialized(), claim.text, short_hash(document(claim), n=64), claim.kind, topic,
             normalized(claim.scope), normalized(claim.object), claim.start, claim.end, int(human)))
        claim_id = int(self.conn.execute(
            "SELECT id FROM claims WHERE source_id=? AND source_hash=? AND chunk_id IS ? AND fingerprint=? AND human=? ORDER BY id DESC LIMIT 1",
            (source_id, source_hash, chunk_id, fingerprint, int(human))).fetchone()[0])
        self.conn.execute("UPDATE claims SET active=1 WHERE id=?", (claim_id,))
        self.conn.execute("UPDATE claims SET active=0 WHERE id=? AND NOT EXISTS(SELECT 1 FROM active_claims WHERE id=?)",
                          (claim_id, claim_id))
        return claim_id

    def correct(self, claim_id: int, replacement: Claim | None, reason: str) -> int | None:
        with self.conn:
            old = self.conn.execute("SELECT * FROM active_claims WHERE id=?", (claim_id,)).fetchone()
            if old is None:
                raise ValueError("claim is not current")
            if replacement is not None and replacement.fingerprint() == old["fingerprint"]:
                raise ValueError("replacement is identical to the current claim")
            replacement_id = None
            if replacement is not None:
                replacement_id = self.add_claim(old["source_id"], old["source_hash"], None, replacement, human=True)
            self.conn.execute(
                "UPDATE claims SET active=0 WHERE id IN (SELECT c.id FROM claims c JOIN claims old ON old.id=? "
                "JOIN sources s ON s.id=c.source_id JOIN identities i ON i.namespace=s.namespace AND i.member=s.member "
                "WHERE i.parent=? AND (c.human=0 OR c.id=old.id) AND " + _CORRECTED_CLAIM.format(claim="c") + ")",
                (claim_id, old["parent"]))
            self.conn.execute("INSERT INTO corrections(claim_id,replacement_id,reason,created_at) VALUES(?,?,?,?)",
                              (claim_id, replacement_id, reason, now_iso()))
        return replacement_id

    def sources(self) -> Iterator[SourceRevision]:
        for row in self.conn.execute(
            "SELECT s.id,s.hash,r.content FROM sources s JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash "
            "WHERE s.present=1 AND s.error='' AND s.key NOT LIKE 'name:%' ORDER BY s.id"):
            yield SourceRevision(row["id"], row["hash"], row["content"])

    def open(self, claim_id: int) -> OpenedClaim:
        row = self.conn.execute(
            "SELECT c.payload,c.human,c.active,r.content,s.citation,s.error,c.source_hash FROM claims c "
            "JOIN sources s ON s.id=c.source_id JOIN revisions r ON r.source_id=c.source_id AND r.hash=c.source_hash WHERE c.id=?",
            (claim_id,)).fetchone()
        if row is None:
            raise ValueError("unknown claim")
        return OpenedClaim(Claim.parse(json.loads(row["payload"])), bool(row["human"]), bool(row["active"]),
                           row["citation"], row["source_hash"], row["content"], row["error"])
