"""Decompose a JD into N diverse, work-described search seeds (one LLM call).

This replaces the harness/sub-agent decomposition step with a callable primitive so any
harness (Claude Code, Codex, another skill) produces the seeds identically. Each seed is a
RICH, work-described sentence (what the person *built/did*, not a job title) — diversity across
seeds is the whole point, because the downstream `--preserve-query-semantic` path uses each seed
verbatim as the retrieval vector, and overlapping/title-y seeds collapse recall (see
packs/search/docs/deep-search-ground-truth-status.md).

Output: seeds.json = [{"key": "q00", "query": "..."}, ...] — consumed by deep_search/run_wide_search.py.
One OpenAI call (json_object), mirroring expand_search_request's client pattern.
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

DEFAULT_MODEL = os.environ.get("RECRUIT_DECOMPOSE_MODEL", "gpt-4o")

SYSTEM = (
    "You are a technical recruiting sourcer. Decompose a job description into a set of DIVERSE "
    "candidate-archetype search seeds for a vector + keyword talent search. Hard rules:\n"
    "- Each seed is ONE rich sentence describing the WORK and EXPERIENCE of a kind of candidate "
    "(what they built/owned/shipped), NOT a job title.\n"
    "- MAXIMIZE diversity across seeds and MINIMIZE overlap: vary the lead concept, the sub-skills, "
    "the tools, the company type, and the problem domain so the seeds cover different regions of the "
    "candidate space. Avoid every seed starting with the same words.\n"
    "- Cover the must-haves AND the bonus/adjacent angles of the role.\n"
    "- Do NOT add seniority or company hard filters to the seed sentences — those are handled "
    "separately. Do not put a location in the seed sentences either.\n"
    "- ALSO extract the job's LOCATION from the JD, normalized to the metro area a candidate search "
    'would use (e.g. "San Francisco, CA" / "SF" / "on-site in SF" -> "San Francisco Bay Area"; '
    '"NYC" -> "New York City metropolitan area"). Use the empty string when the role is remote, '
    "location-flexible, or the JD does not state one.\n"
    'Return strict JSON: {"seeds": ["sentence 1", ...], "location": "<metro or empty>"} with exactly '
    "the requested seed count."
)

# Geo-first sourcing: most probes carry the JD's metro so the initial blast radius is small,
# but every GLOBAL_SEED_EVERY-th seed stays location-free — measured GT for an SF role included
# strong candidates in Seattle/NY/Bengaluru, so geo-only sourcing loses real people (relocators,
# remote-friendly hires). The expand-from-anchor epochs then widen from whoever survives judging.
GLOBAL_SEED_EVERY = 4


def apply_location_mix(seeds: list[dict[str, str]], location: str) -> int:
    """Append the JD metro to all but every GLOBAL_SEED_EVERY-th seed (in place).
    Returns how many seeds were geo-constrained."""
    location = (location or "").strip()
    if not location:
        return 0
    geo = 0
    for i, seed in enumerate(seeds):
        if (i + 1) % GLOBAL_SEED_EVERY == 0:
            continue  # recall hedge: keep this one global
        seed["query"] = f"{seed['query'].rstrip('.')} — based in {location}"
        geo += 1
    return geo


def resolve_location(
    cli_location: str | None,
    plan: dict[str, Any] | None,
    extracted_location: str | None,
) -> str:
    """Resolve geo precedence while preserving an approved explicit global/null scope."""
    if cli_location is not None:
        location = cli_location
    elif plan is not None:
        location = (plan.get("search_scope") or {}).get("location") or ""
    else:
        location = extracted_location or ""
    return "" if str(location).strip().lower() == "global" else str(location)


def plan_context(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    traits = plan.get("traits") or {}
    compact = {
        "job_title": plan.get("job_title"),
        "normalized_archetype": plan.get("normalized_archetype"),
        "hire_stage": plan.get("hire_stage"),
        "target_level": plan.get("target_level"),
        "location": (plan.get("search_scope") or {}).get("location"),
        "core_groups": plan.get("core_groups") or [],
        "must_have": traits.get("must_have") or [],
        "nice_to_have": traits.get("nice_to_have") or [],
        "recruiter_policy": plan.get("recruiter_policy") or {},
    }
    return (
        "\n\nAPPROVED RECRUITER PLAN (authoritative):\n"
        f"{json.dumps(compact, indent=2)}\n"
        "Every core group and must-have needs explicit probe coverage. Nice-to-haves and adjacent "
        "backgrounds broaden recall, but must not replace the approved core coverage."
    )


def build_messages(jd: str, n: int, plan: dict[str, Any] | None = None) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Produce exactly {n} diverse work-described seeds for this JD:\n\n{jd.strip()}"
            f"{plan_context(plan)}"
        )},
    ]


def parse_seeds(obj: dict[str, Any], n: int | None = None) -> list[dict[str, str]]:
    """Normalize the model's JSON into [{key, query}]. Accepts {"seeds":[str|{query}]}."""
    raw = obj.get("seeds") if isinstance(obj, dict) else obj
    if not isinstance(raw, list):
        raise ValueError("expected a 'seeds' list in the response")
    seeds: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        q = item if isinstance(item, str) else (item.get("query") or item.get("seed") or "")
        q = str(q).strip()
        if q:
            seeds.append({"key": f"q{i:02d}", "query": q})
    if n is not None:
        seeds = seeds[:n]
    if not seeds:
        raise ValueError("no non-empty seeds parsed")
    return seeds


def main() -> None:
    ap = argparse.ArgumentParser(description="Decompose a JD into N diverse work-described seeds (1 LLM call).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--jd", help="JD text")
    g.add_argument("--jd-file", help="Path to a file containing the JD text")
    ap.add_argument("--n", type=int, default=18, help="Number of seeds (default 18)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--out", required=True, help="Where to write seeds.json")
    ap.add_argument("--plan", default=None, help="Approved plan.json; its core groups and traits must shape seed coverage")
    ap.add_argument("--location", default=None,
                    help="Override the JD-extracted metro for geo-first probes "
                         "('global' disables geo-constraining entirely)")
    args = ap.parse_args()

    jd = Path(args.jd_file).read_text(encoding="utf-8") if args.jd_file else args.jd
    key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        print(json.dumps({"primitive": "decompose_jd", "status": "failed", "error": "OPENAI_API_KEY not set"}))
        raise SystemExit(1)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else None
    client = make_openai_client(key)
    resp = client.chat.completions.create(
        model=args.model,
        messages=build_messages(jd, args.n, plan),
        response_format={"type": "json_object"},
    )
    obj = json.loads(resp.choices[0].message.content or "{}")
    seeds = parse_seeds(obj, n=args.n)
    # Geo-first: JD-extracted metro unless overridden; --location global turns it off.
    location = resolve_location(args.location, plan, obj.get("location"))
    geo_seeds = apply_location_mix(seeds, location)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(seeds, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "decompose_jd", "status": "completed", "seeds": len(seeds),
                      "location": location, "geo_seeds": geo_seeds, "global_seeds": len(seeds) - geo_seeds,
                      "model": args.model, "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
