"""Compose one closed reflection export from allowlisted manifest state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import SCHEMA_VERSION, count_bucket, key_count, normalized_intervention
from .manifests import WORKFLOW_MANIFESTS, stage_projection
from .metadata import os_metadata, product_metadata, runtime_metadata


def build_export(
    *,
    root: Path,
    workflow: str,
    harness: str,
    model: str,
    provider: str,
    effort: str,
    role: str,
    intervention: str,
    fallback: bool,
) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []
    observations: list[dict[str, str]] = []
    raw_payloads: list[dict[str, Any]] = []
    for stage, relative in WORKFLOW_MANIFESTS[workflow]:
        path = root / relative
        projected, observation = stage_projection(stage, path)
        stages.append(projected)
        if observation and observation not in observations:
            observations.append(observation)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw_payloads.append(raw)
        except (OSError, json.JSONDecodeError):
            pass
    intervention = normalized_intervention(intervention)
    if intervention != "none":
        observations.append({
            "code": (
                "expected_intervention"
                if intervention in {"expected_approval", "oauth", "os_permission"}
                else "avoidable_intervention"
            ),
            "stage": "workflow",
            "category": intervention,
        })
    if all(stage["artifact_state"] == "missing" for stage in stages):
        observations.append({"code": "artifact_state_unavailable", "stage": "workflow"})
    elif not observations:
        observations.append({"code": "no_friction_observed", "stage": "workflow"})
    source_field_count = sum(key_count(payload) for payload in raw_payloads)
    return {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "workflow_reflection",
        "workflow": workflow,
        "scope": "observed_artifact_state",
        "product": product_metadata(root),
        "runtime": runtime_metadata(
            harness=harness,
            model=model,
            provider=provider,
            effort=effort,
            role=role,
            fallback=fallback,
            stages=stages,
            raw_manifests=raw_payloads,
        ),
        "os": os_metadata(),
        "stages": stages,
        "observations": observations,
        "privacy": {
            "projection": "closed_allowlist",
            "raw_manifests_included": False,
            "session_transcript_included": False,
            "free_text_included": False,
            "sanitizer_version": 1,
            "dropped_raw_field_count_bucket": count_bucket(source_field_count),
        },
    }
