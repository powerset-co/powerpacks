"""Cope-with-old-run-dirs scrubs for the deep-search harness.

A harness command calls `scrub_results` first, right after loading
`results.json`; everything after the call may assume the current shape.
Each entry is dated with its removal condition: delete the entry when it
expires, and delete this module when it is empty.

Changelog:
  2026-09-03  scrub_results stamps the harness default retrieval limit onto
              iteration arms and pending payloads saved before
              `compile-pond --limit` existed (590361ef). Delete once no run
              dir under .powerpacks/deep-search predates 2026-09-03.
"""
from __future__ import annotations

from typing import Any


def scrub_results(results: dict[str, Any], *, default_limit: int) -> dict[str, Any]:
    """Runs saved before 2026-09-03 always retrieved at the harness default cap."""
    for iteration in results.get("iterations") or []:
        arm = iteration.get("arm")
        if isinstance(arm, dict) and "limit" not in arm:
            arm["limit"] = default_limit
    pending = results.get("pending_payload")
    if isinstance(pending, dict) and "limit" not in pending:
        pending["limit"] = default_limit
    return results
