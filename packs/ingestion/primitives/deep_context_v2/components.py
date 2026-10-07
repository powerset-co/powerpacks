"""Connected components: A-B and B-C are one group. Used by free merge and dedupe.

Created: 2026-10-06
"""
from __future__ import annotations


def _root(links: dict[str, str], node: str) -> str:
    """The representative of a node's group (union-find with path halving)."""
    while links[node] != node:
        links[node] = links[links[node]]
        node = links[node]
    return node


def connected_components(edges: list[tuple[str, str]]) -> list[list[str]]:
    """Every group of nodes joined by the edges, each group sorted, in order of first appearance."""
    links: dict[str, str] = {}
    for first, second in edges:
        links.setdefault(first, first)
        links.setdefault(second, second)
        links[_root(links, first)] = _root(links, second)
    groups: dict[str, list[str]] = {}
    for node in links:
        groups.setdefault(_root(links, node), []).append(node)
    components: list[list[str]] = []
    for members in groups.values():
        components.append(sorted(members))
    return components
