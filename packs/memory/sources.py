"""Read-only current SQLite people, facts, profiles and human tags; no raw messages."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_name_key, normalize_phone
from packs.ingestion.primitives.deep_context_v2.db.queries_realize import profile_keys
from packs.ingestion.primitives.deep_context_v2.db.queries_review import identifiers
from packs.ingestion.primitives.deep_context_v2.db.schema import SCHEMA_VERSION
from packs.ingestion.primitives.deep_context_v2.db.store import store_path
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import CACHE_RELATIVE_DIR, profile_from_record
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.memory.models import Identity, Source, object_dict, string, strings


def readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-4096")
    conn.execute("PRAGMA temp_store=FILE")
    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if version is None or version[0] != str(SCHEMA_VERSION):
        conn.close()
        raise ValueError(f"source requires current Deep Context schema {SCHEMA_VERSION}")
    conn.execute("CREATE TEMP VIEW current_parent AS SELECT * FROM main.current_parent "
                 "WHERE parent_id NOT IN (SELECT parent_id FROM deleted_parents)")
    conn.execute("PRAGMA query_only=ON")
    return conn


# person_labels probabilities that become searchable labels, in the owner's words.
LABELS = {
    "is_family": "family", "is_close_friend": "close friend", "is_classmate": "classmate",
    "is_coworker_past": "past coworker", "is_coworker_current": "current coworker", "is_investor": "investor",
    "is_recruiter": "recruiter", "is_founder": "founder", "is_mentor_or_advisor": "mentor or advisor",
    "is_service_provider": "service provider", "is_neighbor_or_local": "neighbor or local", "is_client": "client",
    "is_vendor_or_partner": "vendor or partner",
}


def _only(members: tuple[str, ...]) -> str:
    return "WHERE candidate_id IN (" + ",".join("?" for _ in members) + ")" if members else ""


def identity_key(value: str) -> str:
    if "@" in value:
        return normalize_email(value)
    if re.fullmatch(r"[+\d\s().-]+", value) and any(char.isdigit() for char in value):
        return normalize_phone(value)
    return normalize_name_key(value)


@dataclass(frozen=True)
class V2:
    root: Path

    @property
    def database(self) -> Path:
        return store_path(self.root)

    @cached_property
    def namespace(self) -> str:
        """`dc:<store id>`: the store's own id from its meta table, stable across moves and copies."""
        with closing(readonly(self.database)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='store_id'").fetchone()
        if row is None:
            raise ValueError("source has no store id yet; open it with Deep Context once (any People command)")
        return "dc:" + str(row[0])

    @property
    def location(self) -> str:
        """Where the store lives, for citations a person can open."""
        return self.database.resolve().as_uri()

    def identities(self, members: tuple[str, ...] = ()) -> Iterator[Identity]:
        with closing(readonly(self.database)) as conn:
            profiles = profile_keys(conn)
            for row in conn.execute(
                "SELECT c.candidate_id,c.display_name,p.parent_id,f.facts_json "
                "FROM candidates c JOIN current_parent p USING(candidate_id) "
                "LEFT JOIN facts f USING(candidate_id) " + _only(members) + " ORDER BY c.candidate_id", members
            ):
                names = [row["display_name"]]
                canonical = row["display_name"]
                if row["facts_json"] is not None:
                    facts = object_dict(json.loads(row["facts_json"]))
                    canonical = string(facts["canonical_name"]) or canonical
                    names.append(canonical)
                    names.extend(strings(facts["aliases"]))
                keys = [identity_key(name) for name in names if name]
                keys.extend(identity_key(item.normalized_value) for item in identifiers(conn, [row["candidate_id"]]))
                profile = profiles.get(row["parent_id"], "")
                if profile and not profile.startswith("synthetic:"):
                    keys.append(identity_key(profile))
                yield Identity(row["candidate_id"], row["parent_id"], canonical, tuple(keys))

    def sources(self, members: tuple[str, ...] = ()) -> Iterator[Source]:
        """Every member's saved artifacts, or only the given members' after an action touched them."""
        only = _only(members)
        with closing(readonly(self.database)) as conn:
            for row in conn.execute("SELECT candidate_id,display_name FROM candidates JOIN current_parent USING(candidate_id) "
                                    + only + " ORDER BY candidate_id", members):
                key = row["candidate_id"]
                yield Source("name:" + key, key, self.location + "#candidates/" + key, row["display_name"])
            for row in conn.execute("SELECT candidate_id,facts_json FROM facts JOIN current_parent USING(candidate_id) "
                                    + only + " ORDER BY candidate_id", members):
                key = row["candidate_id"]
                yield Source("facts:" + key, key, self.location + "#facts/" + key, row["facts_json"])
            # Every member's human tags survive merges, as in queries_share.current_tags.
            for row in conn.execute("SELECT candidate_id,tags,note FROM person_tags JOIN current_parent USING(candidate_id) "
                                    + only + " ORDER BY candidate_id", members):
                key = row["candidate_id"]
                content = json.dumps({"human_tags": row["tags"].split("|"), "human_note": row["note"]})
                yield Source("tags:" + key, key, self.location + "#person_tags/" + key, content)
            # Who the person is to the owner, as Deep Context already decided it: the relationship kind,
            # the labels it holds with probability at least one half, and warmth (0-4) for ranking.
            for row in conn.execute("SELECT candidate_id,labels_json FROM person_labels JOIN current_parent USING(candidate_id) "
                                    + only + " ORDER BY candidate_id", members):
                key = row["candidate_id"]
                labels = object_dict(json.loads(row["labels_json"]))
                held = sorted(LABELS[name] for name, value in labels.items()
                              if name in LABELS and isinstance(value, (int, float)) and value >= 0.5)
                kind = string(labels["relationship_kind"]).replace("_", " ")
                warmth = labels["warmth"]
                if isinstance(warmth, bool) or not isinstance(warmth, (int, float)):
                    raise ValueError("person_labels warmth must be a number")
                content = json.dumps({"relationship_kind": kind if kind not in ("unknown", "stranger") else "",
                                      "labels": held, "warmth": float(warmth)})
                yield Source("labels:" + key, key, self.location + "#person_labels/" + key, content)
            profiles = profile_keys(conn)
            for row in conn.execute(
                "SELECT min(candidate_id) candidate_id,parent_id FROM current_parent "
                + ("WHERE parent_id IN (SELECT parent_id FROM current_parent " + only + ") " if members else "")
                + "GROUP BY parent_id ORDER BY parent_id", members
            ):
                key = row["parent_id"]
                url = profiles.get(key, "")
                if not url or url.startswith("synthetic:"):
                    continue
                member = row["candidate_id"]
                path = profile_cache_path(self.root / CACHE_RELATIVE_DIR, extract_public_identifier(url))
                if path is None:
                    continue
                try:
                    record = read_usable_cached_profile(path)
                    if record is None:
                        yield Source("profile:" + key, member, path.resolve().as_uri(), "", "profile unavailable")
                        continue
                    profile = profile_from_record(url, record)
                    # The confirmed mapping owns identity, never a name from a cache file.
                    if "li:" + profile.member_id != row["parent_id"]:
                        yield Source("profile:" + key, member, path.resolve().as_uri(), "", "profile member id mismatch")
                        continue
                    yield Source("profile:" + key, member, path.resolve().as_uri(), json.dumps(record["normalized_profile"], ensure_ascii=False))
                except (OSError, ValueError, KeyError) as exc:
                    yield Source("profile:" + key, member, path.resolve().as_uri(), "", type(exc).__name__)

    def owner_context(self) -> str:
        with closing(readonly(self.database)) as conn:
            row = conn.execute("SELECT payload_json FROM owner WHERE owner_key='owner'").fetchone()
            return row[0] if row else "{}"

    def inventory(self) -> dict[str, object]:
        with closing(readonly(self.database)) as conn:
            counts = {}
            for table in ("candidates", "current_parent", "facts", "person_tags", "person_labels", "current_profile"):
                counts[table] = conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
            counts["parents"] = conn.execute("SELECT count(DISTINCT parent_id) FROM current_parent").fetchone()[0]
            counts["parents_without_facts"] = conn.execute(
                "SELECT count(*) FROM (SELECT parent_id FROM current_parent EXCEPT "
                "SELECT parent_id FROM current_parent JOIN facts USING(candidate_id))").fetchone()[0]
        return {"adapter": "v2", "counts": counts, "profile_cache": (self.root / CACHE_RELATIVE_DIR).is_dir(),
                "unsupported": ["raw messages excluded", "synthetic research profiles not imported"]}

