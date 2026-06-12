"""Shared probe artifact contracts for the profile-search (JD) flow.

The canonical shape of ``probe_summaries.json`` is a bare JSON list of probe
objects. Agent-authored runs sometimes wrap the list in an object
(``{"probes": [...]}`` or ``{"probe_summaries": [...]}``); every consumer must
tolerate both shapes through ``coerce_probe_list`` so the contract lives in
one place instead of drifting per primitive.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def coerce_probe_list(doc: Any) -> list[dict[str, Any]]:
    """Normalize a probe_summaries document to a list of probe dicts.

    Accepts a bare list or an object wrapper with a ``probes`` /
    ``probe_summaries`` key. Raises ``ValueError`` on any other shape so
    callers fail loudly instead of iterating string keys.
    """
    if isinstance(doc, list):
        probes = doc
    elif isinstance(doc, dict):
        probes = doc.get("probes") or doc.get("probe_summaries") or []
        if not isinstance(probes, list):
            raise ValueError("probe_summaries object must hold a list under 'probes' or 'probe_summaries'")
    else:
        raise ValueError("probe_summaries must be a list or an object with a 'probes' key")
    bad = [p for p in probes if not isinstance(p, dict)]
    if bad:
        raise ValueError(f"probe_summaries entries must be objects, got {type(bad[0]).__name__}")
    return probes


def load_probe_summaries(path: Path) -> list[dict[str, Any]]:
    """Read and normalize probe_summaries.json from *path*."""
    return coerce_probe_list(json.loads(path.read_text()))
