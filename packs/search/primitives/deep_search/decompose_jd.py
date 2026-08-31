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
from typing import Any, Callable

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from openai_client import make_openai_client  # noqa: E402

try:
    from location_scope import location_scope_from_plan, query_location_label
    from pond_prompts import load_pond_prompt
    from precedents import retrieve_next_moves
except ImportError:  # pragma: no cover - package execution
    from .location_scope import location_scope_from_plan, query_location_label
    from .pond_prompts import load_pond_prompt
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

DYNAMIC_SIMPLE_SYSTEM = load_pond_prompt({"pond_prompt_family": "general"}, "pond-1")


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
            "Use the full JD to choose recognizable source occupations and defining experience. Do not put "
            "location in the model output; the approved location is appended after generation. Level, filters, "
            "and JD traits remain downstream."
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
        "Produce the primary recruiter query for this JD."
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
    cards = retrieve_next_moves(
        title=str(plan.get("job_title") or ""), brief=brief, query=jd, diagnosis="", limit=1,
    )
    return [
        {**card, "chain": list(card.get("chain") or [])[:1]}
        if card.get("chain") else card
        for card in cards[:1]
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


def query_request(
    *, jd: str, plan: dict[str, Any], n: int, model: str,
    reasoning_effort: str | None, system_prompt: str,
    dynamic_simple: bool, service_tier: str | None = None,
    use_precedents: bool = True,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": build_messages(
            jd,
            n,
            plan,
            system_prompt,
            dynamic_simple=dynamic_simple,
            precedent_cards=(
                dynamic_simple_precedents(jd, plan)
                if dynamic_simple and use_precedents else None
            ),
        ),
        "response_format": {"type": "json_object"},
    }
    normalized_model = str(model or "").lower().split("/")[-1]
    if reasoning_effort and normalized_model.startswith(("gpt-5", "o1", "o3", "o4")):
        request["reasoning_effort"] = reasoning_effort
    if service_tier:
        request["service_tier"] = service_tier
    return request


def generate_queries(
    *, jd: str, plan: dict[str, Any], n: int = 18, model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    system_prompt: str | None = None, dynamic_simple: bool = False,
    query_only: bool = False, api_key: str | None = None, client: Any | None = None,
    raw_response_path: Path | None = None,
    on_response: Callable[[Any], None] | None = None,
    service_tier: str | None = None,
    use_precedents: bool = True,
) -> list[dict[str, Any]]:
    """Run the production query-generation request and normalize its seeds."""
    if client is None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        client = make_openai_client(key)
    prompt = system_prompt or (load_pond_prompt(plan, "pond-1") if dynamic_simple else SYSTEM)
    response = client.chat.completions.create(**query_request(
        jd=jd,
        plan=plan,
        n=n,
        model=model,
        reasoning_effort=reasoning_effort,
        system_prompt=prompt,
        dynamic_simple=dynamic_simple,
        service_tier=service_tier,
        use_precedents=use_precedents,
    ))
    raw = response.choices[0].message.content or "{}"
    if raw_response_path is not None:
        raw_response_path.write_text(raw, encoding="utf-8")
    if on_response is not None:
        on_response(response)
    seeds = parse_seeds(json.loads(raw), n=None if dynamic_simple else n)
    if dynamic_simple and len(seeds) != 1:
        raise ValueError(f"dynamic simple generation must return 1 query; received {len(seeds)}")
    location, location_filters = location_scope_from_plan(plan)
    if dynamic_simple and location:
        seeds[0]["query"] = f'{seeds[0]["query"]} in {query_location_label(location_filters)}'
    if not query_only:
        apply_location_scope(seeds, location or "", location_filters)
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
    try:
        from deep_search_loop import validate_approved_plan
    except ImportError:  # pragma: no cover - package execution
        from .deep_search_loop import validate_approved_plan

    try:
        plan = validate_approved_plan(Path(args.plan))
        approved_location, _location_filters = location_scope_from_plan(plan)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({
            "primitive": "decompose_jd",
            "status": "failed",
            "error": f"approved recruiter plan failed validation: {exc}",
        }, indent=2))
        raise SystemExit(1) from exc
    default_system = load_pond_prompt(plan, "pond-1") if args.dynamic_simple else SYSTEM
    system_prompt = (Path(args.system_file).read_text(encoding="utf-8")
                     if args.system_file else default_system)
    if not system_prompt.strip():
        ap.error("system prompt must not be empty")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        seeds = generate_queries(
            jd=jd,
            plan=plan,
            n=args.n,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            system_prompt=system_prompt,
            dynamic_simple=args.dynamic_simple,
            query_only=args.query_only,
            api_key=args.api_key,
            raw_response_path=out.with_suffix(".raw.json"),
        )
    except ValueError as exc:
        if str(exc) != "OPENAI_API_KEY not set":
            raise
        print(json.dumps({"primitive": "decompose_jd", "status": "failed", "error": str(exc)}))
        raise SystemExit(1) from exc
    location = approved_location or ""
    geo_seeds = 0 if args.query_only else len(seeds) if location else 0

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
