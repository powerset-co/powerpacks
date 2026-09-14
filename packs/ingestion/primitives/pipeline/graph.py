#!/usr/bin/env python3
"""Report declared pipeline inputs, outputs, ownership conflicts, and cycles.

Flow: import node modules -> collect declarations -> emit the graph report.
External consumers can make an output appear unused in this partial graph.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import Artifact, Node  # noqa: E402

# The converted nodes. Importing them IS the registration (and would already have
# raised TypeError if any declaration were incomplete).
import packs.ingestion.primitives.deep_context.apply_retargets  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.assemble_synthetic_profile  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.build_owner  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.build_parents  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.cluster_merge_candidates  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.collect_person_context  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.compose_dossier  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.persist_review_identities  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.prefetch_profiles  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.reconcile_deep_research  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.reconcile_linkedin  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.synthesize_person_context  # noqa: E402,F401
import packs.ingestion.primitives.discover.gmail.discover  # noqa: E402,F401
import packs.ingestion.primitives.discover.messages.discover  # noqa: E402,F401
import packs.ingestion.primitives.enrich.enrich_people  # noqa: E402,F401
import packs.ingestion.primitives.imports.gmail.importer  # noqa: E402,F401
import packs.ingestion.primitives.imports.linkedin.network_import  # noqa: E402,F401
import packs.ingestion.primitives.imports.merge_people  # noqa: E402,F401
import packs.ingestion.primitives.imports.messages.importer  # noqa: E402,F401


def node_subclasses(root: type[Node] = Node) -> list[type[Node]]:
    """Every declared Node subclass, depth-first — the graph's node list."""
    found: list[type[Node]] = []
    for subclass in root.__subclasses__():
        found.append(subclass)
        found.extend(node_subclasses(subclass))
    return found


def _claims_all_columns(item: Artifact) -> bool:
    """No column scope (or a full_rewrite, which overwrites every column anyway)."""
    return not item.owns_columns or item.writes == "full_rewrite"


def _claims_all_rows(item: Artifact) -> bool:
    """No row scope, so this declaration covers every row in the file."""
    return not item.owns_rows_where


def _scopes_intersect(first: Artifact, second: Artifact) -> bool:
    """Two writers of one path collide when their (rows x columns) scopes overlap.

    Rows and columns are independent axes and BOTH must intersect for a conflict:
    `contacts.csv`'s writers share every row but own disjoint columns, and
    `directory.csv`'s writers own every column but disjoint rows. Row predicates
    are compared as STRINGS — `owns_rows_where` is a declaration and this checker
    never evaluates it — so two writers naming the same slice still conflict."""
    rows_intersect = (
        _claims_all_rows(first)
        or _claims_all_rows(second)
        or first.owns_rows_where == second.owns_rows_where
    )
    columns_intersect = (
        _claims_all_columns(first)
        or _claims_all_columns(second)
        or bool(set(first.owns_columns) & set(second.owns_columns))
    )
    return rows_intersect and columns_intersect


def check_graph(nodes: list[type[Node]]) -> dict[str, Any]:
    """The report. Pure function of the declarations — reads no files."""
    producers: dict[str, list[tuple[str, Artifact]]] = {}
    consumers: dict[str, list[str]] = {}
    for node in nodes:
        for item in node.outputs:
            producers.setdefault(item.path, []).append((node.name, item))
        for item in node.inputs:
            consumers.setdefault(item.path, []).append(node.name)
    # Manifests are real inputs, but status readers sit outside this graph.
    manifest_producers = {node.manifest: node.name for node in nodes if node.manifest}

    dead_outputs = [
        {"node": name, "path": path}
        for path, declared in producers.items()
        for name, item in declared
        if not consumers.get(path)
    ]
    phantom_inputs = [
        {"node": node.name, "path": item.path}
        for node in nodes
        for item in node.inputs
        if not producers.get(item.path) and item.path not in manifest_producers and not item.external
    ]

    two_writer_conflicts: list[dict[str, Any]] = []
    schema_mismatches: list[dict[str, Any]] = []
    for path, declared in producers.items():
        for index, (name, item) in enumerate(declared):
            for other_name, other in declared[index + 1:]:
                overlap = sorted(set(item.owns_columns) & set(other.owns_columns))
                if _scopes_intersect(item, other):
                    if overlap:
                        reason = "overlapping owned columns"
                    elif item.owns_rows_where and other.owns_rows_where:
                        reason = "two writers own the same row slice"
                    else:
                        reason = "a writer claims the whole file"
                    two_writer_conflicts.append({
                        "path": path,
                        "nodes": [name, other_name],
                        "overlapping_columns": overlap,
                        "reason": reason,
                    })
                if item.row_model is not other.row_model:
                    schema_mismatches.append({
                        "path": path,
                        "nodes": [name, other_name],
                        "reason": "declared with two different row models",
                    })
    for node in nodes:
        for item in (*node.inputs, *node.outputs):
            if item.row_model is None:
                continue
            unknown = [column for column in item.owns_columns if column not in item.row_model.columns()]
            if unknown:
                schema_mismatches.append({
                    "path": item.path,
                    "nodes": [node.name],
                    "reason": f"owns columns absent from {item.row_model.__name__}: {unknown}",
                })

    edges = {
        node.name: sorted({
            name
            for item in node.inputs
            for name in (
                [producer for producer, _artifact in producers.get(item.path, [])]
                + ([manifest_producers[item.path]] if item.path in manifest_producers else [])
            )
            if name != node.name
        })
        for node in nodes
    }
    # Cycle detection excludes `feedback=True` writes (the persist stage's
    # directory.csv slice feeds the NEXT realization of the importers/merge —
    # the one deliberate loop). Everything else about the edge stays scored.
    forward_producers = {
        path: [name for name, item in declared if not item.feedback]
        for path, declared in producers.items()
    }
    forward_edges = {
        node.name: sorted({
            name
            for item in node.inputs
            for name in (
                forward_producers.get(item.path, [])
                + ([manifest_producers[item.path]] if item.path in manifest_producers else [])
            )
            if name != node.name
        })
        for node in nodes
    }
    return {
        "status": "completed",
        "nodes": sorted(node.name for node in nodes),
        "edges": edges,
        "dead_outputs": dead_outputs,
        "phantom_inputs": phantom_inputs,
        "two_writer_conflicts": two_writer_conflicts,
        "schema_mismatches": schema_mismatches,
        "cycles": find_cycles(forward_edges),
    }


def find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """Every DISTINCT cycle reachable by DFS, canonicalized, as `[a, b, a]` paths.

    The DFS finds each loop once per member (every node on the cycle is also a
    DFS start that reports it), so each found cycle is rotated to start at its
    lexicographically-smallest node and deduped; the result is sorted so the
    report is deterministic. Hand-rolled rather than networkx: this is 16 lines,
    networkx is not in the lockfile, and a dependency that exists to replace 16
    lines of DFS is the kind of machinery the ground rules tell us not to add."""
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []
    for start in edges:
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in edges.get(node, []):
                if nxt == start:
                    pivot = path.index(min(path))
                    canonical = tuple(path[pivot:] + path[:pivot])
                    if canonical not in seen:
                        seen.add(canonical)
                        cycles.append([*canonical, canonical[0]])
                elif nxt not in path:
                    stack.append((nxt, path + [nxt]))
    return sorted(cycles)


def main() -> int:
    """Report mode only: always exit 0, findings are in the payload."""
    emit(check_graph(node_subclasses()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
