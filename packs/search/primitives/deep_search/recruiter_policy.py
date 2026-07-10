"""Versioned recruiter defaults and preference resolution for deep search.

The checked-in JSON is the policy source of truth. This module intentionally
uses only the standard library so prompt builders and evaluators can share the
same policy without adding runtime schema-validation dependencies.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SEARCH_DIR = Path(__file__).resolve().parents[2]
POLICY_PATH = SEARCH_DIR / "policies" / "recruiter-defaults.json"
PREFERENCES_SCHEMA_PATH = SEARCH_DIR / "schemas" / "recruiter-preferences.schema.json"

CANONICAL_HIRE_STAGES = ("founding_early", "scaling_late")
HIRE_STAGE_ALIASES = {
    "founding_early": "founding_early",
    "founding": "founding_early",
    "founding_stage": "founding_early",
    "pre_seed": "founding_early",
    "seed": "founding_early",
    "early": "founding_early",
    "early_stage": "founding_early",
    "scaling_late": "scaling_late",
    "growth": "scaling_late",
    "growth_stage": "scaling_late",
    "scale": "scaling_late",
    "scaling": "scaling_late",
    "late": "scaling_late",
    "late_stage": "scaling_late",
    "mature": "scaling_late",
}

EXCELLENCE_DIMENSIONS = ("trajectory", "impact", "pedigree")
ALLOWED_OVERRIDE_FIELDS = (
    "hire_stage",
    "excellence_weights",
    "pedigree_policy",
    "current_founder_c_suite_for_non_exec_ic",
)
PEDIGREE_POLICIES = {"positive_prior_not_gate", "ignore"}
FOUNDER_C_SUITE_POLICIES = {"default_out", "eligible", "review"}
_POLICY_KEYS = {
    "policy_id",
    "version",
    "description",
    "precedence",
    "canonical_hire_stages",
    "hire_stage_aliases",
    "defaults",
    "overridable",
}
_DEFAULT_KEYS = {
    "hire_stage",
    "evidence_priority",
    "excellence_weights",
    "pedigree_policy",
    "current_founder_c_suite_for_non_exec_ic",
    "higher_hands_on_ic",
    "fairness",
}
_FAIRNESS_KEYS = {
    "job_related_evidence_only",
    "exclude_protected_attributes",
    "exclude_protected_attribute_proxies",
}
_RESOLVED_KEYS = {
    "policy_id",
    "policy_version",
    "default_source",
    "precedence",
    "preferences",
    "provenance",
}
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class RecruiterPolicyError(ValueError):
    """Raised when policy or preference data violates the recruiter contract."""


def _normalized_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def canonicalize_hire_stage(value: str) -> str:
    """Return a canonical hire stage, accepting common separators and aliases."""
    if not isinstance(value, str) or not value.strip():
        raise RecruiterPolicyError("hire_stage must be a non-empty string")
    token = _normalized_token(value)
    try:
        return HIRE_STAGE_ALIASES[token]
    except KeyError as exc:
        accepted = ", ".join(CANONICAL_HIRE_STAGES)
        raise RecruiterPolicyError(f"unknown hire_stage {value!r}; canonical stages: {accepted}") from exc


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise RecruiterPolicyError(f"{context} has invalid fields: {', '.join(details)}")


def _validate_weights(
    weights: Any,
    *,
    context: str,
    require_all: bool,
) -> dict[str, float]:
    if not isinstance(weights, Mapping):
        raise RecruiterPolicyError(f"{context} must be an object")
    keys = set(weights)
    unknown = sorted(keys - set(EXCELLENCE_DIMENSIONS))
    missing = sorted(set(EXCELLENCE_DIMENSIONS) - keys) if require_all else []
    if unknown or missing or (not require_all and not keys):
        details = []
        if unknown:
            details.append(f"unexpected {unknown}")
        if missing:
            details.append(f"missing {missing}")
        if not require_all and not keys:
            details.append("at least one weight is required")
        raise RecruiterPolicyError(f"{context} has invalid fields: {', '.join(details)}")

    out: dict[str, float] = {}
    for key, raw in weights.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RecruiterPolicyError(f"{context}.{key} must be a number")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise RecruiterPolicyError(f"{context}.{key} must be finite and non-negative")
        out[key] = value
    return out


def _normalize_weights(weights: Mapping[str, float]) -> dict[str, float]:
    total = math.fsum(weights[key] for key in EXCELLENCE_DIMENSIONS)
    if total <= 0:
        raise RecruiterPolicyError("resolved excellence_weights must contain at least one positive weight")
    normalized: dict[str, float] = {}
    for key in EXCELLENCE_DIMENSIONS[:-1]:
        normalized[key] = weights[key] / total
    normalized[EXCELLENCE_DIMENSIONS[-1]] = 1.0 - sum(normalized.values())
    return normalized


def _validated_policy(document: Any) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise RecruiterPolicyError("recruiter policy must be a JSON object")
    _require_exact_keys(document, _POLICY_KEYS, "recruiter policy")

    policy_id = document["policy_id"]
    version = document["version"]
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise RecruiterPolicyError("recruiter policy.policy_id must be a non-empty string")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise RecruiterPolicyError("recruiter policy.version must be a semantic version")
    if not isinstance(document["description"], str) or not document["description"].strip():
        raise RecruiterPolicyError("recruiter policy.description must be a non-empty string")
    if document["precedence"] != ["user", "jd", "default"]:
        raise RecruiterPolicyError("recruiter policy.precedence must be user > jd > default")
    if document["canonical_hire_stages"] != list(CANONICAL_HIRE_STAGES):
        raise RecruiterPolicyError("recruiter policy canonical_hire_stages drifted from the Python API")
    if document["hire_stage_aliases"] != HIRE_STAGE_ALIASES:
        raise RecruiterPolicyError("recruiter policy hire_stage_aliases drifted from the Python API")
    if document["overridable"] != list(ALLOWED_OVERRIDE_FIELDS):
        raise RecruiterPolicyError("recruiter policy.overridable drifted from the preference schema")

    defaults = document["defaults"]
    if not isinstance(defaults, Mapping):
        raise RecruiterPolicyError("recruiter policy.defaults must be an object")
    _require_exact_keys(defaults, _DEFAULT_KEYS, "recruiter policy.defaults")
    if defaults["hire_stage"] not in CANONICAL_HIRE_STAGES:
        raise RecruiterPolicyError("default hire_stage must be canonical")

    evidence_priority = defaults["evidence_priority"]
    expected_evidence = {"direct_recent_demonstrated_work", "trajectory", "impact", "pedigree"}
    if (
        not isinstance(evidence_priority, list)
        or set(evidence_priority) != expected_evidence
        or len(evidence_priority) != len(expected_evidence)
        or evidence_priority[0] != "direct_recent_demonstrated_work"
        or evidence_priority.index("pedigree") < evidence_priority.index("trajectory")
        or evidence_priority.index("pedigree") < evidence_priority.index("impact")
    ):
        raise RecruiterPolicyError(
            "default evidence_priority must put direct recent work first and pedigree after trajectory/impact"
        )

    weights = _validate_weights(
        defaults["excellence_weights"],
        context="recruiter policy.defaults.excellence_weights",
        require_all=True,
    )
    if weights["trajectory"] <= weights["pedigree"] or weights["impact"] <= weights["pedigree"]:
        raise RecruiterPolicyError("default trajectory and impact weights must each exceed pedigree")
    if defaults["pedigree_policy"] != "positive_prior_not_gate":
        raise RecruiterPolicyError("default pedigree_policy must be positive_prior_not_gate")
    if defaults["current_founder_c_suite_for_non_exec_ic"] != "default_out":
        raise RecruiterPolicyError("default current founder/C-suite policy must be default_out for non-executive IC roles")
    if defaults["higher_hands_on_ic"] != "eligible":
        raise RecruiterPolicyError("higher hands-on IC levels must remain eligible")

    fairness = defaults["fairness"]
    if not isinstance(fairness, Mapping):
        raise RecruiterPolicyError("recruiter policy.defaults.fairness must be an object")
    _require_exact_keys(fairness, _FAIRNESS_KEYS, "recruiter policy.defaults.fairness")
    if any(fairness[key] is not True for key in _FAIRNESS_KEYS):
        raise RecruiterPolicyError("all recruiter fairness safeguards must be enabled")

    result = copy.deepcopy(dict(document))
    result["defaults"]["excellence_weights"] = _normalize_weights(weights)
    return result


def load_recruiter_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and manually validate a recruiter policy JSON document."""
    policy_path = Path(path) if path is not None else POLICY_PATH
    try:
        document = json.loads(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecruiterPolicyError(f"could not read recruiter policy {policy_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RecruiterPolicyError(f"invalid recruiter policy JSON {policy_path}: {exc}") from exc
    return _validated_policy(document)


def validate_recruiter_preferences(
    preferences: Mapping[str, Any] | None,
    *,
    source: str = "preferences",
) -> dict[str, Any]:
    """Validate and copy one partial override object without ``jsonschema``."""
    if preferences is None:
        return {}
    if not isinstance(preferences, Mapping):
        raise RecruiterPolicyError(f"{source} must be an object")
    unknown = sorted(set(preferences) - set(ALLOWED_OVERRIDE_FIELDS))
    if unknown:
        raise RecruiterPolicyError(f"{source} has unsupported fields: {unknown}")

    out: dict[str, Any] = {}
    if "hire_stage" in preferences:
        stage = preferences["hire_stage"]
        token = _normalized_token(stage) if isinstance(stage, str) else ""
        if token not in HIRE_STAGE_ALIASES:
            raise RecruiterPolicyError(f"{source}.hire_stage must be a canonical stage or normalized alias")
        out["hire_stage"] = HIRE_STAGE_ALIASES[token]
    if "excellence_weights" in preferences:
        out["excellence_weights"] = _validate_weights(
            preferences["excellence_weights"],
            context=f"{source}.excellence_weights",
            require_all=False,
        )
    if "pedigree_policy" in preferences:
        value = preferences["pedigree_policy"]
        if value not in PEDIGREE_POLICIES:
            raise RecruiterPolicyError(f"{source}.pedigree_policy must be one of {sorted(PEDIGREE_POLICIES)}")
        out["pedigree_policy"] = value
    if "current_founder_c_suite_for_non_exec_ic" in preferences:
        value = preferences["current_founder_c_suite_for_non_exec_ic"]
        if value not in FOUNDER_C_SUITE_POLICIES:
            raise RecruiterPolicyError(
                f"{source}.current_founder_c_suite_for_non_exec_ic must be one of "
                f"{sorted(FOUNDER_C_SUITE_POLICIES)}"
            )
        out["current_founder_c_suite_for_non_exec_ic"] = value
    return copy.deepcopy(out)


def _provenance(source: str, default_source: str, **extra: Any) -> dict[str, Any]:
    return {"source": source, "default_source": default_source, **extra}


def resolve_recruiter_preferences(
    *,
    user_preferences: Mapping[str, Any] | None = None,
    jd_preferences: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve ``user > JD > default`` and return values plus leaf provenance."""
    policy_doc = load_recruiter_policy() if policy is None else _validated_policy(policy)
    default_source = f"{policy_doc['policy_id']}@{policy_doc['version']}"
    values = copy.deepcopy(policy_doc["defaults"])
    provenance = {
        "hire_stage": _provenance("default", default_source),
        "evidence_priority": _provenance("default", default_source),
        "pedigree_policy": _provenance("default", default_source),
        "current_founder_c_suite_for_non_exec_ic": _provenance("default", default_source),
        "higher_hands_on_ic": _provenance("default", default_source),
        **{
            f"excellence_weights.{key}": _provenance("default", default_source)
            for key in EXCELLENCE_DIMENSIONS
        },
        **{
            f"fairness.{key}": _provenance("default", default_source)
            for key in _FAIRNESS_KEYS
        },
    }

    jd = validate_recruiter_preferences(jd_preferences, source="jd_preferences")
    user = validate_recruiter_preferences(user_preferences, source="user_preferences")
    for source, overrides in (("jd", jd), ("user", user)):
        for key in (
            "hire_stage",
            "pedigree_policy",
            "current_founder_c_suite_for_non_exec_ic",
        ):
            if key in overrides:
                values[key] = overrides[key]
                provenance[key] = _provenance(source, default_source)
        for key, weight in overrides.get("excellence_weights", {}).items():
            values["excellence_weights"][key] = weight
            provenance[f"excellence_weights.{key}"] = _provenance(source, default_source)

    if values["pedigree_policy"] == "ignore":
        pedigree_source = provenance["pedigree_policy"]["source"]
        values["excellence_weights"]["pedigree"] = 0.0
        provenance["excellence_weights.pedigree"] = _provenance(
            pedigree_source,
            default_source,
            derived_from="pedigree_policy",
        )
    values["excellence_weights"] = _normalize_weights(values["excellence_weights"])

    return {
        "policy_id": policy_doc["policy_id"],
        "policy_version": policy_doc["version"],
        "default_source": default_source,
        "precedence": list(policy_doc["precedence"]),
        "preferences": values,
        "provenance": provenance,
    }


def validate_resolved_recruiter_preferences(resolved: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a reviewed policy snapshot without silently rewriting user edits."""
    if not isinstance(resolved, Mapping):
        raise RecruiterPolicyError("resolved recruiter policy must be an object")
    _require_exact_keys(resolved, _RESOLVED_KEYS, "resolved recruiter policy")

    policy_id = resolved["policy_id"]
    version = resolved["policy_version"]
    default_source = resolved["default_source"]
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise RecruiterPolicyError("resolved recruiter policy.policy_id must be a non-empty string")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise RecruiterPolicyError("resolved recruiter policy.policy_version must be semantic")
    if default_source != f"{policy_id}@{version}":
        raise RecruiterPolicyError("resolved recruiter policy.default_source must identify its policy version")
    if resolved["precedence"] != ["user", "jd", "default"]:
        raise RecruiterPolicyError("resolved recruiter policy.precedence must be user > jd > default")

    values = resolved["preferences"]
    if not isinstance(values, Mapping):
        raise RecruiterPolicyError("resolved recruiter policy.preferences must be an object")
    _require_exact_keys(values, _DEFAULT_KEYS, "resolved recruiter policy.preferences")
    if values["hire_stage"] not in CANONICAL_HIRE_STAGES:
        raise RecruiterPolicyError("resolved recruiter policy hire_stage must be canonical")

    evidence = values["evidence_priority"]
    expected_evidence = {
        "direct_recent_demonstrated_work",
        "trajectory",
        "impact",
        "pedigree",
    }
    if (
        not isinstance(evidence, list)
        or len(evidence) != len(expected_evidence)
        or set(evidence) != expected_evidence
        or evidence[0] != "direct_recent_demonstrated_work"
        or evidence.index("pedigree") < evidence.index("trajectory")
        or evidence.index("pedigree") < evidence.index("impact")
    ):
        raise RecruiterPolicyError(
            "resolved evidence_priority must put direct work first and pedigree after trajectory/impact"
        )

    weights = _validate_weights(
        values["excellence_weights"],
        context="resolved recruiter policy.preferences.excellence_weights",
        require_all=True,
    )
    if not math.isclose(math.fsum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise RecruiterPolicyError("resolved excellence_weights must sum to 1")
    if values["pedigree_policy"] not in PEDIGREE_POLICIES:
        raise RecruiterPolicyError("resolved recruiter policy has an invalid pedigree_policy")
    if values["pedigree_policy"] == "ignore" and weights["pedigree"] != 0:
        raise RecruiterPolicyError("pedigree weight must be 0 when pedigree_policy is ignore")
    if values["current_founder_c_suite_for_non_exec_ic"] not in FOUNDER_C_SUITE_POLICIES:
        raise RecruiterPolicyError("resolved recruiter policy has an invalid founder/C-suite policy")
    if values["higher_hands_on_ic"] != "eligible":
        raise RecruiterPolicyError("higher hands-on IC levels must remain eligible")

    fairness = values["fairness"]
    if not isinstance(fairness, Mapping):
        raise RecruiterPolicyError("resolved recruiter policy fairness must be an object")
    _require_exact_keys(fairness, _FAIRNESS_KEYS, "resolved recruiter policy.preferences.fairness")
    if any(fairness[key] is not True for key in _FAIRNESS_KEYS):
        raise RecruiterPolicyError("all resolved recruiter fairness safeguards must be enabled")

    expected_provenance = {
        "hire_stage",
        "evidence_priority",
        "pedigree_policy",
        "current_founder_c_suite_for_non_exec_ic",
        "higher_hands_on_ic",
        *{f"excellence_weights.{key}" for key in EXCELLENCE_DIMENSIONS},
        *{f"fairness.{key}" for key in _FAIRNESS_KEYS},
    }
    provenance = resolved["provenance"]
    if not isinstance(provenance, Mapping):
        raise RecruiterPolicyError("resolved recruiter policy.provenance must be an object")
    _require_exact_keys(provenance, expected_provenance, "resolved recruiter policy.provenance")
    for key, origin in provenance.items():
        if not isinstance(origin, Mapping):
            raise RecruiterPolicyError(f"resolved provenance {key!r} must be an object")
        allowed = {"source", "default_source", "derived_from"}
        if set(origin) - allowed or "source" not in origin or "default_source" not in origin:
            raise RecruiterPolicyError(f"resolved provenance {key!r} has invalid fields")
        if origin["source"] not in {"user", "jd", "default"}:
            raise RecruiterPolicyError(f"resolved provenance {key!r} has invalid source")
        if origin["default_source"] != default_source:
            raise RecruiterPolicyError(f"resolved provenance {key!r} has inconsistent default_source")

    return copy.deepcopy(dict(resolved))


def render_recruiter_prompt(resolved: Mapping[str, Any] | None = None) -> str:
    """Render the resolved policy as a concise judge/sourcer prompt section."""
    resolved_doc = (
        resolve_recruiter_preferences()
        if resolved is None
        else validate_resolved_recruiter_preferences(resolved)
    )
    values = resolved_doc["preferences"]
    provenance = resolved_doc.get("provenance", {})
    weights = values.get("excellence_weights", {})

    stage_source = (provenance.get("hire_stage") or {}).get("source", "unknown")
    weight_text = ", ".join(
        f"{key} {100 * float(weights[key]):g}%"
        for key in EXCELLENCE_DIMENSIONS
    )
    pedigree_policy = values.get("pedigree_policy")
    if pedigree_policy == "ignore":
        pedigree_line = (
            "Ignore pedigree entirely. Missing pedigree is floor-neutral and cannot block top-tier by itself."
        )
    else:
        pedigree_line = (
            "Pedigree is an upside-only positive prior, never a gate. Missing pedigree is floor-neutral: "
            "it cannot lower a candidate or block top-tier by itself."
        )

    founder_policy = values.get("current_founder_c_suite_for_non_exec_ic")
    if founder_policy == "eligible":
        founder_line = (
            "For non-executive IC targets, current founders/C-suite remain eligible; infer willingness only from "
            "job-relevant evidence, not title alone."
        )
    elif founder_policy == "review":
        founder_line = (
            "For non-executive IC targets, send current founder/C-suite hireability to review rather than "
            "hard-gating on title."
        )
    else:
        founder_line = (
            "For non-executive IC targets only, current founders/C-suite default out for likely hireability, "
            "not candidate quality; executive targets are evaluated against their target level."
        )

    policy_name = f"{resolved_doc.get('policy_id', 'recruiter-policy')}@{resolved_doc.get('policy_version', '?')}"
    return "\n".join(
        [
            f"=== RECRUITER POLICY ({policy_name}) ===",
            "- Use only job-relevant evidence. Never use protected attributes or proxies for protected attributes.",
            "- Preference precedence is explicit user direction, then JD evidence, then these defaults.",
            f"- Hire stage: {values.get('hire_stage')} (source: {stage_source}).",
            "- Evidence priority: direct, recent demonstrated work first; then trajectory and concrete impact; "
            "pedigree last.",
            f"- Excellence weights: {weight_text}.",
            f"- {pedigree_line}",
            f"- {founder_line}",
            "- Higher hands-on IC levels remain eligible; never reject staff/principal/distinguished ICs as "
            "too senior solely because of IC level.",
        ]
    )
