#!/usr/bin/env python3
"""Report what the declared pipeline graph says about itself. REPORT ONLY.

Builds the graph from `Node` declarations (never from a run) and reports:

  dead_outputs      an output no declared input reads
  phantom_inputs    an input no node produces, and not external=True. A node's
                    declared `manifest` counts as produced: two nodes read another
                    node's manifest (the gmail import takes the account selection
                    from discovery's; the messages import gates on the matcher's),
                    and that is a real edge, not a missing producer.
  two_writer_conflicts  one path, two writers whose owned columns overlap (or a
                    writer that claims the whole file, or a full_rewrite next to
                    any other writer) — the former whole-file lookup snapshot that cost 494
                    duplicate review rows in #337. Two writers that declare
                    DIFFERENT `owns_rows_where` slices are not a conflict:
                    `directory.csv`'s gmail and messages writers each own only
                    their own source's rows.
  schema_mismatches one path declared with two different row models, or an owned
                    column that is not in the row model
  cycles            a producer/consumer loop

Four of the five findings are empty as of 2026-07-26. `dead_outputs` is not, and
both entries are the LinkedIn enrichment's people.csv under its two bindings
(`enrichment/people.csv` when the enrich stage runs standalone,
`discover/linkedin/people.csv` when the LinkedIn import runs it against its own
dir): their reader is the unconverted indexing pack, whose
`linkedin_modal_pipeline.py` downloads that file to `import/linkedin/people.csv`.
A dead output whose consumer is an unconverted stage is the converted subset's
boundary, not a bug — which is why this is a report and not a CI gate.

Flow: import the converted node modules -> walk Node subclasses -> group
declarations by path -> emit one JSON report.

Changelog:
  2026-07-27 (deep-context registered): the twelve deep-context nodes joined the
    graph — the dossier chain (collect -> synthesize -> compose -> cluster ->
    parents -> reconcile), the enrichment tail (deep-research, assemble,
    prefetch, apply-retargets), owner, and persist-review (the third declared
    `directory.csv` row slice, closing the loop back into merge_people).
    review.csv became the graph's first THREE-owner file (synthesize's
    llm_worth family, reconcile's identity slice, the human's network_worth —
    row-bookkeeping columns deliberately unclaimed).
  2026-07-26 (cycles canonicalized): `find_cycles` rotates each found cycle to
    start at its lexicographically-smallest node and dedups, so a loop is one
    entry instead of one entry per member and path variant (the historical two
    back-edge defaults rendered as 23 entries). The report list is sorted.
  2026-07-26 (manifests are produced; enrich store registered): a node's declared
    `manifest` path now counts as a producer for `phantom_inputs` and `edges` —
    the two manifests another node reads (gmail discovery's, the messages
    matcher's) were reported as producer-less only because a manifest is declared
    as `manifest`, not as an `Artifact`. Manifests are deliberately NOT scored for
    dead outputs or two-writer conflicts (see check_graph). `EnrichPeople` joined
    the node list, and the graph's 23 cycles are gone with the two defaults that
    caused them (the matcher's `merged/people.csv` catalog and the WhatsApp
    extractor's `name_fallback_csv`).
  2026-07-25 (messages): registered the three messages-discovery nodes. They add
    the graph's first reported CYCLE, and it is real: the WhatsApp extractor
    reads the MERGED `.powerpacks/messages/contacts.csv` back as its
    `name_fallback_csv`, so messages_whatsapp_extract and messages_stage_merge
    each consume the other's output.
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
import packs.ingestion.primitives.deep_context.apply_retargets  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.assemble_synthetic_profile  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.build_owner  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.build_parents  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.cluster_merge_candidates  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.collect_person_context  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.compose_dossier  # noqa: E402,F401
import packs.ingestion.primitives.deep_context.ensure_parents  # noqa: E402,F401
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
        _claims_all_rows(first) or _claims_all_rows(second) or first.owns_rows_where == second.owns_rows_where
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
    # A node's declared `manifest` is a path it writes too, so a node that reads
    # ANOTHER node's manifest (gmail's import reads discovery's for the account
    # selection; the messages import gates on the matcher's) has a real producer,
    # not a phantom. Manifests are kept out of `producers` because they are the
    # stage's state contract rather than pipeline data: every one of them is read
    # by a status surface outside the graph, so scoring them as dead outputs would
    # report every converted stage as dead.
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
            for other_name, other in declared[index + 1 :]:
                overlap = sorted(set(item.owns_columns) & set(other.owns_columns))
                if _scopes_intersect(item, other):
                    if overlap:
                        reason = "overlapping owned columns"
                    elif item.owns_rows_where and other.owns_rows_where:
                        reason = "two writers own the same row slice"
                    else:
                        reason = "a writer claims the whole file"
                    two_writer_conflicts.append(
                        {
                            "path": path,
                            "nodes": [name, other_name],
                            "overlapping_columns": overlap,
                            "reason": reason,
                        }
                    )
                if item.row_model is not other.row_model:
                    schema_mismatches.append(
                        {
                            "path": path,
                            "nodes": [name, other_name],
                            "reason": "declared with two different row models",
                        }
                    )
    for node in nodes:
        for item in (*node.inputs, *node.outputs):
            if item.row_model is None:
                continue
            unknown = [column for column in item.owns_columns if column not in item.row_model.columns()]
            if unknown:
                schema_mismatches.append(
                    {
                        "path": item.path,
                        "nodes": [node.name],
                        "reason": f"owns columns absent from {item.row_model.__name__}: {unknown}",
                    }
                )

    edges = {
        node.name: sorted(
            {
                name
                for item in node.inputs
                for name in (
                    [producer for producer, _artifact in producers.get(item.path, [])]
                    + ([manifest_producers[item.path]] if item.path in manifest_producers else [])
                )
                if name != node.name
            }
        )
        for node in nodes
    }
    # Cycle detection excludes `feedback=True` writes (the persist stage's
    # directory.csv slice feeds the NEXT realization of the importers/merge —
    # the one deliberate loop). Everything else about the edge stays scored.
    forward_producers = {
        path: [name for name, item in declared if not item.feedback] for path, declared in producers.items()
    }
    forward_edges = {
        node.name: sorted(
            {
                name
                for item in node.inputs
                for name in (
                    forward_producers.get(item.path, [])
                    + ([manifest_producers[item.path]] if item.path in manifest_producers else [])
                )
                if name != node.name
            }
        )
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
