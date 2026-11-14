#!/usr/bin/env python3
"""Sync this install's network into the Powerset cloud through network-search-api.

Flow: read every person with a LinkedIn slug from the local search index -> fingerprint each
(their index rows, sources, Gmail addresses and warmth) -> POST /v2/network-sync/diff with
{person_id: fingerprint} -> the cloud answers which people changed and which were removed ->
POST /v2/network-sync/apply in gzipped batches carrying only those people (profile, sources,
search documents, and the companies/schools they reference).

Limits: a batch is at most BATCH_PEOPLE people and is halved until it gzips under MAX_BATCH_GZ
(Cloud Run refuses bodies over 32 MiB). Vectors travel as base64 float32. Rows are read from
DuckDB per batch, so memory is one batch plus the people list without their hydrated profiles.
A failed batch is retried with backoff; a run that still fails stops, and the next run's diff
asks again for exactly the people the cloud has not acknowledged.

The cloud decides everything else (access lists, deletes, position ids); this side only reads.
A run that changes nothing is one diff request.

Changelog:
  2026-11-14: created.
"""

from __future__ import annotations

import argparse
import array
import base64
import gzip
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import duckdb

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from packs.indexing.lib.identity import position_uuid  # noqa: E402
from packs.indexing.primitives.upload_powerset import local_index  # noqa: E402
from packs.indexing.primitives.upload_powerset.models import LocalPerson, PersonProfile  # noqa: E402
from packs.ingestion.primitives.deep_context_v2.db.store import STORE_RELATIVE_PATH  # noqa: E402

DEFAULT_ROOT = REPO
INDEX_RELATIVE = Path(".powerpacks/search-index/local-search.duckdb")
BATCH_PEOPLE = 50
MAX_BATCH_GZ = 8 * 2**20
RETRIES = 4
# Laptop namespace name -> the cloud's logical name.
PERSON_DOCS = {"people": "people", "summaries": "summaries", "education": "people_education"}

# path, gzipped JSON body -> parsed JSON reply
Api = Callable[[str, bytes], dict[str, Any]]


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def ordinals(person_id: str, work_experiences: Any) -> dict[str, int]:
    """Local position id -> the position's index in the profile, the way build_roles numbers them;
    the cloud names a position {person_id}-{index}."""
    experiences = [exp for exp in (_json(work_experiences) or []) if isinstance(exp, dict)]
    out = {}
    for idx, exp in enumerate(experiences):
        key = str(exp.get("id") or exp.get("position_id") or exp.get("urn") or "").strip() or idx
        out[position_uuid(person_id, key)] = idx
    return out


def read_warmth(store: Path) -> dict[str, float]:
    if not store.exists():
        return {}
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT lower(public_identifier), warmth FROM families "
                            "WHERE public_identifier != '' AND warmth IS NOT NULL").fetchall()
    return {slug: round(float(warmth), 3) for slug, warmth in rows}


def read_people(con: Any, warmth: dict[str, float]) -> dict[str, dict[str, Any]]:
    """person_id -> what the cloud keeps about them apart from search documents."""
    rows = con.execute("SELECT * FROM local_person_profiles WHERE coalesce(public_identifier, '') != ''")
    columns = [c[0] for c in rows.description]
    people = {}
    for values in rows.fetchall():
        row = dict(zip(columns, values))
        local = LocalPerson.from_csv_row({k: "" if v is None else (v if isinstance(v, str) else json.dumps(v, default=str))
                                          for k, v in row.items()} | {"id": str(row["person_id"])})
        profile = PersonProfile.from_db_row(row)
        sources = [{"source_channel": channel,
                    "source_identifier": local.source_identifier(channel).lower() if channel == "gmail"
                    else local.source_identifier(channel),
                    "total_interactions": local.interaction_counts.get(channel, 0),
                    "last_interaction_at": local.last_interaction or None}
                   for channel in local.channels if local.source_identifier(channel)]
        emails = ([{"primary_email": local.primary_email.lower(), "display_name": profile.full_name,
                    "total_messages": local.interaction_counts.get("gmail", 0)}]
                  if "gmail" in local.channels and local.primary_email else [])
        profile.__dict__.pop("hydrated_context", None)  # read per batch; it is most of a profile's size
        people[profile.id] = {
            "person_id": profile.id,
            "public_identifier": profile.public_identifier,
            "warmth": warmth.get(profile.public_identifier),
            "profile": {k: v for k, v in vars(profile).items() if k not in ("id", "public_identifier") and v is not None},
            "sources": sources,
            "emails": emails,
            "_ordinals": ordinals(profile.id, row.get("work_experiences")),
        }
    return people


def fingerprints(con: Any, people: dict[str, dict[str, Any]]) -> dict[str, str]:
    """One hash per person over their index rows and everything else sent about them."""
    ids = tuple(sorted(people))
    index_hashes = local_index.person_hashes(con, ids)
    out = {}
    for person_id in ids:
        person = people[person_id]
        extra = json.dumps([index_hashes.get(person_id), person["sources"], person["emails"], person["warmth"]],
                           sort_keys=True, default=str)
        out[person_id] = hashlib.sha256(extra.encode()).hexdigest()
    return out


def batch_payload(con: Any, people: dict[str, dict[str, Any]], ids: list[str], hashes: dict[str, str],
                  sent_entities: set[str]) -> dict[str, Any]:
    """One apply body: these people with their documents, and entities not sent earlier in this run."""
    docs: dict[str, dict[str, list[dict[str, Any]]]] = {pid: {} for pid in ids}
    for logical, cloud in PERSON_DOCS.items():
        for doc in local_index.namespace_rows(con, logical, tuple(ids), {}, "", frozenset()):
            pid = str(doc.get("base_id") or doc.get("person_id") or doc.get("id"))
            if pid not in docs:
                continue
            if isinstance(doc.get("vector"), (list, tuple)):
                doc["vector"] = pack_vector(doc["vector"])
            if logical == "people":
                ordinal = people[pid]["_ordinals"].get(str(doc["id"]))
                if ordinal is None:
                    continue
                doc["ordinal"] = ordinal
            docs[pid].setdefault(cloud, []).append(doc)
    entities: dict[str, list[dict[str, Any]]] = {}
    for logical in ("companies", "schools"):
        wanted = sorted({eid for eids in local_index.entity_ids_by_person(con, logical, ids).values() for eid in eids}
                        - sent_entities)
        rows = local_index.namespace_rows(con, logical, tuple(wanted), {}, "", frozenset()) if wanted else []
        for row in rows:
            if isinstance(row.get("vector"), (list, tuple)):
                row["vector"] = pack_vector(row["vector"])
        entities[logical] = rows
    hydrated = dict(con.execute("SELECT person_id, hydrated_context FROM local_person_profiles WHERE person_id = ANY(?)",
                                [list(ids)]).fetchall())
    out = []
    for pid in ids:
        person = {k: v for k, v in people[pid].items() if not k.startswith("_")}
        context = hydrated.get(pid)
        if isinstance(context, str) and context:
            person["profile"] = {**person["profile"], "hydrated_context": context}
        out.append({**person, "hash": hashes[pid], "docs": docs[pid]})
    return {
        "people": out,
        "companies": entities["companies"],
        "schools": entities["schools"],
    }


def pack_vector(vector: Any) -> str:
    """base64 little-endian float32: 8 KB for 1536 dims instead of ~30 KB of JSON numbers."""
    floats = array.array("f", (float(v) for v in vector))
    if sys.byteorder != "little":
        floats.byteswap()
    return base64.b64encode(floats.tobytes()).decode()


def encode(body: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(body, default=str, separators=(",", ":")).encode(), compresslevel=5)


def call_with_retry(api: Api, path: str, data: bytes, log: Callable[[str], None]) -> dict[str, Any]:
    """Retry network errors, timeouts, 429 and 5xx with backoff; anything else is raised at once."""
    for attempt in range(RETRIES):
        try:
            return api(path, data)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500 or attempt == RETRIES - 1:
                raise
            log(f"cloud answered {exc.code}; retrying")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == RETRIES - 1:
                raise
            log(f"network error ({type(exc).__name__}); retrying")
        time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def http_api(root: Path) -> Api:
    from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys

    env_file = root / ".env"
    base = pull_runtime_keys.api_base(env_file)

    def call(path: str, data: bytes) -> dict[str, Any]:
        request = urllib.request.Request(base + path, data=data, method="POST", headers={
            "Authorization": f"Bearer {pull_runtime_keys.bearer_token(env_file)}",
            "Content-Type": "application/json", "Content-Encoding": "gzip"})
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read() or b"{}")
    return call


def sync(root: Path, api: Api, *, dry_run: bool = False, log: Callable[[str], None] = print) -> dict[str, Any]:
    started = time.monotonic()
    con = duckdb.connect(str(root / INDEX_RELATIVE), read_only=True)
    try:
        people = read_people(con, read_warmth(root / ".powerpacks" / STORE_RELATIVE_PATH))
        hashes = fingerprints(con, people)
        read_seconds = time.monotonic() - started
        plan = call_with_retry(api, "/v2/network-sync/diff", encode({"hashes": hashes}), log)
        upsert, remove = plan["upsert"], plan["remove"]
        summary: dict[str, Any] = {"people": len(people), "upsert": len(upsert), "remove": len(remove),
                                   "read_seconds": round(read_seconds, 2), "dry_run": dry_run}
        if dry_run:
            return summary
        totals: dict[str, int] = {"batches": 0, "bytes_sent": 0}
        sent: set[str] = set()
        queue = [upsert[i:i + BATCH_PEOPLE] for i in range(0, len(upsert), BATCH_PEOPLE)]
        pending_removals = list(remove)
        while queue or pending_removals:
            ids = queue.pop(0) if queue else []
            body = batch_payload(con, people, ids, hashes, sent) if ids else {"people": [], "companies": [], "schools": []}
            body["removed"], pending_removals = pending_removals, []
            data = encode(body)
            if len(data) > MAX_BATCH_GZ and len(ids) > 1:
                queue[:0] = [ids[:len(ids) // 2], ids[len(ids) // 2:]]
                pending_removals = body["removed"]
                continue
            result = call_with_retry(api, "/v2/network-sync/apply", data, log)
            sent.update(str(row["id"]) for row in body["companies"] + body["schools"])
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + int(value or 0)
            totals["batches"] += 1
            totals["bytes_sent"] += len(data)
            log(f"synced {totals['batches']} batches, {len(queue)} queued")
        return {**summary, **totals, "seconds": round(time.monotonic() - started, 2)}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="Ask the cloud what would change; write nothing.")
    args = parser.parse_args(argv)
    from packs.ingestion.primitives.deep_context_v2.openai import load_env
    load_env(args.root / ".env")
    print(json.dumps(sync(args.root, http_api(args.root), dry_run=args.dry_run, log=lambda m: print(m, file=sys.stderr))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
