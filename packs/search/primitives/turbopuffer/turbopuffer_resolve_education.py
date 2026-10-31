#!/usr/bin/env python3
"""Resolve education names to canonical school IDs in TurboPuffer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIMITIVES_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
for _path in [LIB_DIR, SHARED_DIR, TURBOPUFFER_DIR]:
    sys.path.insert(0, str(_path))

import turbopuffer_search_backend as turbopuffer_backend  # noqa: E402
from search_common import (  # noqa: E402
    load_env_file,
    role_payload_from_state,
    row_attrs,
)


STRONG_CONSISTENCY = {"level": "strong"}
SCHOOL_ATTRIBUTES = ["school_name", "person_count", "parent_id"]


TOKEN_RE = re.compile(r"[a-z0-9]+")
ROOT_SCHOOL_SUFFIXES = {"university", "college", "institute"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    log_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def education_names(payload: dict[str, Any]) -> list[str]:
    names = payload.get("education_names") or payload.get("school_names") or []
    return list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))


def school_tokens(name: str) -> list[str]:
    return TOKEN_RE.findall(str(name or "").lower())


def affiliated_school_queries(name: str) -> list[str]:
    """Return broader school-family queries for root names like Stanford University."""
    tokens = school_tokens(name)
    if len(tokens) == 2 and tokens[1] in ROOT_SCHOOL_SUFFIXES and len(tokens[0]) >= 4:
        return [tokens[0]]
    return []


def is_affiliated_candidate(root_queries: list[str], school_name: str) -> bool:
    if not root_queries:
        return False
    tokens = school_tokens(school_name)
    return bool(tokens and tokens[0] in set(root_queries))


async def query_schools(filters: tuple, *, limit: int) -> list[dict[str, Any]]:
    ns = turbopuffer_backend.namespace("schools")

    def run_query() -> Any:
        return ns.query(
            filters=filters,
            top_k=limit,
            include_attributes=SCHOOL_ATTRIBUTES,
            consistency=STRONG_CONSISTENCY,
        )

    response = await asyncio.to_thread(run_query)
    return [row_attrs(row, SCHOOL_ATTRIBUTES) for row in (response.rows or [])]


async def query_school_rows(query: str, *, limit: int) -> list[dict[str, Any]]:
    return await query_schools(("school_name", "ContainsAllTokens", query, {"last_as_prefix": True}), limit=limit)


def without_alias(school_name: str) -> str:
    """The schools index appends aliases in a trailing parenthetical ("… (UCLA / Bruins)"); people's rows don't."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", school_name).strip()


async def query_people_school_ids(names: list[str], *, limit: int) -> list[dict[str, Any]]:
    """The school ids people are actually filed under for these exact school names.

    The schools index can hold one school under two ids with two names (KTH is "Kungliga tekniska
    högskolan" there while its people wrote "KTH Royal Institute of Technology"), so a name match in
    the schools index alone leaves those people out of the education prefilter. Each row is one
    person's education entry; a big school has more rows than one page, so pages are walked by id
    until the last one is short: a minority id must not be crowded out by the majority one."""
    ns = turbopuffer_backend.namespace("education")
    by_name: tuple = ("school_name", "In", names)
    rows: list[dict[str, Any]] = []
    last_id: str | None = None
    while True:
        filters = by_name if last_id is None else ("And", [by_name, ("id", "Gt", last_id)])

        def run_query() -> Any:
            return ns.query(
                filters=filters,
                rank_by=("id", "asc"),
                top_k=limit,
                include_attributes=["education_id", "school_name"],
                consistency=STRONG_CONSISTENCY,
            )

        page = [row_attrs(row, ["education_id", "school_name"]) for row in ((await asyncio.to_thread(run_query)).rows or [])]
        rows.extend(page)
        if len(page) < limit:
            return rows
        last_id = page[-1]["id"]


async def resolve_name(name: str, *, limit: int) -> dict[str, Any]:
    root_queries = affiliated_school_queries(name)
    query_rows: list[tuple[str, dict[str, Any]]] = []
    for query in [name, *root_queries]:
        query_rows.extend((query, row) for row in await query_school_rows(query, limit=limit))

    # A match on a child stands for its parent: that is the id people are filed under.
    parent_ids = sorted({str(row["parent_id"]) for _, row in query_rows if row.get("parent_id")})
    parents = {row["id"]: row for row in await query_schools(("id", "In", parent_ids), limit=limit)} if parent_ids else {}

    counts: Counter[str] = Counter()
    top_names: dict[str, str] = {}
    matched_names: dict[str, set[str]] = {}
    named_children: dict[str, dict[str, str]] = {}
    family_ids: set[str] = set()
    query_words = set(school_tokens(name))
    for query, row in query_rows:
        school = parents[row["parent_id"]] if row.get("parent_id") else row
        school_id = str(school.get("id") or "")
        if not school_id:
            continue
        counts[school_id] = max(counts[school_id], int(school.get("person_count") or 1))
        school_name = str(school.get("school_name") or school_id)
        top_names[school_id] = school_name
        matched_names.setdefault(school_id, {school_name}).add(str(row.get("school_name") or ""))
        if row.get("parent_id") and query_words <= set(school_tokens(row.get("school_name"))):
            named_children.setdefault(school_id, {})[str(row["id"])] = str(row["school_name"])
        # A family is judged on the school's own name: a child called Columbian College doesn't put GWU in Columbia's.
        if is_affiliated_candidate(root_queries, school_name) or (query in root_queries and any(word.startswith(query) for word in school_tokens(without_alias(school_name)))):
            family_ids.add(school_id)

    # The index matches the last word as a prefix: a school holding the word itself beats a larger
    # one that only starts with it ("TUM" is the Technical University of Munich, not "Tumor Immunology").
    def has_query_words(school_id: str) -> bool:
        return any(query_words <= set(school_tokens(school_name)) for school_name in matched_names[school_id])

    candidates = []
    for school_id, count in sorted(counts.most_common(10), key=lambda item: not has_query_words(item[0])):
        include = school_id in family_ids or (not candidates and not root_queries)
        candidates.append({
            "id": school_id,
            "name": top_names[school_id],
            "matched_rows": count,
            "included": include,
            "match_family": bool(root_queries and school_id in family_ids),
        })
    resolved_ids = [candidate["id"] for candidate in candidates if candidate.get("included")]
    if not resolved_ids and candidates:
        resolved_ids = [candidates[0]["id"]]

    # Every id people wrote one of the resolved names under, whatever the schools index calls it.
    lookup_names = list(dict.fromkeys([name, *(without_alias(c["name"]) for c in candidates if c["id"] in resolved_ids)]))
    people_rows = await query_people_school_ids(lookup_names, limit=limit)
    people_ids = sorted({str(row["education_id"]) for row in people_rows if row.get("education_id")})
    schools = await query_schools(("id", "In", people_ids), limit=limit) if people_ids else []
    top_ids = {str(school["id"]): str(school.get("parent_id") or school["id"]) for school in schools}
    people_counts: Counter[str] = Counter()
    people_names: dict[str, str] = {}
    for row in people_rows:
        school_id = str(row.get("education_id") or "")
        if not school_id:
            continue
        school_id = top_ids.get(school_id, school_id)
        people_counts[school_id] += 1
        people_names.setdefault(school_id, str(row.get("school_name") or school_id))
    for school_id, count in people_counts.most_common():
        if school_id in resolved_ids:
            continue
        resolved_ids.append(school_id)
        candidates.append({
            "id": school_id,
            "name": people_names[school_id],
            "matched_rows": count,
            "included": True,
            "match_family": False,
            "from_people": True,
        })
    resolved_ids = list(dict.fromkeys(resolved_ids))
    children = await query_schools(("parent_id", "In", resolved_ids), limit=limit) if resolved_ids else []
    return {
        "query": name,
        "candidates": candidates,
        "resolved_ids": resolved_ids,
        "children": [{"id": child["id"], "name": child["school_name"], "parent_id": child["parent_id"], "matched_rows": int(child.get("person_count") or 0)}
                     for child in children],
        # The sub-schools this query names: passed as education_ids, they narrow a search to just them.
        "named_child_ids": [child_id for school_id in resolved_ids for child_id in named_children.get(school_id, {})],
        "affiliated_queries": root_queries,
    }


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "resolve_education",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "resolve_education",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "resolved_count": len(output.get("education_ids") or []),
    })


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file) if args.env_file else None)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)

    existing_ids = [str(value) for value in payload.get("education_ids") or [] if value]
    names = education_names(payload)
    resolutions = [await resolve_name(name, limit=args.max_rows_per_name) for name in names]

    resolved_ids = []
    for resolution in resolutions:
        resolved_ids.extend(str(value) for value in resolution.get("resolved_ids") or [] if value)

    education_ids = list(dict.fromkeys([*existing_ids, *resolved_ids]))
    # The prefilter reuses these children instead of asking the schools index again.
    families: dict[str, list[str]] = {}
    for resolution in resolutions:
        for school_id in resolution.get("resolved_ids") or []:
            families.setdefault(str(school_id), [str(school_id)])
        for child in resolution.get("children") or []:
            if child["id"] not in families[child["parent_id"]]:
                families[child["parent_id"]].append(child["id"])
    return {
        "namespace": turbopuffer_backend.namespace_name("schools"),
        "education_names": names,
        "education_ids": education_ids,
        "education_families": families,
        "resolutions": resolutions,
        "unresolved_names": [
            resolution["query"]
            for resolution in resolutions
            if not resolution["candidates"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve education names to canonical IDs")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--max-rows-per-name", type=int, default=5000)
    args = parser.parse_args()

    started = time.time()
    output = asyncio.run(run(args))
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        if not args.state:
            raise SystemExit("--write-state requires --state")
        state_path = Path(args.state)
        record_step(state_path, read_json(state_path), output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
