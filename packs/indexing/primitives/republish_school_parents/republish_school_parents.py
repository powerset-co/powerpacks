#!/usr/bin/env python3
"""Apply the school parent mapping to the published education namespaces.

Every people-education row moves to the top parent of its education_id, the id
a rebuild would write to canonical_education_id (only that attribute is patched). Each parent school doc gets person_count
recomputed from those rows and its children's names as a trailing alias, so a
child's name still finds the parent; the child docs are then deleted.

School docs follow the mapping, so correcting it undoes a merge on the next run:
a school that stands alone again gets its doc rebuilt and its rows back, and its
name drops off the old parent's alias.

Namespaces follow ALEPH_ENV like the rest of the search code: unset is prod,
dev is the _dev family. Without --apply this only prints counts. --apply first
backs both namespaces up as JSONL. A rerun finds nothing to do.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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


def split_alias(name: str) -> tuple[str, list[str]]:
    """"Harvard University (HBS / HKS)" -> ("Harvard University", ["HBS", "HKS"])."""
    match = re.fullmatch(r"(.*?)\s*\(([^()]*)\)\s*", name)
    return (match[1], [alias.strip() for alias in match[2].split("/") if alias.strip()]) if match else (name, [])


def tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def merged_name(name: str, child_names: Iterable[str]) -> str:
    """The name plus each child name that adds a word to it, in one parenthetical.

    The schools index matches on all query words anywhere in the field, so a
    child whose words are all there already needs no alias. The resolver strips
    the trailing parenthetical, so an alias can't hold parentheses of its own.
    """
    base, aliases = split_alias(name)
    known = tokens(name)
    added = False
    for child in child_names:
        child_base, child_aliases = split_alias(child)
        for alias in [child_base, *child_aliases]:
            alias = " ".join(re.sub(r"[()]", " ", alias).split())
            if tokens(alias) - known:
                aliases.append(alias)
                known |= tokens(alias)
                added = True
    return f"{base} ({' / '.join(aliases)})" if added else name


def plan(edges: list[dict[str, Any]], schools: list[dict[str, Any]], rows: dict[str, dict[str, Any]]) -> dict[str, list]:
    """The writes that make the namespaces match the mapping; empty once they do."""

    def top(school_id: str) -> str:
        return rows[school_id]["parent_id"] or school_id

    edge_patches = [
        {"id": edge["id"], "canonical_education_id": top(edge["education_id"])}
        for edge in edges
        if top(edge["education_id"]) != edge["canonical_education_id"]
    ]
    people: dict[str, set[str]] = {}
    for edge in edges:
        people.setdefault(top(edge["education_id"]), set()).add(edge["person_id"])
    rows_on_child = Counter(edge["education_id"] for edge in edges)
    children: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows.values(), key=lambda row: (-rows_on_child[row["id"]], row["school_name"])):
        if row["parent_id"]:
            children.setdefault(row["parent_id"], []).append(row)

    docs = {school["id"]: school for school in schools}
    school_patches = []
    restores = []
    for row in rows.values():
        if row["parent_id"]:
            continue
        name = merged_name(row["school_name"], [child["school_name"] for child in children.get(row["id"], [])])
        count = len(people.get(row["id"], ()))
        doc = docs.get(row["id"])
        if doc is None:
            restores.append({"id": row["id"], "school_name": name, "person_count": count} | ({"canonical_key": row["key"]} if row["key"] else {}))
        elif name != doc["school_name"] or (row["id"] in children and count != doc.get("person_count")):
            school_patches.append({"id": row["id"], "school_name": name, "person_count": count})
    deletes = [school["id"] for school in schools if rows[school["id"]]["parent_id"]]
    return {"edge_patches": edge_patches, "school_patches": school_patches, "restores": restores, "deletes": deletes}


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
        report["backups"] = [str(backup(edges, edges_ns.id, backup_dir, stamp)), str(backup(schools, schools_ns.id, backup_dir, stamp))]
        write_batches(edges_ns, "patch_rows", writes["edge_patches"])
        write_batches(schools_ns, "upsert_rows", writes["restores"])
        write_batches(schools_ns, "patch_rows", writes["school_patches"])
        write_batches(schools_ns, "deletes", writes["deletes"])
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
