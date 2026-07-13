"""Plan critic: pre-Review sanity check on the generated deep-search plan.

The plan is the highest-leverage artifact in deep mode (the core must-haves ARE the
shortlist gate) and plan generation has a measured defect pattern: missing JD pillars
in `core` (cost the two best candidates in the audited AgentMail run) and usable_cutoff
prose that contradicts the judge rubric's IC-track rule (all three fresh plans on
2026-07-04 had level-based or self-contradictory cutoffs, caught only by hand).

Checks:
- deterministic (code): hire_stage is a valid enum value.
- LLM (one cheap call): every JD responsibility pillar is covered by a `core` trait;
  the usable_cutoff doesn't gate hands-on IC levels (staff/principal/lead-IC) for an
  IC-target role; internal contradictions.

Writes <run>/plan_critic.json; deep mode surfaces it at the Review checkpoint. The
critic ADVISES — the human at Review decides. Exit code stays 0 for advisory findings.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from openai_client import make_openai_client  # noqa: E402

try:  # direct script execution
    from location_scope import location_scope_from_plan
    import recruiter_policy as recruiter_policy
except ImportError:  # module execution
    from .location_scope import location_scope_from_plan
    from . import recruiter_policy

# Judge-grade model: the critic runs ONCE per search, so quality > pennies here
# (gpt-4.1 measurably missed a self-contradictory cutoff and over-flagged soft pillars).
DEFAULT_MODEL = os.environ.get("POWERPACKS_PLAN_CRITIC_MODEL", "gpt-5.4")
VALID_HIRE_STAGES = set(recruiter_policy.CANONICAL_HIRE_STAGES)

SYSTEM = (
    "You review a recruiting search plan against the job description it was generated from. "
    "Report ONLY defects that would change WHO gets shortlisted; do not restyle the plan. "
    "Precision over recall: a false flag wastes the reviewer's one checkpoint. When unsure, "
    "do not flag.\n\n"
    "Check, in order:\n"
    "1. MISSING CORE PILLARS: a pillar is a distinct TECHNICAL capability area the role's day "
    "job depends on (e.g. 'low-latency serving systems', 'consensus protocols', 'ETL/data-lake "
    "architecture'). List any such pillar that no `core`-tier must-have covers. Do NOT flag "
    "soft/process responsibilities (collaboration, communication, monitoring existing systems, "
    "evaluating tools, customer advocacy) — those are table_stakes by definition and must never "
    "be core.\n"
    "2. CUTOFF CONTRADICTIONS — test by SIMULATION: take a hands-on senior IC, a staff IC, a "
    "principal IC, and a tech-lead IC, and apply the usable_cutoff text literally to each. If it "
    "marks ANY of them too_senior for an IC-target role, that is a defect — only the current "
    "management/exec track may gate. Also flag direct self-contradictions, e.g. this REAL defect: "
    "'Hire senior_ic and staff_ic; staff engineers or higher are too_senior' (hires staff while "
    "gating staff).\n"
    "3. GEO SCOPE: if the JD states any geographic hiring restriction (including country/region-"
    "restricted remote work) but the plan's search_scope.location is null, flag it — required-location "
    "sourcing will be disabled for the WHOLE run unless the reviewer sets it.\n"
    "4. CORE PATH PROVENANCE: if the JD explicitly offers independently viable alternatives but "
    "the corresponding singleton groups have source='default', flag that they need source='jd' so "
    "unselected paths do not lower ranking. Do not infer alternatives merely because the plan uses "
    "default singleton membership groups.\n"
    "5. ANYTHING ELSE that would misgate candidates (wrong target level for the JD, a core trait "
    "that is generic table-stakes in disguise).\n\n"
    'Return strict JSON: {"missing_core_pillars": ["<pillar> — <the JD text implying it>"], '
    '"cutoff_issues": ["<issue>"], "other_issues": ["<issue>"], "verdict": "ok|needs_edits"}'
)


def supports_custom_temperature(model: str) -> bool:
    """Match the repo's model-family contract for Chat Completions options."""
    normalized = model.strip().lower()
    return not (normalized.startswith("gpt-5") or normalized.startswith("o"))


def deterministic_checks(plan: dict[str, Any], *, backend: str | None = None) -> list[str]:
    issues: list[str] = []
    try:
        location_scope_from_plan(plan)
    except ValueError as exc:
        issues.append(str(exc))
    stage = (plan.get("hire_stage") or "").strip()
    if stage and stage not in VALID_HIRE_STAGES:
        issues.append(f"hire_stage '{stage}' is off-enum (must be one of {sorted(VALID_HIRE_STAGES)}); "
                      "the judge's hire-stage bar will not match")
    policy_stage = (((plan.get("recruiter_policy") or {}).get("preferences") or {}).get("hire_stage"))
    if policy_stage and stage != policy_stage:
        issues.append(f"hire_stage '{stage}' disagrees with recruiter_policy hire_stage '{policy_stage}'")
    core = {str(t.get("trait") or "").strip() for t in (plan.get("traits", {}) or {}).get("must_have", [])
            if t.get("tier") == "core" and str(t.get("trait") or "").strip()}
    if not core:
        issues.append("no must_have trait is tagged core — the shortlist core-gate will fall back to score-only")
    else:
        grouped = {str(t).strip() for group in plan.get("core_groups") or []
                   for t in (group.get("all_of") or []) if str(t).strip()}
        if not plan.get("core_groups"):
            issues.append("core traits exist but core_groups is empty — the gate is ambiguous")
        missing = sorted(core - grouped)
        unknown = sorted(grouped - core)
        if missing:
            issues.append(f"core traits missing from core_groups: {missing}")
        if unknown:
            issues.append(f"core_groups reference non-core traits: {unknown}")
    # Conjunctivity guard: measured on the audited benchmark, an all-of-3 group cut a
    # validated 22-person shortlist to 1; bigger groups ship empty shortlists.
    for group in plan.get("core_groups") or []:
        n = len(group.get("all_of") or [])
        if n > 1:
            suffix = (
                "; approval rejects groups larger than 3"
                if n > 3
                else "; confirm this conjunction deliberately at Review"
            )
            issues.append(
                f"core group '{group.get('name')}' requires ALL {n} traits at experienced+ — "
                "conjunctions sharply reduce recall; prefer alternative singleton archetypes"
                f"{suffix}")
    scope = plan.get("set_scope") or {}
    if backend == "powerset" and "set_scope" in plan and not (scope.get("set_id") or "").strip():
        issues.append("set_scope.set_id is empty — approval will hard-fail after Review; set "
                      "POWERPACKS_DEFAULT_SET_ID or pass --set-id before approving")
    return issues


def main() -> None:
    ap = argparse.ArgumentParser(description="Critique a generated deep-search plan against its JD (one cheap LLM call).")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--jd-file", required=True)
    ap.add_argument("--out", default=None, help="Default: plan_critic.json next to the plan")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--backend", choices=("powerset", "local"), default=None,
                    help="Execution backend; enables backend-specific deterministic checks")
    ap.add_argument("--no-llm", action="store_true", help="Deterministic checks only (offline/tests)")
    args = ap.parse_args()

    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    jd = Path(args.jd_file).read_text(encoding="utf-8")

    result: dict[str, Any] = {"missing_core_pillars": [], "cutoff_issues": [], "other_issues": []}
    result["deterministic_issues"] = deterministic_checks(plan, backend=args.backend)

    if not args.no_llm:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            print(json.dumps({"primitive": "plan_critic", "status": "failed", "error": "OPENAI_API_KEY not set"}))
            raise SystemExit(1)
        client = make_openai_client(key)
        request: dict[str, Any] = dict(
            model=args.model,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": f"JOB DESCRIPTION:\n{jd.strip()}\n\nPLAN:\n{json.dumps(plan, indent=1)}"}],
            response_format={"type": "json_object"},
        )
        if supports_custom_temperature(args.model):
            request["temperature"] = 0.0
        resp = client.chat.completions.create(**request)
        obj = json.loads(resp.choices[0].message.content or "{}")
        if not isinstance(obj, dict):
            obj = {}
        for k in ("missing_core_pillars", "cutoff_issues", "other_issues"):
            v = obj.get(k)
            if isinstance(v, list):
                result[k] = [str(x) for x in v][:8]

    n_issues = sum(len(result[k]) for k in ("missing_core_pillars", "cutoff_issues", "other_issues", "deterministic_issues"))
    result["verdict"] = "needs_edits" if n_issues else "ok"
    out_path = Path(args.out) if args.out else plan_path.parent / "plan_critic.json"
    out_path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "plan_critic", "status": "completed", "verdict": result["verdict"],
                      "issues": n_issues, "out": str(out_path)}, indent=2))


if __name__ == "__main__":
    main()
