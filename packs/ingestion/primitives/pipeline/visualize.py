#!/usr/bin/env python3
"""Render the declared pipeline graph as Mermaid + tables. REPORT ONLY.

The declarations are the single source of truth (`contract.Node` ClassVars);
this module renders what `graph.check_graph` computes and adds nothing. The
output is one markdown document: a Mermaid flowchart (nodes grouped by stage,
edges labeled with the artifact that carries them, manifest reads dashed,
external inputs as cylinders, reader-less outputs as explicit leaves) followed
by a per-node IO table and the checker's findings. Regenerating after a
declaration change re-renders the truth — the diagram cannot drift from the
code, which is the point.

Flow: graph.node_subclasses() -> graph.check_graph() -> group by stage package
-> emit markdown to --out (default stdout).

Changelog:
  2026-07-26: created.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.pipeline.contract import Node  # noqa: E402
from packs.ingestion.primitives.pipeline.graph import check_graph, node_subclasses  # noqa: E402

# Stage grouping comes from the node's package, never from its name.
_STAGE_OF_PACKAGE = {"discover": "discover", "imports": "import", "enrich": "enrich"}


def stage_of(node: type[Node]) -> str:
    for token, stage in _STAGE_OF_PACKAGE.items():
        if f".{token}." in node.__module__:
            return stage
    return "other"


def short(path: str) -> str:
    """An edge/table label: the path minus the fixed prefixes, the home dir as
    `~` (a shareable document must not carry the username), Mermaid-safe
    (curly braces from the per-account template would end a Mermaid node)."""
    text = path.replace(".powerpacks/network-import/", "").replace(".powerpacks/", "")
    text = text.replace(str(Path.home()), "~")
    return text.replace("{", "(").replace("}", ")")


def _mermaid_id(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "_", name)


def _label(paths: list[str], limit: int = 2) -> str:
    names = [short(p) for p in sorted(set(paths))]
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" +{len(names) - limit}"
    return ", ".join(names)


def mermaid(nodes: list[type[Node]], report: dict) -> str:
    # A path can have SEVERAL producers (messages/contacts.csv: the discovery
    # merge writes the metadata columns, the matcher annotates the match_*
    # columns) — a last-wins dict here would silently drop the first writer's
    # edges, so every producer of a consumed path gets one.
    producers: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for item in node.outputs:
            producers[item.path].append(node.name)
    manifest_producers = {node.manifest: node.name for node in nodes if node.manifest}

    # producer -> consumer -> [artifact paths]; manifest edges kept separate (dashed).
    artifact_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    manifest_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    external_inputs: dict[str, list[str]] = defaultdict(list)  # path -> consumers
    for node in nodes:
        for item in node.inputs:
            if item.path in producers and [p for p in producers[item.path] if p != node.name]:
                for producer in producers[item.path]:
                    if producer != node.name:
                        artifact_edges[(producer, node.name)].append(item.path)
            elif item.path in manifest_producers and manifest_producers[item.path] != node.name:
                manifest_edges[(manifest_producers[item.path], node.name)].append(item.path)
            elif item.external:
                external_inputs[item.path].append(node.name)

    lines = ["flowchart LR"]
    for stage in ("discover", "import", "enrich", "other"):
        members = [n for n in nodes if stage_of(n) == stage]
        if not members:
            continue
        lines.append(f"  subgraph {stage}")
        for node in sorted(members, key=lambda n: n.name):
            lines.append(f"    {_mermaid_id(node.name)}[{node.name}]")
        lines.append("  end")
    for path, consumers in sorted(external_inputs.items()):
        ext_id = "ext_" + _mermaid_id(short(path))
        lines.append(f"  {ext_id}[({short(path)})]")
        for consumer in sorted(set(consumers)):
            lines.append(f"  {ext_id} --> {_mermaid_id(consumer)}")
    for (producer, consumer), paths in sorted(artifact_edges.items()):
        lines.append(f"  {_mermaid_id(producer)} -->|{_label(paths)}| {_mermaid_id(consumer)}")
    for (producer, consumer), paths in sorted(manifest_edges.items()):
        lines.append(f"  {_mermaid_id(producer)} -.->|{_label(paths)}| {_mermaid_id(consumer)}")
    for dead in report["dead_outputs"]:
        leaf_id = "dead_" + _mermaid_id(short(dead["path"]))
        lines.append(f"  {leaf_id}[/{short(dead['path'])} - no reader yet/]")
        lines.append(f"  {_mermaid_id(dead['node'])} --> {leaf_id}")
    return "\n".join(lines)


def node_table(nodes: list[type[Node]]) -> str:
    rows = ["| node | stage | reads | writes | manifest |", "|---|---|---|---|---|"]
    for node in sorted(nodes, key=lambda n: (stage_of(n), n.name)):
        reads = ", ".join(
            short(item.path) + (" (external)" if item.external else "")
            for item in node.inputs
        ) or "—"
        writes = ", ".join(short(item.path) for item in node.outputs) or "—"
        manifest = short(node.manifest) if node.manifest else "(parent)"
        rows.append(f"| `{node.name}` | {stage_of(node)} | {reads} | {writes} | {manifest} |")
    return "\n".join(rows)


def render(nodes: list[type[Node]]) -> str:
    report = check_graph(nodes)
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    findings = "\n".join(
        f"- `{key}`: **{len(report[key])}**" + (
            "".join(f"\n  - `{d['node']}` → `{short(d['path'])}`" for d in report[key])
            if key in ("dead_outputs", "phantom_inputs") and report[key] else ""
        )
        for key in ("two_writer_conflicts", "schema_mismatches", "cycles", "phantom_inputs", "dead_outputs")
    )
    return (
        f"# Ingestion pipeline DAG\n\n"
        f"Generated by `packs/ingestion/primitives/pipeline/visualize.py` from the\n"
        f"node declarations at `{sha}`. Do not edit by hand — regenerate.\n\n"
        f"```mermaid\n{mermaid(nodes, report)}\n```\n\n"
        f"Solid edges carry the labeled artifact; dashed edges are manifest reads;\n"
        f"cylinders are external inputs no node produces.\n\n"
        f"## Nodes\n\n{node_table(nodes)}\n\n"
        f"## Checker findings\n\n{findings}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the markdown here instead of stdout")
    args = parser.parse_args(argv)
    document = render(node_subclasses())
    if args.out:
        Path(args.out).write_text(document, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
