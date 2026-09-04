"""Generate the reviewed Pond-1 query from a JD in one model call.

Emits one literal high-recall candidate-population query using the plan's pond
prompt family, with at most one retrieved move card as reviewed guidance. The
approved plan location is appended after generation; downstream retrieval uses
the query verbatim as its semantic input and the ordinary expansion primitive
derives structured traits.

Output: queries.json = [{"key": "q00", "query": "..."}]; queries.raw.json keeps
the parsed model response plus the injected precedent cards.

Changelog:
  2026-09-02  The N-seed mode that fed the deleted exhaustive engine is gone;
              the Pond-1 query is the only output.
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

SYSTEM = load_pond_prompt({"pond_prompt_family": "general"}, "pond-1")


def plan_context(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    compact = {
        "job_title": plan.get("job_title"),
        "location": (plan.get("search_scope") or {}).get("location"),
        "candidate_populations": plan.get("candidate_populations") or [],
    }
    return (
        "\n\nSEARCH PLANNING CONTEXT:\n"
        f"{json.dumps(compact, indent=2)}\n"
        "The exact approved location is authoritative; the job title is only a clue. Treat "
        "candidate_populations as the JD-grounded pond menu and consider its hints before the "
        "title or retrieved precedents. "
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


def build_messages(
    jd: str,
    plan: dict[str, Any] | None = None,
    system_prompt: str = SYSTEM,
    precedent_cards: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    precedent_context = ""
    if precedent_cards:
        precedent_context = (
            "\n\nRETRIEVED RECRUITER PRECEDENTS:\n"
            f"{json.dumps(precedent_cards, indent=2)}\n"
            "Use a precedent only when its source population and defining work are analogous to this JD. "
            "Quality tiers are evidence strength, not permission to copy an irrelevant query."
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Produce the primary recruiter query for this JD.\n\n{jd.strip()}"
            f"{plan_context(plan)}"
            f"{precedent_context}"
        )},
    ]


def retrieve_precedent_cards(jd: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The single best move card for this JD, chain cut to its first link."""
    brief = {
        "occupation": plan.get("normalized_archetype"),
        "defining_capability": " ".join(
            row["trait"] for row in plan.get("traits") or [] if row["kind"] == "capability"
        ),
    }
    cards = retrieve_next_moves(
        title=str(plan.get("job_title") or ""), brief=brief, query=jd, diagnosis="", limit=1,
    )
    return [
        {**card, "chain": list(card.get("chain") or [])[:1]}
        if card.get("chain") else card
        for card in cards[:1]
    ]


def parse_seeds(obj: dict[str, Any]) -> list[dict[str, str]]:
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
    if not seeds:
        raise ValueError("no non-empty seeds parsed")
    return seeds


def query_request(
    *, jd: str, plan: dict[str, Any], model: str,
    reasoning_effort: str | None, system_prompt: str,
    service_tier: str | None = None,
    precedent_cards: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": build_messages(jd, plan, system_prompt, precedent_cards=precedent_cards),
        "response_format": {"type": "json_object"},
    }
    normalized_model = str(model or "").lower().split("/")[-1]
    if reasoning_effort and normalized_model.startswith(("gpt-5", "o1", "o3", "o4")):
        request["reasoning_effort"] = reasoning_effort
    if service_tier:
        request["service_tier"] = service_tier
    return request


def generate_queries(
    *, jd: str, plan: dict[str, Any], model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    system_prompt: str | None = None,
    api_key: str | None = None, client: Any | None = None,
    raw_response_path: Path | None = None,
    on_response: Callable[[Any], None] | None = None,
    service_tier: str | None = None,
    use_precedents: bool = True,
) -> list[dict[str, Any]]:
    """Run the Pond-1 query request and return exactly one located seed."""
    if client is None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        client = make_openai_client(key)
    prompt = system_prompt or load_pond_prompt(plan, "pond-1")
    precedent_cards = retrieve_precedent_cards(jd, plan) if use_precedents else []
    response = client.chat.completions.create(**query_request(
        jd=jd,
        plan=plan,
        model=model,
        reasoning_effort=reasoning_effort,
        system_prompt=prompt,
        service_tier=service_tier,
        precedent_cards=precedent_cards,
    ))
    raw = response.choices[0].message.content or "{}"
    if raw_response_path is not None:
        raw_response_path.write_text(raw, encoding="utf-8")
    if on_response is not None:
        on_response(response)
    parsed = json.loads(raw)
    if raw_response_path is not None:
        raw_response_path.write_text(json.dumps(
            {**parsed, "precedent_cards": precedent_cards}, indent=2) + "\n", encoding="utf-8")
    seeds = parse_seeds(parsed)
    if len(seeds) != 1:
        raise ValueError(f"Pond-1 generation must return 1 query; received {len(seeds)}")
    location, location_filters = location_scope_from_plan(plan)
    if location:
        seeds[0]["query"] = f'{seeds[0]["query"]} in {query_location_label(location_filters)}'
    return seeds


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the reviewed Pond-1 query from a JD (1 LLM call).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--jd", help="JD text")
    g.add_argument("--jd-file", help="Path to a file containing the JD text")
    ap.add_argument("--system-file", default=None,
                    help="Use this reviewed system prompt instead of the plan's pond-1 prompt")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                    help="Reasoning effort for supported query-generation models")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--out", help="Where to write queries.json")
    ap.add_argument("--plan",
                    help="Approved plan.json; supplies authoritative traits and structured location scope")
    args = ap.parse_args()

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
    system_prompt = (Path(args.system_file).read_text(encoding="utf-8")
                     if args.system_file else load_pond_prompt(plan, "pond-1"))
    if not system_prompt.strip():
        ap.error("system prompt must not be empty")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        seeds = generate_queries(
            jd=jd,
            plan=plan,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            system_prompt=system_prompt,
            api_key=args.api_key,
            raw_response_path=out.with_suffix(".raw.json"),
        )
    except ValueError as exc:
        if str(exc) != "OPENAI_API_KEY not set":
            raise
        print(json.dumps({"primitive": "decompose_jd", "status": "failed", "error": str(exc)}))
        raise SystemExit(1) from exc

    out.write_text(json.dumps(seeds, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "decompose_jd", "status": "completed", "seeds": len(seeds),
                      "location": approved_location or "",
                      "model": args.model,
                      "reasoning_effort": args.reasoning_effort,
                      "system_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
                      "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
