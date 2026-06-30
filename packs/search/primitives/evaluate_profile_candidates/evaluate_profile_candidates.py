#!/usr/bin/env python3
"""Automated profile-search candidate evaluator (Task 5a primitive).

Reads candidate_frontier.json(l) + plan.json from a profile-search run
directory, loads hydrated profiles from profile-search artifacts, and runs an
async LLM evaluation per candidate. The model produces structured judgments
only — per-trait evidence statuses, excellence subscores (trajectory /
pedigree / impact), seniority fit, and caveats. The final score and verdict
are computed deterministically in code from those judgments:

    trait_score    = (2*sum(must statuses) + sum(nice statuses)) / (2*n_must + n_nice)
    excellence     = 0.4*trajectory + 0.3*impact + 0.3*pedigree
    caveat_penalty = min(0.20, 0.05 * material_caveat_count)
    final_score    = 0.55*trait_score + 0.45*excellence - caveat_penalty

Verdict ladder (bar-raiser model — default is out):
    top_tier        in-band, no missing must-have, trait_score >= 0.85,
                    excellence >= 0.70
    high_potential  in-band, trait_score >= 0.60, trajectory >= 0.75
                    (confident diamond-in-the-rough)
    out             everyone else, including seniority-gated candidates

Seniority is a hard gate enforced in code: too_senior / too_junior /
wrong_track force verdict out and cap final_score at 0.3 regardless of
trait scores. Writes candidate_evaluations.raw.jsonl in the Task 5a schema so
capture_jd_evaluations / export_candidate_shortlist work unchanged.
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
SHARED_DIR = ROOT / "packs/search/primitives/shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from probe_artifacts import load_probe_summaries  # noqa: E402
DEFAULT_MODEL = os.environ.get("PROFILE_EVAL_MODEL", os.environ.get("JD_EVAL_MODEL", "gpt-5.4"))
DEFAULT_REASONING_EFFORT = os.environ.get("PROFILE_EVAL_REASONING_EFFORT", os.environ.get("JD_EVAL_REASONING_EFFORT", "medium"))
DEFAULT_CONCURRENCY = 100
DEFAULT_MAX_CANDIDATES = 0  # 0 = evaluate the full merged frontier

VALID_VERDICTS = {"top_tier", "high_potential", "out"}
VALID_SENIORITY = {"ideal", "acceptable", "too_senior", "too_junior", "wrong_track", "unknown"}
# Per-trait evidence ladder. The model assigns a labeled bucket; code maps it
# to a value. Anchored buckets are more consistent than raw 0-1 floats, and
# aggregation stays deterministic.
#   doing_now     doing this exact work in the current role
#   experienced   clear prior direct experience
#   capable       enough evidence to do the work / pick it up quickly
#   foundational  adjacent/foundational background + vertical expertise to slot in
#   thin          weak, speculative
#   missing       no evidence
STATUS_VALUE = {
    "doing_now": 0.95,
    "experienced": 0.80,
    "capable": 0.70,
    "foundational": 0.50,
    "thin": 0.25,
    "missing": 0.0,
    "unknown": 0.0,
    # Legacy buckets (old run dirs / fallbacks) folded onto the ladder.
    "strong": 0.80,
    "partial": 0.50,
    "weak": 0.25,
}
VALID_TRAIT_STATUS = set(STATUS_VALUE)
GATED_SENIORITY = {"too_senior", "too_junior", "wrong_track"}

# Deterministic scoring constants.
MUST_WEIGHT = 2.0
NICE_WEIGHT = 1.0
TRAIT_COMPONENT_WEIGHT = 0.55
EXCELLENCE_COMPONENT_WEIGHT = 0.45
EXCELLENCE_WEIGHTS = {"trajectory": 0.4, "impact": 0.3, "pedigree": 0.3}
CAVEAT_PENALTY_EACH = 0.05
CAVEAT_PENALTY_CAP = 0.20
# Quorum / consensus aggregation for must-haves. Real top candidates spike on
# most must-haves and have a gap or two; a linear mean punishes that gap
# proportionally, a quorum does not. We discount the candidate's weakest
# ~(1-QUORUM_FRACTION) of must-haves (they count QUORUM_DISCOUNT instead of
# full weight). For 2-4 must-haves this means "forgive one":
#   3/3 -> 1.00, 2/3 -> 0.87, 1/3 -> 0.43; 5/7 -> 0.89; 5/9 -> 0.72
QUORUM_FRACTION = 0.70
QUORUM_DISCOUNT = 0.30
NICE_BONUS_WEIGHT = 0.10  # nice-to-haves are upside, never a gate
# Thresholds anchored to the trait ladder: 0.80 == "experienced" across
# must-haves (you'd be lucky to have them); 0.60 == "capable" coverage
# (can do the work / pick it up quickly).
TOP_TIER_MIN_TRAIT = 0.80
TOP_TIER_MIN_EXCELLENCE = 0.70
HIGH_POTENTIAL_MIN_MUST = 0.60  # must-have coverage only; nice-to-haves are differentiators
HIGH_POTENTIAL_DIAMOND_MIN_MUST = 0.45  # lower coverage tolerated when trajectory is steep
HIGH_POTENTIAL_MIN_TRAJECTORY = 0.75
GATED_SCORE_CAP = 0.3

SYSTEM_PROMPT = """You are the bar-raiser for a team recruiting top-tier talent. Your default disposition is OUT.

You only surface candidates the hiring team would be lucky to get — people who would raise the team's bar, not people who could merely do the job. Eagerness test: if the evidence would not make a hiring manager move fast, the candidate does not belong on the shortlist.

You will receive: the job context (including hire stage), the seniority band / usable cutoff policy, must-have traits, nice-to-have traits, and one candidate profile.

You do NOT produce a final score or verdict. You produce structured judgments — per-trait evidence statuses, excellence subscores, seniority fit, and caveats. The final score and verdict are computed deterministically from your judgments, so be precise and calibrated: every status and subscore directly moves the ranking.

=== SENIORITY IS A HARD GATE, SEPARATE FROM SKILLS ===

The policy names a TARGET LEVEL for this role (e.g. senior IC, staff IC, lead, manager, director, VP, executive). If none is stated, treat the target as a senior INDIVIDUAL-CONTRIBUTOR role. Seniority is judged ASYMMETRICALLY around that target: you hire people who STEP UP into the role, never people who would step DOWN.

First decide seniority_fit from the candidate's CURRENT career level relative to the target:
- "ideal": current level is AT the target.
- "acceptable": current level is exactly ONE level BELOW the target — a strong candidate who would step UP. This is IN-BAND and is frequently the best hire; do not penalize ambition or being one rung below.
- "too_senior": current level is ONE level ABOVE the target, OR HIGHER. People do not step down, so they are OUT regardless of skill (they will decline or be a flight risk). For a senior-IC / lead target this means current CEOs, CTOs, CFOs, COOs, presidents, current founders/co-founders, VPs, heads-of, managing directors, general partners, board members, angel investors, and advisors are too_senior unless the target level explicitly includes them. (For a VP/exec target, a VP/Director IS in-band and only a CEO/founder/president/C-suite is too_senior.) A person whose CURRENT title mixes lower-level work with a founder/exec role is too_senior when the founder/exec role is their primary current identity.
- "too_junior": current level is TWO OR MORE levels below the target (e.g. interns, new grads, analysts for a senior role; an IC two rungs down for a director target).
- "wrong_track": different career lane (e.g. data scientist without pipeline ownership for a data-engineering role, pure people-manager without hands-on evidence, consultant/agency when the role is in-house).
- "unknown": genuinely cannot tell.

A candidate with deep matching skills but out-of-band seniority is OUT. Do NOT rescue someone one level too senior because of their skills — they will not step down. Past founder/exec roles are fine if the CURRENT role is in-band. The whole point: reach for people ready to step UP (target and one level below), never people who would step down (one level above and higher).

=== TRAIT EVIDENCE LADDER ===

For every provided trait, assign exactly one evidence level with a short cite from the profile. The levels are a capability ladder — "can this person do this part of the job, and how surely?":
- "doing_now": doing this exact work in the CURRENT role. They are clearly performing it today.
- "experienced": clear prior direct experience doing this work (not current, or current but lighter). Has demonstrably done it before.
- "capable": enough adjacent/recent evidence to do the work or pick it up quickly. Not a direct match, but the building blocks are plainly there.
- "foundational": foundational background plus enough product/vertical context to plausibly slot in, but no direct evidence of doing this specific work.
- "thin": weak or speculative — only a faint signal.
- "missing": no evidence in the profile.
- "unknown": profile genuinely cannot answer.

Discipline:
- Only profile evidence counts. Do not invent facts. No evidence is "missing" or "unknown", not "foundational".
- Recency: weight current and recent (last ~5 years) roles most heavily. A trait evidenced only by roles older than ~8 years caps at "capable".
- Cross-track: evidence from a different career lane than the trait implies caps at "capable", never "experienced"/"doing_now" (e.g. SRE/platform reliability work for a product-API-ownership trait).
- Brand-name employers, total years of experience, and seniority of past titles are NOT trait evidence by themselves. Score the trait, not the resume.
- Do not park everyone at "capable"/"foundational" to hedge. If they are clearly doing it now, say "doing_now"; if there is genuinely nothing, say "missing". Calibrate honestly — the buckets are the score.

=== EXCELLENCE SUBSCORES (0.0-1.0 each, evidence required) ===

- "trajectory": speed and steepness of growth — promotions and scope expansion relative to time, increasing difficulty of problems chosen, leaving comfortable roles for harder ones. This is where late bloomers and diamonds in the rough surface: a steep recent curve at unknown companies scores high.
- "pedigree": selectivity of companies, teams, and schools — known-strong engineering organizations, competitive programs, hard-to-get-into teams. List the companies and schools you counted. Pedigree is a prior, not a gate: it can RAISE the picture, never sink it. A high-trajectory candidate with a no-name background must not lose points here being scored low while trajectory carries them.
- "impact": concrete shipped outcomes with ownership — built X used by Y, owned the migration that did Z, scaled W. Outcomes, not responsibilities.

Calibration: 0.9+ is exceptional / top-decile; 0.7 clearly above average; 0.5 typical for the band; below 0.3 weak. Do not inflate. Most candidates are near 0.5 on most subscores.

=== HIRE STAGE BAR ===

The job context names a hire stage. Apply the matching bar:
- "founding_early": weight trajectory steepness, 0-to-1 ownership, breadth, speed of scope growth, comfort with ambiguity. A high-growth builder with 5 years beats a 20-year maintainer.
- "scaling_late": weight depth plus years of experience WITH continued growth — evidence of hardening MVPs/POCs into battle-tested production systems, reliability under real load, scaling teams and systems, leading through influence.

=== CAVEATS ===

Each caveat is {"text": "...", "material": true|false}. Mark material=true only when it would genuinely give the hiring manager pause for THIS role. Material caveats reduce the computed score; do not pad with trivia.

=== OUTPUT ===

Return ONLY a JSON object:
{
  "seniority_fit": "ideal|acceptable|too_senior|too_junior|wrong_track|unknown",
  "must_have": [{"trait": "<exact trait text>", "status": "doing_now|experienced|capable|foundational|thin|missing|unknown", "evidence": "<short cite from profile>"}],
  "nice_to_have": [{"trait": "<exact trait text>", "status": "...", "evidence": "..."}],
  "excellence": {
    "trajectory": {"score": 0.0, "evidence": "<short justification>"},
    "pedigree": {"score": 0.0, "evidence": "<short justification>", "companies": [], "schools": []},
    "impact": {"score": 0.0, "evidence": "<short justification>"}
  },
  "rationale": "<one short paragraph>",
  "caveats": [{"text": "<short string>", "material": true}]
}

Include every provided trait exactly once with its exact text.
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
        for probe in load_probe_summaries(summaries):
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
    hire_stage = plan.get("hire_stage") or "founding_early"
    parts = [
        f"Job: {plan.get('job_title') or ''} ({plan.get('normalized_archetype') or ''})",
        f"Hire stage: {hire_stage}",
        f"Target level: {plan.get('target_level') or 'senior individual contributor (default)'}",
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


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------

def quorum_aggregate(values: list[float], discount: float = QUORUM_DISCOUNT, quorum_fraction: float = QUORUM_FRACTION) -> float:
    """Consensus aggregate: the weakest ~(1-quorum_fraction) of the values are
    discounted to ``discount`` weight, the rest count fully. Equal values are
    unaffected (a uniform list returns its own value). Never discounts all."""
    if not values:
        return 0.0
    s = sorted(values)  # ascending; weakest first
    n = len(s)
    k = round((1.0 - quorum_fraction) * n)
    k = max(0, min(k, n - 1))
    weights = [discount] * k + [1.0] * (n - k)
    return sum(w * v for w, v in zip(weights, s)) / sum(weights)


def _status_values(traits: list[dict[str, Any]]) -> list[float]:
    return [STATUS_VALUE.get(t.get("status", "unknown"), 0.0) for t in traits]


def compute_trait_score(must: list[dict[str, Any]], nice: list[dict[str, Any]]) -> float:
    """Quorum over must-haves, plus nice-to-haves as a small additive bonus.

    Must-haves drive the score via the consensus aggregate (a gap or two is
    forgiven). Nice-to-haves are differentiators: they can only RAISE the
    score (capped at 1.0), never sink a candidate below the bar.
    """
    must_vals = _status_values(must)
    nice_vals = _status_values(nice)
    must_q = quorum_aggregate(must_vals)
    nice_avg = sum(nice_vals) / len(nice_vals) if nice_vals else 0.0
    return min(1.0, must_q + NICE_BONUS_WEIGHT * nice_avg)


def compute_excellence(excellence: dict[str, Any]) -> float:
    total = 0.0
    for key, weight in EXCELLENCE_WEIGHTS.items():
        block = excellence.get(key) or {}
        try:
            score = max(0.0, min(1.0, float(block.get("score", 0))))
        except (TypeError, ValueError):
            score = 0.0
        total += weight * score
    return total


def caveat_penalty(caveats: list[dict[str, Any]]) -> float:
    material = sum(1 for c in caveats if isinstance(c, dict) and c.get("material"))
    return min(CAVEAT_PENALTY_CAP, CAVEAT_PENALTY_EACH * material)


def must_coverage(must: list[dict[str, Any]]) -> float:
    """Must-have coverage as the quorum/consensus aggregate (0-1)."""
    return quorum_aggregate(_status_values(must))


def decide_verdict(
    seniority_fit: str,
    trait_score: float,
    excellence: float,
    trajectory: float,
    must: list[dict[str, Any]],
) -> str:
    """Deterministic verdict ladder. Default is out.

    top_tier gates on combined trait coverage (must + nice) plus excellence.
    high_potential gates on must-have coverage only — nice-to-haves are
    differentiators and must not sink a steep-trajectory candidate — plus a
    high trajectory subscore (the explicit diamond-in-the-rough bet).
    """
    if seniority_fit in GATED_SENIORITY:
        return "out"
    must_missing = any(t.get("status") in ("missing",) for t in must)
    if not must_missing and trait_score >= TOP_TIER_MIN_TRAIT and excellence >= TOP_TIER_MIN_EXCELLENCE:
        return "top_tier"
    cov = must_coverage(must)
    # high_potential = solid must-have coverage (can do the work / pick it up
    # quickly), OR the diamond-in-the-rough escape: lighter coverage rescued
    # by a steep trajectory. Trajectory is an OR escape hatch, never a second
    # AND-gate that sinks solid-coverage candidates.
    if cov >= HIGH_POTENTIAL_MIN_MUST:
        return "high_potential"
    if cov >= HIGH_POTENTIAL_DIAMOND_MIN_MUST and trajectory >= HIGH_POTENTIAL_MIN_TRAJECTORY:
        return "high_potential"
    return "out"


def normalize_evaluation(parsed: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    traits = plan.get("traits", {}) or {}
    must_expected = [t.get("trait") for t in traits.get("must_have", []) if t.get("trait")]
    nice_expected = [t.get("trait") for t in traits.get("nice_to_have", []) if t.get("trait")]

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

    def norm_excellence(raw: Any) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        out: dict[str, Any] = {}
        for key in EXCELLENCE_WEIGHTS:
            block = raw.get(key) if isinstance(raw.get(key), dict) else {}
            try:
                score = max(0.0, min(1.0, float(block.get("score", 0))))
            except (TypeError, ValueError):
                score = 0.0
            entry: dict[str, Any] = {
                "score": round(score, 2),
                "evidence": str(block.get("evidence", ""))[:400],
            }
            if key == "pedigree":
                entry["companies"] = [str(c)[:80] for c in (block.get("companies") or []) if c][:12]
                entry["schools"] = [str(s)[:80] for s in (block.get("schools") or []) if s][:8]
            out[key] = entry
        return out

    def norm_caveats(raw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            raw = [raw] if raw else []
        for c in raw:
            if isinstance(c, dict) and c.get("text"):
                out.append({"text": str(c["text"])[:200], "material": bool(c.get("material"))})
            elif isinstance(c, str) and c.strip():
                # Legacy plain-string caveats: keep, but do not penalize.
                out.append({"text": c[:200], "material": False})
        return out[:8]

    seniority_fit = str(parsed.get("seniority_fit", "")).lower()
    if seniority_fit not in VALID_SENIORITY:
        seniority_fit = "unknown"

    must = norm_traits(parsed.get("must_have"), must_expected)
    nice = norm_traits(parsed.get("nice_to_have"), nice_expected)
    excellence_block = norm_excellence(parsed.get("excellence"))
    caveats = norm_caveats(parsed.get("caveats"))

    trait_score = compute_trait_score(must, nice)
    excellence_score = compute_excellence(excellence_block)
    trajectory = float(excellence_block.get("trajectory", {}).get("score", 0))
    penalty = caveat_penalty(caveats)

    final_score = TRAIT_COMPONENT_WEIGHT * trait_score + EXCELLENCE_COMPONENT_WEIGHT * excellence_score - penalty
    final_score = max(0.0, min(1.0, final_score))

    verdict = decide_verdict(seniority_fit, trait_score, excellence_score, trajectory, must)
    # Hard gate enforced in code, not just the prompt.
    if seniority_fit in GATED_SENIORITY:
        verdict = "out"
        final_score = min(final_score, GATED_SCORE_CAP)

    return {
        # jd_score keeps its name for downstream contract compatibility, but
        # it is now computed in code, never model-assigned.
        "jd_score": round(final_score, 3),
        "verdict": verdict,
        "seniority_fit": seniority_fit,
        "must_have": must,
        "nice_to_have": nice,
        "excellence": excellence_block,
        "score_breakdown": {
            "trait_score": round(trait_score, 3),
            "excellence_score": round(excellence_score, 3),
            "caveat_penalty": round(penalty, 3),
            "formula": f"{TRAIT_COMPONENT_WEIGHT}*trait + {EXCELLENCE_COMPONENT_WEIGHT}*excellence - penalty",
        },
        "rationale": str(parsed.get("rationale", ""))[:1200],
        "caveats": caveats,
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
    service_tier: str | None = None,
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
            "caveats": [{"text": "missing_hydrated_profile", "material": True}],
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
                if service_tier:
                    # flex = ~50% cheaper, slower batch tier (resource_unavailable 429s are
                    # retried by the loop below); ideal for non-latency-sensitive reranking.
                    kwargs["service_tier"] = service_tier
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
        "caveats": [{"text": "evaluation_error", "material": True}],
        "error": last_error[:400],
    }


VERDICT_ORDER = {"top_tier": 0, "high_potential": 1, "out": 2}


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
            args.service_tier,
        )
        for candidate in selected
    ]
    results = await asyncio.gather(*tasks)
    elapsed = round(time.monotonic() - started, 2)

    # Rank by verdict tier first, then computed score; out candidates sink.
    ordered = sorted(
        results,
        key=lambda r: (VERDICT_ORDER.get(r.get("verdict", "out"), 9), -r.get("jd_score", 0)),
    )
    for i, r in enumerate(ordered):
        r["rank"] = i + 1

    raw_path = run_dir / "candidate_evaluations.raw.jsonl"
    with raw_path.open("w") as handle:
        for r in ordered:
            handle.write(json.dumps(r, sort_keys=True) + "\n")

    counts = {v: sum(1 for r in ordered if r.get("verdict") == v) for v in ("top_tier", "high_potential", "out")}
    gated = sum(1 for r in ordered if r.get("seniority_fit") in GATED_SENIORITY)
    errors = sum(1 for r in ordered if r.get("error"))
    return {
        "primitive": "evaluate_profile_candidates",
        "status": "completed",
        "created_at": now_iso(),
        "run_dir": str(run_dir),
        "model": args.model,
        "service_tier": args.service_tier,
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
    parser = argparse.ArgumentParser(description="Automated profile-search candidate evaluation with deterministic bar-raiser scoring")
    parser.add_argument("--run-dir", required=True, help="Profile-search run directory containing plan.json and candidate_frontier.jsonl")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, help="Evaluate only the top-N frontier candidates by best probe score; 0 = all (default)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--service-tier", default=None,
                        help="OpenAI service tier: 'flex' (~50%% cheaper, slower batch tier — use for "
                             "reranking; pair with a higher --timeout) | auto | default | priority. "
                             "Default None = account default (unchanged behavior for other callers).")
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit("OPENAI_API_KEY required")
    result = asyncio.run(evaluate_all(args))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
