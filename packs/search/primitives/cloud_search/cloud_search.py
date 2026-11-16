#!/usr/bin/env python3
"""Search a Powerset set through the Powerset API, the same search the Powerset web app runs.

prepare: POST /v2/search/expand turns the query into the API's filters; the card shows them under
its own keys (card_filters). run: the card's filters go back to the API's (api_request), POST
/v2/search/execute returns the set's people, and POST /v2/hydrate their profiles. The local LLM
filter and rerank then read those profiles like any other run's. No Postgres, no TurboPuffer.

Changelog:
  2026-11-16: created; replaces the direct TurboPuffer/Postgres set search (resolve_set_operators,
      resolve_companies/education/investors, apply_prefilters, execute_role_search, Postgres hydration).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.shared import powerset_api  # noqa: E402

# The API's names for what the card calls otherwise; every other key is the same on both.
CARD_KEYS = {"role_semantic_query": "semantic_query", "is_current": "is_current_role",
             "company_ids": "company_names", "investors": "investor_names", "education_ids": "education_names"}
API_KEYS = {card: api for api, card in CARD_KEYS.items()}
# A name the expansion did not resolve is looked up here (GET /v2/autocomplete/<kind>?q=).
LOOKUPS = {"company_ids": "companies", "education_ids": "schools"}
# Card-only keys with no API field.
CARD_ONLY = {"bm25_queries", "set_id", "is_current_company"}
LIMIT = 2000  # /v2/search/execute caps a page at 2000


def _label(item: Any) -> Any:
    return (item.get("display_value") or item.get("id")) if isinstance(item, dict) else item


def card_filters(api_filters: dict[str, Any]) -> dict[str, Any]:
    """The API's filters as the card shows them: entities by name, keywords from the role patterns."""
    out: dict[str, Any] = {}
    for key, value in api_filters.items():
        if value in (None, "", [], {}):
            continue
        out[CARD_KEYS.get(key, key)] = [_label(item) for item in value] if isinstance(value, list) and key not in ("role_core_patterns", "role_adjacent_patterns") else value
    keywords = [example for pattern in api_filters.get("role_core_patterns") or [] for example in pattern.get("examples") or []]
    if keywords:
        out["bm25_queries"] = list(dict.fromkeys(keywords))
    return out


def expand(env_file: Path | None, query: str, set_id: str | None) -> dict[str, Any]:
    """The prepared payload for a query: the card's filters, the API's as expanded, and the traits."""
    out = powerset_api.call(env_file, "/v2/search/expand", {"query": query, "set_id": set_id})
    api_filters = out.get("filters") or {}
    return {"normalized_query": query, "role_search_filters": card_filters(api_filters), "api_filters": api_filters,
            "traits": out.get("traits") or [], "has_domain_intent": bool(out.get("has_domain_intent"))}


def _resolve(env_file: Path | None, key: str, names: list[Any], expanded: list[Any]) -> list[str]:
    known = {str(_label(item)).lower(): str(item.get("id") or _label(item)) for item in expanded if isinstance(item, dict)}
    ids = []
    for name in names:
        if str(name).lower() in known:
            ids.append(known[str(name).lower()])
        elif key in LOOKUPS:
            hits = powerset_api.call(env_file, f"/v2/autocomplete/{LOOKUPS[key]}?limit=1&q={powerset_api.quote(str(name))}").get("suggestions") or []
            ids += [str(hit["id"]) for hit in hits[:1] if hit.get("id")]
        else:
            ids.append(str(name))
    return ids


def api_request(env_file: Path | None, payload: dict[str, Any], *, query: str, set_id: str | None, limit: int) -> dict[str, Any]:
    """The card's filters as a /v2/search/execute request; names map back to the expansion's ids."""
    expanded = payload.get("api_filters") or {}
    request: dict[str, Any] = {}
    for card_key, value in (payload.get("role_search_filters") or {}).items():
        if card_key in CARD_ONLY or value in (None, "", [], {}):
            continue
        key = API_KEYS.get(card_key, card_key)
        is_entities = isinstance(expanded.get(key), list) or key in LOOKUPS
        request[key] = _resolve(env_file, key, value, expanded.get(key) or []) if is_entities and isinstance(value, list) and key not in ("role_core_patterns", "role_adjacent_patterns") else value
    if request.get("role_semantic_query") and not request.get("role_core_patterns"):
        request.pop("role_semantic_query")  # the API takes a role query only with its patterns
    return {**request, "query": query[:500], "traits": payload.get("traits") or [],
            "has_domain_intent": bool(payload.get("has_domain_intent")), "set_id": set_id,
            "limit": min(limit or LIMIT, LIMIT), "rerank": False, "skip_llm_filter": True}


def record(state_path: Path, step_id: str, output: dict[str, Any], elapsed_ms: int) -> None:
    state = json.loads(state_path.read_text())
    now = datetime.now(timezone.utc).isoformat()
    state.setdefault("steps", []).append({"id": step_id, "status": "completed", "recorded_at": now,
                                          "elapsed_ms": elapsed_ms, "output": output})
    state["updated_at"] = now
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    state_path = Path(args.state)
    state = json.loads(state_path.read_text())
    payload = next((step.get("output") or {} for step in reversed(state.get("steps", []))
                    if step.get("id") == "expand_search_request"), {})
    env_file = Path(args.env_file) if args.env_file else None
    set_id = args.set_id or (payload.get("role_search_filters") or {}).get("set_id") or powerset_api.personal_set_id(env_file)
    request = api_request(env_file, payload, query=str(state.get("query") or ""), set_id=set_id, limit=args.limit)
    response = powerset_api.call(env_file, "/v2/search/execute", request, timeout=args.timeout)
    candidates = [{"person_id": row["person_id"], "score": row.get("final_score"),
                   "vertical_sources": row.get("vertical_sources") or [], "tags": row.get("tags") or []}
                  for row in response.get("results") or [] if row.get("status") != "llm_filtered"]
    if args.limit:  # the API can return more than it was asked for (its maybe and hail-mary buckets)
        candidates = candidates[:args.limit]
    output = {"set_id": set_id, "backend": "powerset_api", "total_count": response.get("total_count"),
              "candidate_count": len(candidates), "candidate_ids": [row["person_id"] for row in candidates],
              "candidates": candidates, "request": request}
    if args.write_state:
        record(state_path, "execute_role_search", output, int((time.time() - started) * 1000))
    return {key: value for key, value in output.items() if key not in ("candidates", "candidate_ids")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--set-id")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--write-state", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
