"""Generate reviewed query seeds from a JD in one model call.

Default/exhaustive callers request N diverse work-described seeds. The default
simple deep path passes `--dynamic-simple`: emit one literal high-recall query,
or two only when the second describes a genuinely distinct candidate population
or career transition. Downstream retrieval uses each query verbatim as its
semantic input; the ordinary expansion primitive derives structured traits.

Output: seeds.json = [{"key": "q00", "query": "...", "required_location": "...",
"location_filters": {...}}, ...] — consumed by deep_search/run_wide_search.py.
One OpenAI call (json_object), mirroring expand_search_request's client pattern.

Changelog:
  2026-08-18  Add dynamic simple generation without changing exhaustive N-seed mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from openai_client import make_openai_client  # noqa: E402

try:
    from location_scope import location_scope_from_plan
    from precedents import retrieve_next_moves
except ImportError:  # pragma: no cover - package execution
    from .location_scope import location_scope_from_plan
    from .precedents import retrieve_next_moves

DEFAULT_MODEL = os.environ.get("RECRUIT_DECOMPOSE_MODEL", "gpt-4o")
DEFAULT_REASONING_EFFORT = os.environ.get("RECRUIT_DECOMPOSE_REASONING_EFFORT")

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
    "separately. Do not put a location in the seed sentences either; the approved recruiter plan "
    "supplies the authoritative structured location filter.\n"
    'Return strict JSON: {"seeds": ["sentence 1", ...]} with exactly the requested seed count.'
)

DYNAMIC_SIMPLE_GUIDANCE = """Write the smallest useful recruiter search set from a job description. You will inspect the top 50
profiles, so retrieve a coherent candidate pond rather than summarize the ideal person. Do not use benchmark
examples, company-specific rules, or a fixed strategy roster.

Read the work and qualification sections before trusting the posting title. The title is a clue, not authority.
Identify the broadest established source occupation whose members can already do the role's irreducible work.
Use the JD's required background, experience, proficiency, and recurring work as evidence. A preferred item may
still define the pond when the actual work repeatedly depends on it; a generic bonus or personality claim does not.
The source occupation need not appear verbatim: infer which established professions normally perform the recurring
work. Do not infer one from a vague adjective, isolated duty, collaborator, customer, or reporting relationship.
Before writing, silently list the concrete outputs the person creates, operates, or evaluates and the established
occupations that normally own those outputs. Choose from that list, not from the audience, product, or team being
served. Separately list occupations explicitly accepted in candidate-background language, then collapse aliases.
Use ordinary labor-market meaning: building or operating software maps to the appropriate software occupation;
creating visual or product experiences maps to design; producing technical documentation or written research maps
to technical writing; designing tests, evaluations, benchmarks, or quality systems maps to QA or evaluation work;
operating processes, vendors, logistics, or programs maps to operations; and acquisition, selling, or customer
support maps to its conventional go-to-market function. Apply a mapping only to substantial recurring output.

Use this query grammar:
  <source occupation> [with <one defining experience>] [in <approved location>]
The source occupation is a recognizable job people hold. The defining experience is the single capability or
domain that separates qualified members of that occupation from the rest. It need not be another job title.
Default to the plain occupation. Add an experience clause only when the occupation alone would retrieve a
materially broader, wrong population; never add one merely to echo the JD or restate the occupation.

For software work, start from the broad software occupation unless one engineering lane is required throughout
the JD. Use the conventional lane only when the role is truly limited to it; otherwise attach the indispensable
work area as experience. Keep an explicit programming language or professional technology only when the JD makes
real proficiency in it a core qualification, not when it merely appears in a stack list.

For operations work, identify the underlying operating function and whether the hire is an individual contributor
or a true functional leader. Use a conventional operations occupation with the defining operating domain as
experience. Do not repeat an invented internal operations title. If the JD explicitly accepts established feeder
professions, those professions are candidate ponds; for a vague generalist destination, prefer them over the
destination title. When two distinct professions are independently sufficient, preserve both as separate ponds.
Strip ordinary destination level words, including managerial modifiers, and let ranking judge readiness; retain
level only when it changes who could credibly take the job.

For any hybrid, choose the occupation accountable for the final work product and attach the other indispensable
capability as experience. If either of two different source occupations could independently qualify, query each
direction separately. When the recurring work genuinely spans two professional crafts, infer those source
occupations even if the JD uses an internal title instead of naming them. Each craft must own a substantial output,
not merely serve or collaborate with the other. Do not combine unrelated occupations with "or" merely to save a
query; combine close aliases when they describe the same pond.

Query 1 is the largest coherent source population. Query 2 is optional and exists only for a genuinely different
source occupation or prior-career path that query 1 would miss. It must add different people, not paraphrase,
narrow, or widen query 1. Prefer one query when there is only one pond.

Keep every query short, positive, and self-contained. Use the approved location exactly when one exists. Do not
include exclusions, responsibilities, long skill lists, years, pedigree, employer identity, company stage, quality
adjectives, or ordinary seniority. Do not append the person, customer, product, or team being supported. Preserve
a license, legal authority, or other true occupational boundary. Other reviewed filters remain downstream.

Before returning, verify: each query names a real source occupation; each experience clause is indispensable;
query 2 reaches different candidates; and no internal destination wording displaced a broader credible pond."""

DYNAMIC_SIMPLE_SYSTEM = (
    DYNAMIC_SIMPLE_GUIDANCE
    + "\n"
    '- Return strict JSON only: {"seeds": ["query 1"]} or '
    '{"seeds": ["query 1", "query 2"]}.'
)


def apply_location_scope(
    seeds: list[dict[str, Any]],
    location: str,
    location_filters: dict[str, list[str]],
) -> int:
    """Bind the approved JD location to every seed. Returns the constrained count."""
    location = (location or "").strip()
    for seed in seeds:
        seed["required_location"] = location
        seed["location_filters"] = location_filters
    return len(seeds) if location else 0


def plan_context(plan: dict[str, Any] | None, *, dynamic_simple: bool = False) -> str:
    if not plan:
        return ""
    if dynamic_simple:
        compact = {
            "job_title": plan.get("job_title"),
            "location": (plan.get("search_scope") or {}).get("location"),
            "candidate_populations": plan.get("candidate_populations") or [],
        }
        return (
            "\n\nSEARCH PLANNING CONTEXT:\n"
            f"{json.dumps(compact, indent=2)}\n"
            "The exact approved location is authoritative; the job title is only a clue. Treat "
            "candidate_populations as the JD-grounded pond menu and consider its population-bearing "
            "hints before the title or retrieved precedents. Ranking-boost hints may shape ordering or "
            "an experience clause but never define a pond; comp-band-anchor hints never define a query. "
            "When department-title tension, portfolio culture, or dual-craft hints agree, make the "
            "department/portfolio craft the primary source occupation and the other craft a defining "
            "experience; use the reverse occupation as the distinct second pond when credible. Choose "
            "query 1 by the strongest independent hint support, not by candidate_populations list order: "
            "a source craft supported by a portfolio-signal or department-title-tension takes precedence "
            "over the pure implementation side when both are credible. "
            "Use the full JD to choose recognizable source occupations and defining experience. Include "
            "the approved location exactly in every query. Level, filters, and JD traits remain downstream."
        )
    traits = plan.get("traits") or {}
    compact = {
        "job_title": plan.get("job_title"),
        "normalized_archetype": plan.get("normalized_archetype"),
        "hire_stage": plan.get("hire_stage"),
        "target_level": plan.get("target_level"),
        "location": (plan.get("search_scope") or {}).get("location"),
        "filters": plan.get("filters") or [],
        "retrieval_filters": plan.get("retrieval_filters") or {},
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


def build_messages(
    jd: str,
    n: int,
    plan: dict[str, Any] | None = None,
    system_prompt: str = SYSTEM,
    dynamic_simple: bool = False,
    precedent_cards: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    instruction = (
        "Produce the smallest useful search set for this JD: one query by default, and at most "
        "two only when the second targets a genuinely distinct candidate population the first "
        "would miss."
        if dynamic_simple
        else f"Produce exactly {n} diverse work-described seeds for this JD:"
    )
    precedent_context = ""
    if dynamic_simple and precedent_cards:
        precedent_context = (
            "\n\nRETRIEVED RECRUITER PRECEDENTS:\n"
            f"{json.dumps(precedent_cards, indent=2)}\n"
            "Use a precedent only when its source population and defining work are analogous to this JD. "
            "Quality tiers are evidence strength, not permission to copy an irrelevant query."
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"{instruction}\n\n{jd.strip()}"
            f"{plan_context(plan, dynamic_simple=dynamic_simple)}"
            f"{precedent_context}"
        )},
    ]


def dynamic_simple_precedents(jd: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    traits = (plan.get("traits") or {}).get("must_have") or []
    brief = {
        "occupation": plan.get("normalized_archetype"),
        "defining_capability": " ".join(str(row.get("trait") or "") for row in traits),
    }
    return retrieve_next_moves(
        title=str(plan.get("job_title") or ""), brief=brief, query=jd, diagnosis="", limit=3,
    )


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
    ap = argparse.ArgumentParser(description="Generate fixed-N or dynamic simple query seeds from a JD (1 LLM call).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--jd", help="JD text")
    g.add_argument("--jd-file", help="Path to a file containing the JD text")
    ap.add_argument("--print-system", action="store_true",
                    help="Print the shipped system prompt and exit; no model call")
    ap.add_argument("--system-file", default=None,
                    help="Use this reviewed system prompt instead of the shipped default")
    ap.add_argument("--n", type=int, default=18, help="Number of seeds (default 18)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                    help="Reasoning effort for supported query-generation models")
    ap.add_argument("--query-only", action="store_true",
                    help="Write only key/query fields; shared plan scope is applied later")
    ap.add_argument("--dynamic-simple", action="store_true",
                    help="Generate one literal query, or two only for distinct candidate populations")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--out", help="Where to write seeds.json")
    ap.add_argument("--plan",
                    help="Approved plan.json; supplies authoritative traits and structured location scope")
    args = ap.parse_args()

    if args.print_system:
        print(DYNAMIC_SIMPLE_SYSTEM if args.dynamic_simple else SYSTEM)
        return
    if bool(args.jd) == bool(args.jd_file):
        ap.error("provide exactly one of --jd or --jd-file")
    if not args.plan:
        ap.error("--plan is required")
    if not args.out:
        ap.error("--out is required")

    jd = Path(args.jd_file).read_text(encoding="utf-8") if args.jd_file else args.jd
    default_system = DYNAMIC_SIMPLE_SYSTEM if args.dynamic_simple else SYSTEM
    system_prompt = (Path(args.system_file).read_text(encoding="utf-8")
                     if args.system_file else default_system)
    if not system_prompt.strip():
        ap.error("system prompt must not be empty")
    try:
        from deep_search_loop import validate_approved_plan
    except ImportError:  # pragma: no cover - package execution
        from .deep_search_loop import validate_approved_plan

    try:
        plan = validate_approved_plan(Path(args.plan))
        approved_location, location_filters = location_scope_from_plan(plan)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({
            "primitive": "decompose_jd",
            "status": "failed",
            "error": f"approved recruiter plan failed validation: {exc}",
        }, indent=2))
        raise SystemExit(1) from exc
    key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        print(json.dumps({"primitive": "decompose_jd", "status": "failed", "error": "OPENAI_API_KEY not set"}))
        raise SystemExit(1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    client = make_openai_client(key)
    request: dict[str, Any] = {
        "model": args.model,
        "messages": build_messages(
            jd,
            args.n,
            plan,
            system_prompt,
            dynamic_simple=args.dynamic_simple,
            precedent_cards=(dynamic_simple_precedents(jd, plan) if args.dynamic_simple else None),
        ),
        "response_format": {"type": "json_object"},
    }
    normalized_model = str(args.model or "").lower().split("/")[-1]
    if args.reasoning_effort and normalized_model.startswith(("gpt-5", "o1", "o3", "o4")):
        request["reasoning_effort"] = args.reasoning_effort
    resp = client.chat.completions.create(**request)
    raw = resp.choices[0].message.content or "{}"
    out.with_suffix(".raw.json").write_text(raw, encoding="utf-8")
    obj = json.loads(raw)
    seeds = parse_seeds(obj, n=None if args.dynamic_simple else args.n)
    if args.dynamic_simple and not 1 <= len(seeds) <= 2:
        raise ValueError(
            f"dynamic simple generation must return 1 or 2 queries; received {len(seeds)}"
        )
    location = approved_location or ""
    geo_seeds = 0 if args.query_only else apply_location_scope(seeds, location, location_filters)

    out.write_text(json.dumps(seeds, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "decompose_jd", "status": "completed", "seeds": len(seeds),
                      "location": location, "geo_seeds": geo_seeds, "global_seeds": len(seeds) - geo_seeds,
                      "model": args.model,
                      "reasoning_effort": args.reasoning_effort,
                      "query_only": args.query_only,
                      "dynamic_simple": args.dynamic_simple,
                      "system_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
                      "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
