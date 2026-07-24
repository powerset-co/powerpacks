#!/usr/bin/env python3
"""Shared spend-gate contract for the spend-bearing ingestion orchestrators.

One home for the exit-code + CLI-emit machinery that enrich_people,
imports/linkedin/network_import, and discover/twitter/network_import each used to
re-implement:

- ``EXIT_NEEDS_APPROVAL`` — the ``run`` exit code when paid work is gated behind
  ``--approve-spend``: nonzero (so callers/CI notice) but clean (not a failure).
- ``needs_approval_payload`` — builds the "a named spend step is blocked" manifest
  payload (step + provider + estimated_calls + message [+ continue_command]). This
  is the canonical shape for spend gates that name the blocked step (twitter today;
  the shape any future step-gated vertical should reuse). enrich_people's gate is a
  DIFFERENT shape (a single implicit RapidAPI fetch reported as credits, not a named
  step) and keeps its own literal — see enrich_people.EnrichPeople.run.
- ``exit_code_for_status`` — maps a manifest status string to a process exit code.
- ``manifest_emit_payload`` — the terse JSON a spend-gated ``run`` emits from its
  typed manifest object (status + manifest path + counts/artifacts, plus the
  needs_approval / error detail when present).

Changelog:
  2026-07-23 (audit class-sharing): created. EXIT_NEEDS_APPROVAL (was a per-file
    NEEDS_APPROVAL_CODE = 20 in enrich + linkedin, inline 20 in twitter),
    exit_code_for_status (was byte-identical in enrich + linkedin, inline in
    twitter's cmd_run), and manifest_emit_payload (was byte-identical in enrich +
    linkedin) moved here; needs_approval_payload centralizes twitter's step-gate
    payload shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# `run` exit code when paid provider work is gated behind --approve-spend:
# nonzero (so callers/CI notice) but clean (not a failure).
EXIT_NEEDS_APPROVAL = 20


def needs_approval_payload(
    *,
    step: str,
    provider: str,
    estimated_calls: int,
    message: str,
    continue_command: str | None = None,
) -> dict[str, Any]:
    """Build the manifest ``needs_approval`` payload for a blocked named spend step.

    The canonical shape for spend gates that stop at a specific step and name the
    provider they would call: ``{step, provider, estimated_calls, message}`` plus an
    optional ``continue_command`` (dropped when None). enrich_people's credit-gate is
    a different shape and does not use this builder."""
    payload: dict[str, Any] = {
        "step": step,
        "provider": provider,
        "estimated_calls": estimated_calls,
        "message": message,
    }
    if continue_command is not None:
        payload["continue_command"] = continue_command
    return payload


def exit_code_for_status(status: str) -> int:
    """Map a run's terminal manifest status to a process exit code: completed -> 0,
    needs_approval -> EXIT_NEEDS_APPROVAL, everything else (failed/unknown) -> 1."""
    return {"completed": 0, "needs_approval": EXIT_NEEDS_APPROVAL, "failed": 1}.get(status, 1)


def manifest_emit_payload(manifest: Any) -> dict[str, Any]:
    """The terse JSON a spend-gated ``run`` emits from its typed manifest object.

    ``manifest`` is any orchestrator manifest exposing ``status``, ``artifact_dir``,
    ``counts``, ``artifacts``, ``needs_approval``, and ``error`` (e.g.
    enrich_people.EnrichManifest, linkedin LinkedInImportManifest). needs_approval /
    error are included only when present."""
    payload: dict[str, Any] = {
        "status": manifest.status,
        "artifact_dir": manifest.artifact_dir,
        "manifest": str(Path(manifest.artifact_dir) / "manifest.json"),
        "counts": manifest.counts,
        "artifacts": manifest.artifacts,
    }
    if manifest.needs_approval is not None:
        payload["needs_approval"] = manifest.needs_approval
    if manifest.error is not None:
        payload["error"] = manifest.error
    return payload
