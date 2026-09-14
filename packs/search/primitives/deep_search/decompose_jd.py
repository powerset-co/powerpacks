"""Generate the reviewed Pond-1 query from a JD in one model call.

Emits one literal high-recall candidate-population query, including the JD's
required locations, using the general pond prompt and at most one retrieved move
card. Downstream retrieval uses the query verbatim as its semantic input and the
ordinary expansion primitive derives structured traits and geography.

Output: queries.json = [{"key": "q00", "query": "..."}]; queries.raw.json keeps
the parsed model response plus the injected precedent cards.

Changelog:
  2026-09-09  Generate the complete query from the JD without a recruiter plan.
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
    from pond_prompts import load_pond_prompt
    import precedents
except ImportError:  # pragma: no cover - package execution
    from .pond_prompts import load_pond_prompt
    from . import precedents

DEFAULT_MODEL = os.environ.get("RECRUIT_DECOMPOSE_MODEL", "gpt-4o")
DEFAULT_REASONING_EFFORT = os.environ.get("RECRUIT_DECOMPOSE_REASONING_EFFORT")

SYSTEM = load_pond_prompt({"pond_prompt_family": "general"}, "pond-1")


def build_messages(
    jd: str,
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
            f"{precedent_context}"
        )},
    ]


def retrieve_precedent_cards(jd: str) -> list[dict[str, Any]]:
    """The single best move card for this JD, chain cut to its first link."""
    cards = precedents.retrieve_jd_precedents(jd, {}, collection="pond", limit=1)
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
    *, jd: str, model: str,
    reasoning_effort: str | None, system_prompt: str,
    service_tier: str | None = None,
    precedent_cards: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": build_messages(jd, system_prompt, precedent_cards=precedent_cards),
        "response_format": {"type": "json_object"},
    }
    normalized_model = str(model or "").lower().split("/")[-1]
    if reasoning_effort and normalized_model.startswith(("gpt-5", "o1", "o3", "o4")):
        request["reasoning_effort"] = reasoning_effort
    if service_tier:
        request["service_tier"] = service_tier
    return request


def generate_queries(
    *, jd: str, model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    system_prompt: str | None = None,
    api_key: str | None = None, client: Any | None = None,
    raw_response_path: Path | None = None,
    on_response: Callable[[Any], None] | None = None,
    service_tier: str | None = None,
    use_precedents: bool = True,
) -> list[dict[str, Any]]:
    """Run the Pond-1 query request and return exactly one located seed."""
    if raw_response_path is not None and raw_response_path.is_file():
        parsed = json.loads(raw_response_path.read_text(encoding="utf-8"))
    else:
        if client is None:
            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ValueError("OPENAI_API_KEY not set")
            client = make_openai_client(key)
        prompt = system_prompt or SYSTEM
        precedent_cards = retrieve_precedent_cards(jd) if use_precedents else []
        response = client.chat.completions.create(**query_request(
            jd=jd,
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
    return seeds


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the reviewed Pond-1 query from a JD (1 LLM call).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--jd", help="JD text")
    g.add_argument("--jd-file", help="Path to a file containing the JD text")
    ap.add_argument("--system-file", default=None,
                    help="Use this reviewed system prompt instead of the general pond-1 prompt")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                    help="Reasoning effort for supported query-generation models")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--out", help="Where to write queries.json")
    args = ap.parse_args()

    if bool(args.jd) == bool(args.jd_file):
        ap.error("provide exactly one of --jd or --jd-file")
    if not args.out:
        ap.error("--out is required")

    jd = Path(args.jd_file).read_text(encoding="utf-8") if args.jd_file else args.jd
    system_prompt = (Path(args.system_file).read_text(encoding="utf-8")
                     if args.system_file else SYSTEM)
    if not system_prompt.strip():
        ap.error("system prompt must not be empty")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        seeds = generate_queries(
            jd=jd,
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
                      "model": args.model,
                      "reasoning_effort": args.reasoning_effort,
                      "system_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
                      "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
