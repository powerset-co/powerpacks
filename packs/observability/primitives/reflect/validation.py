"""Exact outbound schema validation; any expansion fails before authentication."""

from __future__ import annotations

import json
import re
from typing import Any

from .contracts import (
    EFFORTS,
    HARNESSES,
    INTERVENTIONS,
    PROVIDERS,
    ROLES,
    SEMVER_RE,
    WORKFLOWS,
    normalized_model,
)

SENSITIVE_TEXT_RE = re.compile(
    r"(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"https?://|/Users/|\\\\Users\\\\|\+?\d[\d ()-]{8,}\d)"
)
EXPORT_KEYS = {
    "schema_version", "report_kind", "workflow", "scope", "product",
    "runtime", "os", "stages", "observations", "privacy",
}
COUNT_BUCKETS = {"unknown", "0", "1-10", "11-100", "101-1k", "1k-10k", "10k+"}
DURATION_BUCKETS = {"unknown", "<1s", "1-10s", "10-60s", "1-5m", "5-30m", "30m-2h", "2h+"}
TOKEN_BUCKETS = {"unknown", "0", "1-1k", "1k-10k", "10k-100k", "100k-1m", "1m+"}
COST_BUCKETS = {"unknown", "$0", "<$0.01", "$0.01-0.10", "$0.10-1", "$1-10", "$10+"}
COUNT_METRICS = {
    "accounts", "calls", "candidates", "contacts", "errors", "input_rows",
    "matched", "messages", "output_rows", "people", "processed", "retries",
    "rows", "skipped", "unmatched",
}
OBSERVATION_CODES = {
    "manifest_unreadable", "stage_failed", "expected_gate", "workflow_blocked",
    "expected_intervention", "avoidable_intervention", "no_friction_observed",
    "artifact_state_unavailable",
}


class PrivacyProjectionError(ValueError):
    """The closed outbound contract was violated; nothing may be sent."""


def _valid_count_buckets(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and set(item) == {"metric", "bucket"}
        and item.get("metric") in COUNT_METRICS
        and item.get("bucket") in COUNT_BUCKETS
        for item in value
    )


def validate_export(
    payload: dict[str, Any],
    workflow_manifests: dict[str, tuple[tuple[str, str], ...]],
) -> None:
    """Fail closed if any field or value leaves the approved remote contract."""
    if set(payload) != EXPORT_KEYS or payload.get("workflow") not in WORKFLOWS:
        raise PrivacyProjectionError("unexpected top-level export fields")
    if payload.get("schema_version") != 1:
        raise PrivacyProjectionError("unexpected schema version")
    if payload.get("report_kind") != "workflow_reflection":
        raise PrivacyProjectionError("unexpected report kind")
    if payload.get("scope") != "observed_artifact_state":
        raise PrivacyProjectionError("unexpected scope")

    product = payload.get("product")
    if (
        not isinstance(product, dict)
        or set(product) != {"version", "channel"}
        or product.get("channel") not in {"stable", "rc", "edge", "unknown"}
        or (
            product.get("version") != "unknown"
            and not SEMVER_RE.fullmatch(str(product.get("version") or ""))
        )
    ):
        raise PrivacyProjectionError("unexpected product metadata")

    runtime = payload.get("runtime")
    runtime_fields = {
        "harness", "provider", "model", "effort", "role", "fallback_or_reroute",
        "token_bucket", "cost_bucket", "call_count_bucket", "latency_buckets",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise PrivacyProjectionError("unexpected runtime metadata")
    if (
        runtime.get("harness") not in HARNESSES
        or runtime.get("provider") not in PROVIDERS
        or runtime.get("effort") not in EFFORTS
        or runtime.get("role") not in ROLES
        or not isinstance(runtime.get("fallback_or_reroute"), bool)
        or runtime.get("token_bucket") not in TOKEN_BUCKETS
        or runtime.get("cost_bucket") not in COST_BUCKETS
        or runtime.get("call_count_bucket") not in COUNT_BUCKETS
        or (
            runtime.get("model") != "unknown"
            and normalized_model(runtime.get("model")) != runtime.get("model")
        )
    ):
        raise PrivacyProjectionError("unexpected runtime value")

    allowed_stages = {
        stage for stage, _path in workflow_manifests[payload["workflow"]]
    }
    latency = runtime.get("latency_buckets")
    if not isinstance(latency, list) or any(
        not isinstance(item, dict)
        or set(item) != {"stage", "bucket"}
        or item.get("stage") not in allowed_stages
        or item.get("bucket") not in DURATION_BUCKETS
        for item in latency
    ):
        raise PrivacyProjectionError("unexpected latency projection")

    stages = payload.get("stages")
    stage_fields = {
        "stage", "artifact_state", "status", "model", "effort",
        "duration_bucket", "count_buckets", "cache_outcome",
        "approval_category", "error_code",
    }
    if not isinstance(stages, list) or any(
        not isinstance(stage, dict)
        or set(stage) != stage_fields
        or stage.get("stage") not in allowed_stages
        or stage.get("artifact_state") not in {"missing", "present", "unreadable"}
        or stage.get("status") not in {"unknown", "blocked", "completed", "failed", "in_progress", "partial"}
        or (
            stage.get("model") != "unknown"
            and normalized_model(stage.get("model")) != stage.get("model")
        )
        or stage.get("effort") not in EFFORTS
        or stage.get("duration_bucket") not in DURATION_BUCKETS
        or not _valid_count_buckets(stage.get("count_buckets"))
        or stage.get("cache_outcome") not in {"unknown", "reused", "no-op", "incremental", "full"}
        or stage.get("approval_category") not in {"none", "expected_approval", "oauth", "os_permission", "other_expected_action"}
        or stage.get("error_code") not in {"none", "stage_failed", "manifest_unreadable"}
        for stage in stages
    ):
        raise PrivacyProjectionError("unexpected stage projection")

    observations = payload.get("observations")
    if not isinstance(observations, list) or any(
        not isinstance(observation, dict)
        or not set(observation).issubset({"code", "stage", "category"})
        or observation.get("code") not in OBSERVATION_CODES
        or observation.get("stage") not in allowed_stages | {"workflow"}
        or observation.get("category", "none") not in set(INTERVENTIONS) | {"none"}
        for observation in observations
    ):
        raise PrivacyProjectionError("unexpected observation projection")

    os_meta = payload.get("os")
    if (
        not isinstance(os_meta, dict)
        or set(os_meta) != {"family", "major"}
        or os_meta.get("family") not in {"macos", "linux", "windows", "other"}
        or not re.fullmatch(r"(?:unknown|\d+)", str(os_meta.get("major") or ""))
    ):
        raise PrivacyProjectionError("unexpected OS metadata")
    privacy = payload.get("privacy")
    if (
        not isinstance(privacy, dict)
        or set(privacy) != {
            "projection", "raw_manifests_included", "session_transcript_included",
            "free_text_included", "sanitizer_version", "dropped_raw_field_count_bucket",
        }
        or privacy.get("projection") != "closed_allowlist"
        or privacy.get("raw_manifests_included") is not False
        or privacy.get("session_transcript_included") is not False
        or privacy.get("free_text_included") is not False
        or privacy.get("sanitizer_version") != 1
        or privacy.get("dropped_raw_field_count_bucket") not in COUNT_BUCKETS
    ):
        raise PrivacyProjectionError("unexpected privacy metadata")
    if SENSITIVE_TEXT_RE.search(json.dumps(payload, sort_keys=True)):
        raise PrivacyProjectionError("sensitive-looking text in export")
