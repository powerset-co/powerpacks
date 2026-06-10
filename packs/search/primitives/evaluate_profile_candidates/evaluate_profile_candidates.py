#!/usr/bin/env python3
"""Automated profile-search candidate evaluator (Task 5a primitive).

Reads candidate_frontier.json(l) + plan.json from a profile-search run
directory, loads hydrated profiles from profile-search artifacts, and runs an
async LLM evaluation per candidate with seniority enforced as a hard gate that
is independent of skill-trait scores. Writes candidate_evaluations.raw.jsonl
in the Task 5a schema so capture_jd_evaluations / export_candidate_shortlist
work unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = os.environ.get("PROFILE_EVAL_MODEL", os.environ.get("JD_EVAL_MODEL", "gpt-5.1"))
DEFAULT_REASONING_EFFORT = os.environ.get("PROFILE_EVAL_REASONING_EFFORT", os.environ.get("JD_EVAL_REASONING_EFFORT", "low"))
DEFAULT_CONCURRENCY = 100
DEFAULT_MAX_CANDIDATES = 200

VALID_VERDICTS = {"strong", "maybe", "weak", "out"}
VALID_SENIORITY = {"ideal", "acceptable", "too_senior", "too_junior", "wrong_track", "unknown"}
VALID_TRAIT_STATUS = {"strong", "partial", "weak", "missing", "unknown"}
GATED_SENIORITY = {"too_senior", "too_junior", "wrong_track"}

SYSTEM_PROMPT = """You are a senior technical recruiter producing the final evaluation for a hiring-manager shortlist.

You will receive: the job context, the seniority band / usable cutoff policy, must-have traits, nice-to-have traits, and one candidate profile.

=== SENIORITY IS A HARD GATE, SEPARATE FROM SKILLS ===

First decide seniority_fit from the candidate's CURRENT career level:
- "ideal": current role is squarely in the target band
- "acceptable": adjacent band but plausibly analogous after company-size context
- "too_senior": current level is executive/founder/advisory or clearly above the band. CEOs, CTOs, CFOs, COOs, presidents, founders/co-founders (current), VPs, heads-of, managing directors, general partners, board members, angel investors, and advisors are too_senior for an IC search unless the policy explicitly includes them. A person whose CURRENT title mixes IC work with founder/advisor roles is too_senior when the founder/exec role is their primary current identity.
- "too_junior": clearly below the band (interns, new grads, analysts for a senior role)
- "wrong_track": different career lane (e.g. data scientist without pipeline ownership for a data engineering role, pure people-manager without hands-on evidence, consultant/agency when the role is in-house IC)
- "unknown": genuinely cannot tell

A candidate with deep matching skills but out-of-band seniority is OUT. Do not rescue a CTO because they once built ETL pipelines. Past founder roles are fine if the CURRENT role is an in-band IC role at a different company.

=== VERDICT RULES ===

- seniority_fit in (too_senior, too_junior, wrong_track) => verdict MUST be "out", regardless of trait scores.
- Otherwise: "strong" = in-band and strong evidence on most must-have traits; "maybe" = in-band with partial/uncertain evidence; "weak" = thin evidence, keep only for debug pools; "out" = does not fit.
- Only profile evidence counts. Do not invent facts. Missing evidence is "missing" or "unknown", not "partial".

=== OUTPUT ===

Return ONLY a JSON object:
{
  "jd_score": 0.0-1.0,
  "verdict": "strong|maybe|weak|out",
  "seniority_fit": "ideal|acceptable|too_senior|too_junior|wrong_track|unknown",
  "must_have": [{"trait": "<exact trait text>", "status": "strong|partial|weak|missing|unknown", "evidence": "<short cite from profile>"}],
  "nice_to_have": [{"trait": "<exact trait text>", "status": "...", "evidence": "..."}],
  "rationale": "<one short paragraph>",
  "caveats": ["<short strings>"]
}

Include every provided trait exactly once with its exact text. jd_score reflects overall fit; gated candidates should score <= 0.3.
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_frontier(run_dir: Path) -> list[dict[str, Any]]:
    jsonl = run_dir / "candidate_frontier.jsonl"
    if jsonl.exists():
        return [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    doc = read_json(run_dir / "candidate_frontier.json")
    return doc.get("candidates", []) if isinstance(doc, dict) else doc


def best_source_score(candidate: dict[str, Any]) -> float:
    best = 0.0
    for row in candidate.get("source_rows", []) or []:
        try:
            best = max(best, float(row.get("score") or 0))
        except (TypeError, ValueError):
            continue
    return best


def collect_profiles(candidates: list[dict[str, Any]], run_dir: Path) -> dict[str, dict[str, Any]]:
    """Load hydrated profiles for the wanted candidates from probe artifacts."""
    wanted = {c.get("person_id") or c.get("candidate_id") for c in candidates}
    profile_dirs: list[Path] = []
    summaries = run_dir / "probe_summaries.json"
    if summaries.exists():
        for probe in read_json(summaries):
            artifact_dir = probe.get("artifact_dir")
            if artifact_dir:
                p = Path(artifact_dir)
                profile_dirs.append(p if p.is_absolute() else ROOT / p)
    # Also honor profile_context_ref run ids
    for c in candidates:
        ref = c.get("profile_context_ref")
        if ref:
            profile_dirs.append(ROOT / ".powerpacks/runs/artifacts" / ref)
    out: dict[str, dict[str, Any]] = {}
    seen_dirs = set()
    for d in profile_dirs:
        key = str(d)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        gz = d / "hydrate_people" / "profiles.jsonl.gz"
        if not gz.exists():
            continue
        try:
            with gzip.open(gz, "rt") as handle:
                for line in handle:
                    obj = json.loads(line)
                    pid = obj.get("person_id") or obj.get("id")
                    if pid and pid in wanted and pid not in out:
                        out[pid] = obj
        except OSError:
            continue
        if len(out) == len(wanted):
            break
    return out


def build_user_prompt(plan: dict[str, Any], profile: dict[str, Any]) -> str:
    traits = plan.get("traits", {}) or {}
    must = [t.get("trait") for t in traits.get("must_have", []) if t.get("trait")]
    nice = [t.get("trait") for t in traits.get("nice_to_have", []) if t.get("trait")]
    parts = [
        f"Job: {plan.get('job_title') or ''} ({plan.get('normalized_archetype') or ''})",
        f"Seniority / usable cutoff policy: {plan.get('usable_cutoff') or 'Senior in-band IC; executives, founders, and advisors are out.'}",
        "Must-have traits:",
        *[f"- {t}" for t in must],
        "Nice-to-have traits:",
        *[f"- {t}" for t in nice],
        "",
        "Candidate profile (JSON):",
        json.dumps(profile, sort_keys=True),
        "",
        "Return the JSON evaluation object only.",
    ]
    return "\n".join(parts)


def supports_reasoning_effort(model: str) -> bool:
    normalized = str(model or "").lower().split("/")[-1]
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def normalize_evaluation(parsed: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    traits = plan.get("traits", {}) or {}
    must = [t.get("trait") for t in traits.get("must_have", []) if t.get("trait")]
    nice = [t.get("trait") for t in traits.get("nice_to_have", []) if t.get("trait")]

    def norm_traits(items: Any, expected: list[str]) -> list[dict[str, Any]]:
        by_trait = {}
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("trait"):
                    status = str(it.get("status", "unknown")).lower()
                    by_trait[it["trait"]] = {
                        "trait": it["trait"],
                        "status": status if status in VALID_TRAIT_STATUS else "unknown",
                        "evidence": str(it.get("evidence", ""))[:400],
                    }
        return [by_trait.get(t, {"trait": t, "status": "unknown", "evidence": ""}) for t in expected]

    try:
        jd_score = max(0.0, min(1.0, float(parsed.get("jd_score", 0))))
    except (TypeError, ValueError):
        jd_score = 0.0
    verdict = str(parsed.get("verdict", "")).lower()
    if verdict not in VALID_VERDICTS:
        verdict = "out"
    seniority_fit = str(parsed.get("seniority_fit", "")).lower()
    if seniority_fit not in VALID_SENIORITY:
        seniority_fit = "unknown"
    # Hard gate enforced in code, not just the prompt.
    if seniority_fit in GATED_SENIORITY:
        verdict = "out"
        jd_score = min(jd_score, 0.3)
    caveats = parsed.get("caveats") or []
    if not isinstance(caveats, list):
        caveats = [str(caveats)]
    return {
        "jd_score": round(jd_score, 2),
        "verdict": verdict,
        "seniority_fit": seniority_fit,
        "must_have": norm_traits(parsed.get("must_have"), must),
        "nice_to_have": norm_traits(parsed.get("nice_to_have"), nice),
        "rationale": str(parsed.get("rationale", ""))[:1200],
        "caveats": [str(c)[:200] for c in caveats][:8],
    }


async def evaluate_one(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    model: str,
    reasoning_effort: str | None,
    plan: dict[str, Any],
    candidate: dict[str, Any],
    profile: dict[str, Any] | None,
    timeout: int,
    max_retries: int,
) -> dict[str, Any]:
    pid = candidate.get("person_id") or candidate.get("candidate_id")
    base = {
        "candidate_id": pid,
        "duplicate_signal": {
            "matched_probe_count": (candidate.get("duplicate_signal") or {}).get("matched_probe_count")
            or len(candidate.get("matched_probe_ids", []) or []),
            "matched_probe_ids": candidate.get("matched_probe_ids", []) or [],
            "interpretation": f"Appeared in {len(candidate.get('matched_probe_ids', []) or [])} profile searches",
        },
    }
    if profile is None:
        return {
            **base,
            "jd_score": 0.0,
            "verdict": "out",
            "seniority_fit": "unknown",
            "must_have": [],
            "nice_to_have": [],
            "rationale": "No hydrated profile available for evaluation.",
            "caveats": ["missing_hydrated_profile"],
            "error": "missing_profile",
        }
    user_prompt = build_user_prompt(plan, profile)
    last_error = ""
    async with semaphore:
        for attempt in range(max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "timeout": timeout,
                }
                if reasoning_effort and supports_reasoning_effort(model):
                    kwargs["reasoning_effort"] = reasoning_effort
                response = await client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content or "{}"
                parsed = json.loads(content)
                return {**base, **normalize_evaluation(parsed, plan)}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 8))
    return {
        **base,
        "jd_score": 0.0,
        "verdict": "out",
        "seniority_fit": "unknown",
        "must_have": [],
        "nice_to_have": [],
        "rationale": f"Evaluation failed: {last_error[:200]}",
        "caveats": ["evaluation_error"],
        "error": last_error[:400],
    }


async def evaluate_all(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    plan = read_json(run_dir / "plan.json")
    frontier = load_frontier(run_dir)
    if not frontier:
        raise SystemExit("empty candidate frontier")

    # Select candidates: sort by best per-probe score, then multi-probe count.
    frontier.sort(
        key=lambda c: (
            -best_source_score(c),
            -len(c.get("matched_probe_ids", []) or []),
        )
    )
    selected = frontier[: args.max_candidates] if args.max_candidates else frontier

    profiles = collect_profiles(selected, run_dir)
    print(
        f"evaluate: candidates={len(selected)} of {len(frontier)} profiles_found={len(profiles)} "
        f"model={args.model} concurrency={args.concurrency}",
        file=sys.stderr,
    )

    client = AsyncOpenAI(api_key=args.api_key, base_url=args.api_base)
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.monotonic()
    tasks = [
        evaluate_one(
            client,
            semaphore,
            args.model,
            args.reasoning_effort,
            plan,
            candidate,
            profiles.get(candidate.get("person_id") or candidate.get("candidate_id")),
            args.timeout,
            args.max_retries,
        )
        for candidate in selected
    ]
    results = await asyncio.gather(*tasks)
    elapsed = round(time.monotonic() - started, 2)

    # Rank by jd_score desc; gated/out candidates sink naturally.
    ordered = sorted(results, key=lambda r: -r.get("jd_score", 0))
    for i, r in enumerate(ordered):
        r["rank"] = i + 1

    raw_path = run_dir / "candidate_evaluations.raw.jsonl"
    with raw_path.open("w") as handle:
        for r in ordered:
            handle.write(json.dumps(r, sort_keys=True) + "\n")

    counts = {v: sum(1 for r in ordered if r.get("verdict") == v) for v in ("strong", "maybe", "weak", "out")}
    gated = sum(1 for r in ordered if r.get("seniority_fit") in GATED_SENIORITY)
    errors = sum(1 for r in ordered if r.get("error"))
    return {
        "primitive": "evaluate_profile_candidates",
        "status": "completed",
        "created_at": now_iso(),
        "run_dir": str(run_dir),
        "model": args.model,
        "evaluated": len(ordered),
        "frontier_total": len(frontier),
        "missing_profiles": len(selected) - len(profiles),
        "seniority_gated": gated,
        "errors": errors,
        "elapsed_seconds": elapsed,
        "verdicts": counts,
        "raw_evaluations": str(raw_path),
        "next_command": (
            "uv run --project . python packs/search/primitives/capture_jd_evaluations/capture_jd_evaluations.py "
            f"--run-dir {run_dir} --evaluator-mode primitive --evaluator-model {args.model}"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated profile-search candidate evaluation with seniority hard gate")
    parser.add_argument("--run-dir", required=True, help="Profile-search run directory containing plan.json and candidate_frontier.jsonl")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, help="Evaluate only the top-N frontier candidates by best probe score; 0 = all")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit("OPENAI_API_KEY required")
    result = asyncio.run(evaluate_all(args))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
