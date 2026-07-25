#!/usr/bin/env python3
"""Report what the declared pipeline graph says about itself. REPORT ONLY.

Builds the graph from `Node` declarations (never from a run) and reports:

  dead_outputs      an output no declared input reads, and not consumers_optional
  phantom_inputs    an input no node produces, and not external=True
  two_writer_conflicts  one path, two writers whose owned columns overlap (or a
                    writer that claims the whole file, or a full_rewrite next to
                    any other writer) — the `index.json` shape that cost 494
                    duplicate review rows in #337. Two writers that declare
                    DIFFERENT `owns_rows_where` slices are not a conflict:
                    `directory.csv`'s gmail and messages writers each own only
                    their own source's rows.
  schema_mismatches one path declared with two different row models, or an owned
                    column that is not in the row model
  cycles            a producer/consumer loop

Most of the pipeline is NOT converted yet, so a dead output or phantom input here
usually means "its producer/consumer is an unconverted stage" — the converted
subset's boundary, not a bug. That is exactly why this is not a CI gate.

Flow: import the converted node modules -> walk Node subclasses -> group
declarations by path -> emit one JSON report.

Changelog:
  2026-07-25 (import stage): the four import-stage nodes joined the graph
    (`gmail_import`, `messages_import`, `messages_match_local`,
    `linkedin_import`), and the two-writer check learned the row-slice axis
    (`Artifact.owns_rows_where`) that `directory.csv`'s two writers need.
  2026-07-25: created with the declared-contract prototype.
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
import packs.ingestion.primitives.discover.gmail.discover  # noqa: E402,F401
import packs.ingestion.primitives.imports.gmail.importer  # noqa: E402,F401
import packs.ingestion.primitives.imports.linkedin.network_import  # noqa: E402,F401
import packs.ingestion.primitives.imports.merge_people  # noqa: E402,F401
import packs.ingestion.primitives.imports.messages.importer  # noqa: E402,F401
import packs.ingestion.primitives.imports.messages.match_local_candidates  # noqa: E402,F401


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

    dead_outputs = [
        {"node": name, "path": path}
        for path, declared in producers.items()
        for name, item in declared
        if not consumers.get(path) and not item.consumers_optional
    ]
    phantom_inputs = [
        {"node": node.name, "path": item.path}
        for node in nodes
        for item in node.inputs
        if not producers.get(item.path) and not item.external
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
            for name, _artifact in producers.get(item.path, [])
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
        "cycles": find_cycles(edges),
    }


def find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """Every cycle reachable by DFS, as `[a, b, a]` paths.

    Hand-rolled rather than networkx: this is 12 lines, networkx is not in the
    lockfile, and a dependency that exists to replace 12 lines of DFS is the kind
    of machinery the ground rules tell us not to add."""
    cycles: list[list[str]] = []
    for start in edges:
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in edges.get(node, []):
                if nxt == start:
                    cycles.append(path + [start])
                elif nxt not in path:
                    stack.append((nxt, path + [nxt]))
    return cycles


def main() -> int:
    """Report mode only: always exit 0, findings are in the payload."""
    emit(check_graph(node_subclasses()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
