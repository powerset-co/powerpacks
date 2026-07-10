"""Bridge a deep-search wide-search run into the inputs the canonical judge expects.

`evaluate_profile_candidates` reads a *profile-search* run dir:
  - plan.json            (job_title, normalized_archetype, hire_stage, usable_cutoff, traits)
  - candidate_frontier.jsonl  (one {person_id, source_rows:[{score}], matched_probe_ids} per candidate)
  - probe_summaries.json (list of {artifact_dir} -> <dir>/hydrate_people/profiles.jsonl.gz)

The deep-search pipeline instead emits `union.jsonl` (deduped candidates + found_by) plus
`probes/<key>/ledger.json` (each pointing at a search-network artifact dir that ALREADY holds
the hydrated profiles.jsonl.gz). This adapter rewrites the deep-search run into the judge's contract
WITHOUT recomputing anything expensive:

  - probe_summaries.json  <- artifact_dir from every probe ledger (profiles already on disk)
  - candidate_frontier.jsonl <- union rows; source score = #probes that found them (multi-probe
    signal), matched_probe_ids = found_by. The judge re-ranks by its own rubric afterwards, so
    this only seeds selection order.
  - plan.json  <- ONE LLM call extracts must/nice traits + core groups + hire_stage + usable_cutoff
    from the JD (mirrors the hand-authored plan.json step, made callable & portable). `--plan-only`
    writes this contract before sourcing so the human Review can shape epoch-0 probes.

One OpenAI call total (traits). See packs/search/skills/search/SKILL.md.
"""
from __future__ import annotations

import argparse
import gzip
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
    import recruiter_policy as recruiter_policy
except ImportError:  # module execution
    from . import recruiter_policy

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = os.environ.get("RECRUIT_PLAN_MODEL", "gpt-4o")

PLAN_SYSTEM = (
    "You are a technical recruiter turning a job description into a structured evaluation plan "
    "for an automated candidate judge. Extract ONLY what the JD supports. Hard rules:\n"
    "- must_have traits: the non-negotiable capabilities the JD demands. Tag EACH must_have with a "
    "`tier`:\n"
    "    * 'core' = a domain-defining differentiator that makes THIS role hard — the specific "
    "capability or domain a generically strong, senior person would NOT automatically have (e.g. "
    "'delivered large fusion/plasma hardware programs', 'built distributed schedulers at scale', "
    "'shipped LLM inference systems in production'). These are the GATES: someone who lacks a core "
    "trait is not a real fit no matter how senior or impressive. Make core traits as SHARP and "
    "domain-specific as the JD allows — prefer the narrowest true requirement over a broad one.\n"
    "    * 'table_stakes' = generic competence most qualified seniors in this band already have "
    "(leadership, communication, strategic thinking, people/eng management, relocation/logistics). "
    "Real requirements, but NOT what separates a fit from a non-fit.\n"
    "  Core is about WHAT DOMAIN/CAPABILITY the person has built — NOT how senior, how long, or "
    "where. Stage/tenure/experience-amount traits ('early-stage startup experience', '10+ years', "
    "'worked at a big company') are table_stakes, never core. Most roles have only 1-3 core traits. "
    "NEVER mark generic leadership/communication/management/relocation/stage as core. "
    "nice_to_have: real pluses the JD mentions.\n"
    "- Each trait is a short evidence-checkable phrase, NOT a sentence and NOT a job title.\n"
    "- core_groups: encode the actual gate. Each group is an alternative viable archetype; ALL core "
    "traits named inside one group are required, while satisfying any one whole group is sufficient. "
    "Most roles have one group containing all core traits. Use multiple groups only when the JD truly "
    "admits alternative backgrounds. Reference core trait text exactly.\n"
    "- hire_stage: one of founding_early | scaling_late. Use founding_early for 0-to-1/ambiguous/early "
    "startup work and scaling_late for hardening, scale, mature systems, or later-stage organizations.\n"
    "- target_level: the role's career level — one of senior_ic | staff_ic | lead | manager | "
    "director | vp | exec. Infer from the title/responsibilities (an IC eng role is senior_ic/"
    "staff_ic; a 'VP of Engineering' is vp; a 'Head of X' is director/vp).\n"
    "- usable_cutoff: ONE sentence stating the target level and seniority/track policy. For IC roles, "
    "higher hands-on IC levels (staff/principal/distinguished/lead-IC) remain in-band; current "
    "management/exec/company-running identities are too_senior unless the role asks for that track. "
    "For management/exec roles, in-band is the target and one level below; one+ above is too_senior, "
    "two+ below is too_junior. Name concrete in-band and gated titles for THIS role.\n"
    "- location: the JD's candidate-sourcing metro, normalized for search; empty for remote/flexible/"
    "unstated. Location scopes probes but is not a candidate-quality trait.\n"
    "- normalized_archetype: a 2-4 word canonical role archetype (e.g. 'distributed systems engineer').\n"
    "- recruiter_preferences: OPTIONAL and only for recruiter-ranking preferences the JD states "
    "explicitly. Allowed fields are excellence_weights, pedigree_policy, and "
    "current_founder_c_suite_for_non_exec_ic. Never infer brand/pedigree preference or weights from "
    "company identity; omit the object when the JD is silent.\n"
    'Return strict JSON: {"job_title","normalized_archetype","hire_stage","target_level","usable_cutoff",'
    '"location":"","must_have":[{"trait":"...","tier":"core|table_stakes"}],'
    '"core_groups":[{"name":"<archetype>","all_of":["<exact core trait>"]}],'
    '"nice_to_have":["..."],"recruiter_preferences":{...}}.'
)

VALID_TARGET_LEVELS = {"senior_ic", "staff_ic", "lead", "manager", "director", "vp", "exec"}
VALID_TIERS = {"core", "table_stakes"}


def build_plan_messages(jd: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": f"Job description:\n\n{jd.strip()}"},
    ]


def _must_trait(t: Any) -> dict[str, str] | None:
    """Normalize one must_have entry into {trait, tier}. Accepts the tagged object form
    ({"trait","tier"}) and the legacy bare-string form. An unrecognized/absent tier degrades to
    'table_stakes' so a mis-tagged plan falls back to the score gate rather than over-gating
    (the core-gate only fires on traits the model EXPLICITLY marked 'core')."""
    if isinstance(t, dict):
        text = str(t.get("trait") or "").strip()
        tier = str(t.get("tier") or "").strip().lower()
        tier = tier if tier in VALID_TIERS else "table_stakes"
    else:
        text, tier = str(t).strip(), "table_stakes"
    return {"trait": text, "tier": tier, "source": "jd"} if text else None


def _core_groups(obj: dict[str, Any], must: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Normalize alternative all-of gates, falling back to one group containing every core trait."""
    core_by_norm = {_norm(t["trait"]): t["trait"] for t in must if t["tier"] == "core"}
    groups: list[dict[str, Any]] = []
    for i, raw in enumerate(obj.get("core_groups") or []):
        if not isinstance(raw, dict):
            continue
        traits: list[str] = []
        for value in raw.get("all_of") or []:
            canonical = core_by_norm.get(_norm(str(value)))
            if canonical and canonical not in traits:
                traits.append(canonical)
        if traits:
            groups.append({
                "name": str(raw.get("name") or f"archetype_{i + 1}").strip(),
                "all_of": traits,
                "source": "jd",
            })
    if groups:
        return groups
    core = [t["trait"] for t in must if t["tier"] == "core"]
    return [{"name": "default", "all_of": core, "source": "jd"}] if core else []


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def plan_from_obj(
    obj: dict[str, Any],
    *,
    set_name: str,
    set_id: str,
    source_url: str | None,
    created_at: str,
    user_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize the model's JSON into a plan.json the judge can read.

    Only the fields the judge consumes are required to be meaningful; the rest are filled with
    sane, schema-shaped defaults so the artifact is self-describing.
    """
    must = [o for o in (_must_trait(t) for t in (obj.get("must_have") or [])) if o]
    nice = [{"trait": str(t).strip(), "source": "jd"} for t in (obj.get("nice_to_have") or []) if str(t).strip()]
    if not must:
        raise ValueError("plan extraction produced no must_have traits")
    target_level = str(obj.get("target_level") or "senior_ic").strip().lower()
    if target_level not in VALID_TARGET_LEVELS:
        target_level = "senior_ic"
    try:
        hire_stage = recruiter_policy.canonicalize_hire_stage(
            str(obj.get("hire_stage") or "founding_early")
        )
    except recruiter_policy.RecruiterPolicyError:
        hire_stage = "founding_early"
    jd_preferences = dict(obj.get("recruiter_preferences") or {})
    jd_preferences["hire_stage"] = hire_stage
    resolved_policy = recruiter_policy.resolve_recruiter_preferences(
        user_preferences=user_preferences,
        jd_preferences=jd_preferences,
    )
    return {
        "route": "deep",
        "parse_only": False,
        "retrieval_ran": False,
        "job_id": "deep",
        "job_title": str(obj.get("job_title") or "role").strip(),
        "normalized_archetype": str(obj.get("normalized_archetype") or "engineer").strip(),
        "source_url": source_url,
        "source_title": None,
        "set_scope": {"name": set_name, "set_id": set_id},
        "search_scope": {
            "location": str(obj.get("location") or "").strip() or None,
            "source": "jd",
        },
        "hire_stage": resolved_policy["preferences"]["hire_stage"],
        "target_level": target_level,
        "usable_cutoff": str(obj.get("usable_cutoff") or "Senior in-band IC; executives, founders, and advisors are out.").strip(),
        "traits": {"must_have": must, "nice_to_have": nice},
        "core_groups": _core_groups(obj, must),
        "recruiter_policy": resolved_policy,
        "created_at": created_at,
    }


def extract_plan(
    *,
    jd_file: Path,
    set_name: str,
    set_id: str,
    source_url: str | None,
    created_at: str,
    model: str,
    api_key: str | None,
    user_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY not set")
    client = make_openai_client(key)
    jd = jd_file.read_text(encoding="utf-8")
    resp = client.chat.completions.create(
        model=model,
        messages=build_plan_messages(jd),
        response_format={"type": "json_object"},
    )
    return plan_from_obj(
        json.loads(resp.choices[0].message.content or "{}"),
        set_name=set_name,
        set_id=set_id,
        source_url=source_url,
        created_at=created_at,
        user_preferences=user_preferences,
    )


def load_user_preferences(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return recruiter_policy.validate_recruiter_preferences(document, source="user_preferences")


def build_frontier(union: list[dict[str, Any]], source_map: dict[str, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """union row -> frontier candidate. score = #probes (multi-probe = stronger seed signal).

    source_map: person_id -> (source_operator, source_channel) = the REAL import provenance — the
    operator whose network the person came through and the platform they arrived on (gmail /
    linkedin / imessage / whatsapp / ...), from the hydrated profiles. This is what the sendable
    shortlist's Source/Channel columns mean — NOT the sourcing method. Falls back to "" (unknown)
    when a candidate has no provenance on file."""
    source_map = source_map or {}
    out: list[dict[str, Any]] = []
    for r in union:
        pid = r.get("person_id")
        if not pid:
            continue
        found = r.get("found_by") or []
        matched_probe_ids = list(found)
        source_rows = [
            {"probe_id": k, "probe": k, "score": float(len(found))}
            for k in (found or ["_"])
        ]
        op, ch = source_map.get(pid, ("", ""))
        out.append({
            "person_id": pid,
            "candidate_id": pid,
            "public_identifier": None,
            "name": r.get("name"),
            "linkedin_url": r.get("linkedin_url"),
            "current_title": r.get("current_title"),
            "current_role": r.get("current_title"),
            "current_company": r.get("current_company"),
            "location": r.get("location"),
            "source_operator": op,
            "source_channel": ch,
            "matched_probe_ids": matched_probe_ids,
            "source_rows": source_rows,
            "duplicate_signal": {
                "matched_probe_count": len(matched_probe_ids),
                "matched_probe_ids": matched_probe_ids,
                "interpretation": "matched multiple deep-search probes" if len(matched_probe_ids) > 1 else "single deep-search probe match",
            },
        })
    return out


def write_frontier_artifacts(run_dir: Path, frontier: list[dict[str, Any]]) -> None:
    """Write the streaming and canonical candidate frontier artifacts from the same full list."""
    with (run_dir / "candidate_frontier.jsonl").open("w", encoding="utf-8") as fh:
        for c in frontier:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    (run_dir / "candidate_frontier.json").write_text(
        json.dumps({
            "candidates": frontier,
            "candidate_count": len(frontier),
            "source": "deep_search/build_eval_inputs",
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def probe_artifact_dirs(run_dir: Path) -> list[str]:
    """Every probe ledger's artifact_dir (each holds hydrate_people/profiles.jsonl.gz)."""
    dirs: list[str] = []
    seen: set[str] = set()
    # Match both run_wide_search (run_dir/probes/<k>) and robust_source (run_dir/round*/probes/<k>).
    ledgers = sorted(run_dir.glob("probes/*/ledger.json")) + sorted(run_dir.glob("round*/probes/*/ledger.json"))
    for led in ledgers:
        try:
            arts = json.loads(led.read_text()).get("artifacts") or {}
        except (json.JSONDecodeError, OSError):
            continue
        d = arts.get("artifact_dir")
        if d and d not in seen:
            seen.add(d)
            dirs.append(d)
    return dirs


def verify_profile_coverage(frontier: list[dict[str, Any]], artifact_dirs: list[str]) -> int:
    """How many frontier person_ids have a hydrated profile in the artifact dirs (sanity)."""
    wanted = {c["person_id"] for c in frontier}
    found: set[str] = set()
    for d in artifact_dirs:
        p = Path(d)
        gz = (p if p.is_absolute() else ROOT / p) / "hydrate_people" / "profiles.jsonl.gz"
        if not gz.exists():
            continue
        try:
            with gzip.open(gz, "rt") as fh:
                for line in fh:
                    try:
                        pid = json.loads(line).get("person_id")
                    except json.JSONDecodeError:
                        continue
                    if pid in wanted:
                        found.add(pid)
        except OSError:
            continue
    return len(found)


def profile_source_map(artifact_dirs: list[str]) -> dict[str, tuple[str, str]]:
    """person_id -> (source_operator, source_channel) from the hydrated profiles: the operator
    whose network the person came through and the platform they arrived on (gmail / linkedin /
    imessage / whatsapp / ...). First profile wins; a candidate with no provenance maps to nothing
    (build_frontier then fills ("", ""))."""
    out: dict[str, tuple[str, str]] = {}
    for d in artifact_dirs:
        p = Path(d)
        gz = (p if p.is_absolute() else ROOT / p) / "hydrate_people" / "profiles.jsonl.gz"
        if not gz.exists():
            continue
        try:
            with gzip.open(gz, "rt") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pid = r.get("person_id")
                    if pid and pid not in out:
                        op = r.get("primary_source_operator") or next(iter(r.get("source_operators") or []), "") or ""
                        ch = r.get("primary_source_channel") or next(iter(r.get("source_channels") or []), "") or ""
                        out[pid] = (op, ch)
        except OSError:
            continue
    return out


def _load_union(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build plan.json + candidate_frontier.jsonl + probe_summaries.json for the canonical judge.")
    ap.add_argument("--run-dir", required=True, help="Deep-search run dir; full mode expects union.jsonl + probe ledgers")
    ap.add_argument("--union", default=None, help="Override union path (default <run-dir>/union.jsonl)")
    ap.add_argument("--jd-file", default=None, help="Path to the JD text (for trait extraction; not needed with --plan)")
    ap.add_argument("--plan", default=None, help="Reuse an existing plan.json (skip the LLM trait extraction — for loop epochs)")
    ap.add_argument("--plan-only", action="store_true", help="Extract and write plan.json before sourcing; do not require a union/frontier")
    ap.add_argument("--set-id", default=os.environ.get("POWERPACKS_DEFAULT_SET_ID", ""))
    ap.add_argument("--set-name", default="deep-search set")
    ap.add_argument("--source-url", default=None)
    ap.add_argument("--created-at", default=None, help="ISO timestamp (required unless --plan has created_at)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument(
        "--preferences",
        default=None,
        help="Optional recruiter-preferences JSON; explicit user values override JD inference and defaults",
    )
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.plan_only:
        if args.plan:
            ap.error("--plan-only cannot be combined with --plan")
        if not args.jd_file or not args.created_at:
            ap.error("--plan-only requires --jd-file and --created-at")
        try:
            plan = extract_plan(
                jd_file=Path(args.jd_file),
                set_name=args.set_name,
                set_id=args.set_id,
                source_url=args.source_url,
                created_at=args.created_at,
                model=args.model,
                api_key=args.api_key,
                user_preferences=load_user_preferences(args.preferences),
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": str(exc)}))
            raise SystemExit(1) from exc
        (run_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "primitive": "build_eval_inputs",
            "status": "awaiting_plan_approval",
            "plan": str(run_dir / "plan.json"),
            "must_have": len(plan["traits"]["must_have"]),
            "nice_to_have": len(plan["traits"]["nice_to_have"]),
            "core_groups": len(plan["core_groups"]),
        }, indent=2))
        return

    union = _load_union(Path(args.union) if args.union else run_dir / "union.jsonl")
    artifact_dirs = probe_artifact_dirs(run_dir)
    frontier = build_frontier(union, profile_source_map(artifact_dirs))
    if not frontier:
        print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": "empty union"}))
        raise SystemExit(1)

    covered = verify_profile_coverage(frontier, artifact_dirs)

    if args.plan:  # reuse an existing plan (loop epochs) — no LLM call
        plan = json.loads(Path(args.plan).read_text())
        if not plan.get("created_at"):
            if not args.created_at:
                print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": "approved plan missing created_at; pass --created-at to fill it"}))
                raise SystemExit(1)
            plan["created_at"] = args.created_at
    else:
        if not args.created_at:
            print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": "need --created-at unless --plan includes created_at"}))
            raise SystemExit(1)
        if not args.jd_file:
            print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": "need --jd-file or --plan"}))
            raise SystemExit(1)
        try:
            plan = extract_plan(
                jd_file=Path(args.jd_file), set_name=args.set_name, set_id=args.set_id,
                source_url=args.source_url, created_at=args.created_at,
                model=args.model, api_key=args.api_key,
                user_preferences=load_user_preferences(args.preferences),
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": str(exc)}))
            raise SystemExit(1) from exc

    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    write_frontier_artifacts(run_dir, frontier)
    (run_dir / "probe_summaries.json").write_text(
        json.dumps([{"artifact_dir": d} for d in artifact_dirs], indent=2), encoding="utf-8")

    print(json.dumps({
        "primitive": "build_eval_inputs", "status": "completed",
        "frontier": len(frontier), "profile_coverage": covered,
        "probe_dirs": len(artifact_dirs), "must_have": len(plan["traits"]["must_have"]),
        "nice_to_have": len(plan["traits"]["nice_to_have"]), "run_dir": str(run_dir),
    }, indent=2))


if __name__ == "__main__":
    main()
