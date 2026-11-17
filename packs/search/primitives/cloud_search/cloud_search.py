#!/usr/bin/env python3
"""Search a Powerset set through the Powerset API, the same search the Powerset web app runs.

prepare: POST /v2/search/expand turns the query into the API's filters; the card shows them under
its own keys (card_filters). run: the card's filters go back to the API's (api_request) and POST
/v2/search/execute searches the set. With --rerank the API also filters and ranks the people and
saves them to a conversation, and this downloads them back from GET /v2/conversations/{id}/results,
500 a request, gzipped on the wire. The good ones (score 0.3 and up with no zero trait, the API's
own split) are the run's ranking, so nothing is scored on this Mac. They arrive in two steps so the
page has people to show at once: --rerank records the good ones among the 50 the search answers
with, and --rerank-rest replaces them with all of them once the API has saved the results. The bad
ones follow in a background process (--bad-for), page by page into bad_results.jsonl.gz; the
results page reads that file only when asked for them, and shows them loading until it exists.
Without --rerank (a job-description run) the people come back unranked for the local screen. No
Postgres, no TurboPuffer.

Changelog:
  2026-11-17: --rerank: the API filters, ranks and stores a set search and the app downloads the
      result back in pages, good and bad apart; the app no longer filters or ranks a set search.
  2026-11-16: created; replaces the direct TurboPuffer/Postgres set search (resolve_set_operators,
      resolve_companies/education/investors, apply_prefilters, execute_role_search, Postgres hydration).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
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
PAGE = 500  # GET /v2/conversations/{id}/results caps a page at 500
GOOD_SCORE = 0.3  # the API's line between good and bad (with no zero trait)


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


def api_request(env_file: Path | None, payload: dict[str, Any], *, query: str, set_id: str | None, limit: int,
                conversation_id: str | None = None) -> dict[str, Any]:
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
            "limit": min(limit or LIMIT, LIMIT), "rerank": bool(conversation_id), "skip_llm_filter": not conversation_id,
            "conversation_id": conversation_id}


def record(state_path: Path, step_id: str, output: dict[str, Any], elapsed_ms: int) -> None:
    state = json.loads(state_path.read_text())
    now = datetime.now(timezone.utc).isoformat()
    state.setdefault("steps", []).append({"id": step_id, "status": "completed", "recorded_at": now,
                                          "elapsed_ms": elapsed_ms, "output": output})
    state["updated_at"] = now
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


RANKED_FIELDS = ("person_id", "result_index", "matched_position_indexes", "final_score", "trait_scores",
                 "overall_reasoning", "vertical_sources")


def write_ranking(path: Path, rows: list[dict[str, Any]]) -> None:
    """The API's ranking in the columns the local rerank writes (query_results.csv)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RANKED_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row.get(key), sort_keys=True) if isinstance(row.get(key), (dict, list)) else row.get(key)
                             for key in RANKED_FIELDS})


def wait_saved(env_file: Path | None, conversation_id: str, expected: int, *, timeout: int = 120, pause: float = 2.0) -> None:
    """Return once the conversation holds the search's results: the API saves them, 50 at a time,
    after it answers. Saved means the count it answered with, or (it drops duplicates) a count that
    has not grown over three checks."""
    seen, still, deadline = -1, 0, time.time() + timeout
    while expected:
        page = powerset_api.call(env_file, f"/v2/conversations/{conversation_id}/results?limit=1")
        count = int(page.get("good_count") or 0) + int(page.get("bad_count") or 0)
        still = still + 1 if count and count == seen else 0
        if count >= expected or still == 3:
            return
        if time.time() > deadline:
            raise RuntimeError(f"the Powerset API saved {count} of {expected} results in {timeout}s; run the search again")
        seen = count
        time.sleep(pause)


def saved_results(env_file: Path | None, conversation_id: str, kind: str):
    """The conversation's good or bad results, best first, a page of 500 at a time."""
    offset = 0
    while True:
        page = powerset_api.call(env_file, f"/v2/conversations/{conversation_id}/results?filter={kind}&limit={PAGE}&offset={offset}")
        yield page.get("results") or []
        if not page.get("has_more"):
            return
        offset += PAGE


def download_bad(env_file: Path | None, conversation_id: str, out: Path) -> int:
    """The conversation's bad results into `out`, a page at a time; the file appears only when whole."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part, count = out.with_name(out.name + ".part"), 0
    with gzip.open(part, "wt") as handle:
        for page in saved_results(env_file, conversation_id, "bad"):
            handle.writelines(json.dumps(row) + "\n" for row in page)
            count += len(page)
    os.replace(part, out)
    return count


def _record_ranked(args: argparse.Namespace, state_path: Path, good: list[dict[str, Any]], ranking: Path,
                   output: dict[str, Any], started: float) -> dict[str, Any]:
    """Write the ranking and record the search and rank steps over `good`."""
    write_ranking(ranking, good)
    ids = [row["person_id"] for row in good]
    output = {**output, "backend": "powerset_api", "candidate_count": len(ids), "candidate_ids": ids,
              "candidates": [{"person_id": row["person_id"], "score": row.get("final_score"),
                              "vertical_sources": row.get("vertical_sources") or []} for row in good]}
    if args.write_state:
        elapsed_ms = int((time.time() - started) * 1000)
        record(state_path, "execute_role_search", output, elapsed_ms)
        record(state_path, "llm_rerank_candidates", {
            "model": "powerset_api", "conversation_id": output["conversation_id"], "ranked_count": len(ids),
            "ranked_candidate_ids": ids, "artifacts": {"query_results_csv": str(ranking)}}, elapsed_ms)
    return {key: value for key, value in output.items() if key not in ("candidates", "candidate_ids")}


def _out_dir(state_path: Path, state: dict[str, Any]) -> Path:
    return state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem) / "cloud_search"


def run_ranked(args: argparse.Namespace, state_path: Path, state: dict[str, Any], env_file: Path | None,
               payload: dict[str, Any], set_id: str | None, started: float) -> dict[str, Any]:
    """The API searches, filters, ranks and saves. Its answer carries the best 50, so the good ones
    among them are this run's first ranking at once; --rerank-rest brings the rest."""
    query = str(state.get("query") or "")
    conversation_id = str(powerset_api.call(env_file, "/v2/conversations", {"title": query[:200] or "Search", "set_id": set_id})["id"])
    request = api_request(env_file, payload, query=query, set_id=set_id, limit=args.limit, conversation_id=conversation_id)
    response = powerset_api.call(env_file, "/v2/search/execute", request, timeout=args.timeout)
    first = [row for row in response.get("results") or []
             if row.get("status") == "good" and float(row.get("final_score") or 0) >= GOOD_SCORE]
    return _record_ranked(args, state_path, first, _out_dir(state_path, state) / "query_results.csv", {
        "set_id": set_id, "conversation_id": conversation_id, "total_count": response.get("total_count"),
        "good_count": int(response.get("good_count") or 0), "bad_count": int(response.get("bad_count") or 0),
        "partial": True, "request": request}, started)


def run_ranked_rest(args: argparse.Namespace, state_path: Path, state: dict[str, Any], env_file: Path | None,
                    started: float) -> dict[str, Any]:
    """Every good result of the search --rerank started, in pages, once the API has saved them; the
    bad ones follow in a background process, since the search is done without them."""
    searched = next(step["output"] for step in reversed(state["steps"]) if step.get("id") == "execute_role_search")
    conversation_id, bad_count = searched["conversation_id"], int(searched.get("bad_count") or 0)
    wait_saved(env_file, conversation_id, int(searched.get("good_count") or 0) + bad_count)
    good = [row for page in saved_results(env_file, conversation_id, "good") for row in page]
    if args.limit:
        good = good[:args.limit]
    out_dir = _out_dir(state_path, state)
    bad_path = (out_dir / "bad_results.jsonl.gz").resolve()
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--bad-for", conversation_id, "--out", str(bad_path),
                      "--env-file", str(env_file) if env_file else ""],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return _record_ranked(args, state_path, good, out_dir / "query_results.csv", {
        **{key: searched.get(key) for key in ("set_id", "conversation_id", "total_count", "request")},
        "good_count": len(good), "bad_count": bad_count, "bad_results_path": str(bad_path), "partial": False}, started)


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    state_path = Path(args.state)
    state = json.loads(state_path.read_text())
    payload = next((step.get("output") or {} for step in reversed(state.get("steps", []))
                    if step.get("id") == "expand_search_request"), {})
    env_file = Path(args.env_file) if args.env_file else None
    set_id = args.set_id or (payload.get("role_search_filters") or {}).get("set_id") or powerset_api.personal_set_id(env_file)
    if args.rerank_rest:
        return run_ranked_rest(args, state_path, state, env_file, started)
    if args.rerank:
        return run_ranked(args, state_path, state, env_file, payload, set_id, started)
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
    parser.add_argument("--state")
    parser.add_argument("--bad-for", metavar="CONVERSATION_ID", help="Only download this conversation's bad results into --out")
    parser.add_argument("--out")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--set-id")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--rerank", action="store_true", help="The API filters, ranks and stores the results in a conversation")
    parser.add_argument("--rerank-rest", action="store_true", help="Download the rest of the results --rerank started")
    args = parser.parse_args(argv)
    if args.bad_for:
        print(json.dumps({"bad_count": download_bad(Path(args.env_file) if args.env_file else None, args.bad_for, Path(args.out))}))
        return 0
    if not args.state:
        parser.error("--state is required")
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
