#!/usr/bin/env python3
"""Apply the school parent mapping to the published education namespaces.

Every school keeps its own doc under its own name, and a child's doc carries
parent_id. A parent's person_count covers its children too. A school's name is
written exactly as the mapping has it: released apps match a query on the
parent's own name, so trimming "(MIT)" off a parent breaks "MIT" for every app
that has not updated (2026-10-31). People rows are read, never written: search
expands a parent to its children when it filters on education_id.

School docs follow the mapping, so correcting it undoes a merge on the next run.
Namespaces follow ALEPH_ENV like the rest of the search code: unset is prod,
dev is the _dev family. Without --apply this only prints counts. --apply first
backs the school docs up as JSONL. A rerun finds nothing to do.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
SEARCH_PRIMITIVES = REPO / "packs/search/primitives"
for _path in [REPO, SEARCH_PRIMITIVES / "lib", SEARCH_PRIMITIVES / "shared", SEARCH_PRIMITIVES / "turbopuffer"]:
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import turbopuffer_search_backend as tp_backend  # noqa: E402
from search_common import load_env_file  # noqa: E402

from packs.indexing.lib.school_parents import MAPPING_PATH, read_rows  # noqa: E402

BACKUP_DIR = REPO / ".powerpacks/tmp-education-mapping/backups"
BATCH_SIZE = 500
PAGE_SIZE = 1000
STRONG_CONSISTENCY = {"level": "strong"}


def plan(edges: list[dict[str, Any]], schools: list[dict[str, Any]], rows: dict[str, dict[str, Any]]) -> dict[str, list]:
    """The writes that make the school docs match the mapping; empty once they do."""

    def top(school_id: str) -> str:
        return rows[school_id]["parent_id"] or school_id

    own_people: dict[str, set[str]] = {}
    family_people: dict[str, set[str]] = {}
    for edge in edges:
        own_people.setdefault(edge["education_id"], set()).add(edge["person_id"])
        family_people.setdefault(top(edge["education_id"]), set()).add(edge["person_id"])
    children = {row["parent_id"] for row in rows.values() if row["parent_id"]}

    docs = {school["id"]: school for school in schools}
    former_parents = {school.get("parent_id") for school in schools}
    school_patches = []
    restores = []
    for row in rows.values():
        school_id, parent_id = row["id"], row["parent_id"]
        name = row["school_name"]
        family = not parent_id and (school_id in children or school_id in former_parents)
        count = len((family_people if family else own_people).get(school_id, ()))
        doc = docs.get(school_id)
        if doc is None:
            restores.append({"id": school_id, "school_name": name, "person_count": count}
                            | ({"parent_id": parent_id} if parent_id else {})
                            | ({"canonical_key": row["key"]} if row["key"] else {}))
            continue
        wanted = {"school_name": name, "parent_id": parent_id} | ({"person_count": count} if family else {})
        changed = {field: value for field, value in wanted.items() if value != doc.get(field)}
        if changed:
            school_patches.append({"id": school_id, **changed})
    return {"school_patches": school_patches, "restores": restores}


def export(namespace: Any) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    while True:
        page = namespace.query(
            rank_by=("id", "asc"),
            top_k=PAGE_SIZE,
            include_attributes=True,
            filters=("id", "Gt", docs[-1]["id"]) if docs else None,
            consistency=STRONG_CONSISTENCY,
        )
        docs += [row.to_dict() for row in page.rows]
        if len(page.rows) < PAGE_SIZE:
            return docs


def backup(docs: list[dict[str, Any]], name: str, directory: Path, stamp: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def write_batches(namespace: Any, field: str, items: list) -> None:
    for start in range(0, len(items), BATCH_SIZE):
        namespace.write(**{field: items[start : start + BATCH_SIZE]})


def counts(writes: dict[str, list]) -> dict[str, int]:
    return {name: len(items) for name, items in writes.items()}


def run(edges_ns: Any, schools_ns: Any, rows: dict[str, dict[str, Any]], *, apply: bool, backup_dir: Path) -> dict[str, Any]:
    edges, schools = export(edges_ns), export(schools_ns)
    writes = plan(edges, schools, rows)
    report: dict[str, Any] = {"edges": len(edges), "schools": len(schools), **counts(writes), "applied": apply}
    if apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report["backup"] = str(backup(schools, schools_ns.id, backup_dir, stamp))
        write_batches(schools_ns, "upsert_rows", writes["restores"])
        write_batches(schools_ns, "patch_rows", writes["school_patches"])
        report["remaining"] = counts(plan(export(edges_ns), export(schools_ns), rows))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Back up, then write; default prints counts only")
    parser.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    load_env_file(args.env_file)
    report = run(tp_backend.namespace("education"), tp_backend.namespace("schools"), read_rows(args.mapping), apply=args.apply, backup_dir=args.backup_dir)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
