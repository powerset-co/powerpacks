"""Bridge a recruit shotgun run into the inputs the canonical judge expects.

`evaluate_profile_candidates` reads a *profile-search* run dir:
  - plan.json            (job_title, normalized_archetype, hire_stage, usable_cutoff, traits)
  - candidate_frontier.jsonl  (one {person_id, source_rows:[{score}], matched_probe_ids} per candidate)
  - probe_summaries.json (list of {artifact_dir} -> <dir>/hydrate_people/profiles.jsonl.gz)

The recruit pipeline instead emits `union.jsonl` (deduped candidates + found_by) plus
`probes/<key>/ledger.json` (each pointing at a search-network artifact dir that ALREADY holds
the hydrated profiles.jsonl.gz). This adapter rewrites the recruit run into the judge's contract
WITHOUT recomputing anything expensive:

  - probe_summaries.json  <- artifact_dir from every probe ledger (profiles already on disk)
  - candidate_frontier.jsonl <- union rows; source score = #probes that found them (multi-probe
    signal), matched_probe_ids = found_by. The judge re-ranks by its own rubric afterwards, so
    this only seeds selection order.
  - plan.json  <- ONE LLM call extracts must/nice traits + hire_stage + usable_cutoff from the JD
    (mirrors the hand-authored plan.json in search-profile Task 1, made callable & portable).

One OpenAI call total (traits). See packs/search/skills/recruit/SKILL.md.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = os.environ.get("RECRUIT_PLAN_MODEL", "gpt-4o")
DEFAULT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")

PLAN_SYSTEM = (
    "You are a technical recruiter turning a job description into a structured evaluation plan "
    "for an automated candidate judge. Extract ONLY what the JD supports. Hard rules:\n"
    "- must_have traits: the differentiating, non-negotiable capabilities the JD demands "
    "(what the person must have built/owned). nice_to_have: real pluses the JD mentions.\n"
    "- Each trait is a short evidence-checkable phrase, NOT a sentence and NOT a job title.\n"
    "- hire_stage: one of founding_early | early | growth | scale — infer from company stage/role.\n"
    "- usable_cutoff: one sentence on the seniority band that is in-band vs out (the judge "
    "hard-gates too-senior execs/founders/advisors and too-junior ICs).\n"
    "- normalized_archetype: a 2-4 word canonical role archetype (e.g. 'distributed systems engineer').\n"
    'Return strict JSON: {"job_title","normalized_archetype","hire_stage","usable_cutoff",'
    '"must_have":["..."],"nice_to_have":["..."]}.'
)


def build_plan_messages(jd: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": f"Job description:\n\n{jd.strip()}"},
    ]


def plan_from_obj(obj: dict[str, Any], *, set_name: str, set_id: str, source_url: str | None, created_at: str) -> dict[str, Any]:
    """Normalize the model's JSON into a plan.json the judge can read.

    Only the fields the judge consumes are required to be meaningful; the rest are filled with
    sane, schema-shaped defaults so the artifact is self-describing.
    """
    must = [{"trait": str(t).strip()} for t in (obj.get("must_have") or []) if str(t).strip()]
    nice = [{"trait": str(t).strip()} for t in (obj.get("nice_to_have") or []) if str(t).strip()]
    if not must:
        raise ValueError("plan extraction produced no must_have traits")
    return {
        "route": "recruit",
        "parse_only": False,
        "retrieval_ran": False,
        "job_id": "recruit",
        "job_title": str(obj.get("job_title") or "role").strip(),
        "normalized_archetype": str(obj.get("normalized_archetype") or "engineer").strip(),
        "source_url": source_url,
        "source_title": None,
        "set_scope": {"name": set_name, "set_id": set_id},
        "hire_stage": str(obj.get("hire_stage") or "early").strip(),
        "usable_cutoff": str(obj.get("usable_cutoff") or "Senior in-band IC; executives, founders, and advisors are out.").strip(),
        "traits": {"must_have": must, "nice_to_have": nice},
        "created_at": created_at,
    }


def build_frontier(union: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """union row -> frontier candidate. score = #probes (multi-probe = stronger seed signal)."""
    out: list[dict[str, Any]] = []
    for r in union:
        pid = r.get("person_id")
        if not pid:
            continue
        found = r.get("found_by") or []
        out.append({
            "person_id": pid,
            "candidate_id": pid,
            "name": r.get("name"),
            "linkedin_url": r.get("linkedin_url"),
            "current_title": r.get("current_title"),
            "current_company": r.get("current_company"),
            "location": r.get("location"),
            "matched_probe_ids": list(found),
            "source_rows": [{"probe": k, "score": float(len(found))} for k in (found or ["_"])],
        })
    return out


def probe_artifact_dirs(run_dir: Path) -> list[str]:
    """Every probe ledger's artifact_dir (each holds hydrate_people/profiles.jsonl.gz)."""
    dirs: list[str] = []
    seen: set[str] = set()
    for led in sorted(run_dir.glob("probes/*/ledger.json")):
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


def _load_union(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build plan.json + candidate_frontier.jsonl + probe_summaries.json for the canonical judge.")
    ap.add_argument("--run-dir", required=True, help="Recruit run dir with union.jsonl + probes/<key>/ledger.json")
    ap.add_argument("--union", default=None, help="Override union path (default <run-dir>/union.jsonl)")
    ap.add_argument("--jd-file", required=True, help="Path to the JD text (for trait extraction)")
    ap.add_argument("--set-id", default=os.environ.get("POWERPACKS_DEFAULT_SET_ID", ""))
    ap.add_argument("--set-name", default="recruit set")
    ap.add_argument("--source-url", default=None)
    ap.add_argument("--created-at", required=True, help="ISO timestamp (passed in; primitives stay deterministic)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    union = _load_union(Path(args.union) if args.union else run_dir / "union.jsonl")
    frontier = build_frontier(union)
    if not frontier:
        print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": "empty union"}))
        raise SystemExit(1)

    artifact_dirs = probe_artifact_dirs(run_dir)
    covered = verify_profile_coverage(frontier, artifact_dirs)

    key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        print(json.dumps({"primitive": "build_eval_inputs", "status": "failed", "error": "OPENAI_API_KEY not set"}))
        raise SystemExit(1)

    import openai  # imported here so --help works without the dep

    client = openai.OpenAI(api_key=key, base_url=DEFAULT_API_BASE)
    jd = Path(args.jd_file).read_text(encoding="utf-8")
    resp = client.chat.completions.create(
        model=args.model,
        messages=build_plan_messages(jd),
        response_format={"type": "json_object"},
    )
    plan = plan_from_obj(
        json.loads(resp.choices[0].message.content or "{}"),
        set_name=args.set_name, set_id=args.set_id, source_url=args.source_url, created_at=args.created_at,
    )

    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    with (run_dir / "candidate_frontier.jsonl").open("w", encoding="utf-8") as fh:
        for c in frontier:
            fh.write(json.dumps(c) + "\n")
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
