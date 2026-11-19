#!/usr/bin/env python3
"""Search a Powerset set through the Powerset API, the same search the Powerset web app runs.

prepare: the app expands the query, then POST /v2/search/resolve turns its names into ids and
returns the set's title groups. run: api_request builds the execute body from the edited card,
resolving added names together through /v2/search/resolve; POST /v2/search/execute searches the set.
With --rerank the API also filters and ranks the people and
saves them to a conversation, and this downloads them back from GET /v2/conversations/{id}/results,
500 a request, gzipped on the wire, into gzip files of the API's rows: good_results.jsonl.gz (score
0.3 and up with no zero trait, the API's own split) and bad/bad_results.jsonl.gz. Those files are
the only source of a ranked run's people: each row carries the score and the whole profile, so the
search's own answer and /v2/hydrate are not read, and good and bad go through the same functions
into the same artifacts. Nothing is scored on this Mac.

The search asks the API to answer only once every result is saved (save_before_response), so
nothing here waits on a clock or polls a count. The good ones arrive in two steps so the page has
people at once: --rerank downloads the best 500 and --rerank-rest replaces the file with all of them. The bad ones follow in a background process (--bad-for); the results page
reads them only when asked, and shows them loading until their rows file exists. Without --rerank
(a job-description run) the people come back unranked for the local screen. No Postgres, no
TurboPuffer.

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
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.shared import powerset_api  # noqa: E402

LIMIT = 2000  # /v2/search/execute caps a page at 2000
ENTITY_KEYS = {"company_names": "company_ids", "education_names": "education_ids", "investor_names": "investors"}
RESOLVE_KEYS = {**ENTITY_KEYS,
                **{key: key for key in ("cities", "states", "countries", "metro_areas", "macro_regions")}}
LIST_FILTERS = (
    "role_ids",
    "company_cities", "company_states", "company_countries", "company_metro_areas", "company_macro_regions",
    "yc_batches", "entity_types", "sector_types", "seniority_bands", "role_tracks", "degree_levels",
    "fields_of_study", "company_semantic_queries", "company_adjacency_queries",
)
SCALAR_FILTERS = (
    "funding_stage_min", "funding_stage_max", "funding_amount_min", "funding_amount_max",
    "headcount_min", "headcount_max", "last_funding_before", "last_funding_after",
    "founded_year_min", "founded_year_max", "valuation_min", "valuation_max",
    "graduation_year_min", "graduation_year_max", "years_experience_min", "years_experience_max",
    "age_min", "age_max", "role_function", "x_followers_min", "x_followers_max",
    "li_followers_min", "li_followers_max", "ig_followers_min", "ig_followers_max",
    "li_connections_min", "li_connections_max", "operator_interaction_min", "operator_interaction_max",
    "set_interaction_min", "set_interaction_max", "position_after_date", "position_before_date",
)
PAGE = 500  # GET /v2/conversations/{id}/results caps a page at 500
GOOD_SCORE = 0.3  # the API's line between good and bad (with no zero trait)


def lookup_person(env_file: Path | None, set_id: str, name: str) -> list[dict[str, Any]]:
    """Read the set's contacts once and keep only names containing every requested word."""
    response = powerset_api.call(
        env_file, f"/v2/set-contacts/{powerset_api.quote(set_id)}?search={powerset_api.quote(name)}&page_size=200",
    )
    words = name.casefold().split()
    profiles = []
    for lead in response["leads"]:
        if not all(word in lead["name"].casefold() for word in words):
            continue
        operators = lead.get("operators") or []
        profiles.append({
            **lead,
            "headline": lead.get("title"),
            "source_operators": [operator["operator_name"] for operator in operators],
            "source_channels": sorted({channel for operator in operators for channel in operator["source_channels"]}),
        })
    return profiles


def _label(item: Any) -> Any:
    return (item.get("display_value") or item.get("id")) if isinstance(item, dict) else item


def resolve(env_file: Path | None, payload: dict[str, Any], set_id: str | None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve the app's names, keeping the returned ids and displaying their labels on the card."""
    filters = dict(payload.get("role_search_filters") or {})
    request = {key: filters.get(key) or [] for key in RESOLVE_KEYS}
    request.update(role_ids=filters.get("role_agent_ids") or filters.get("role_ids") or [], set_id=set_id)
    resolved = powerset_api.call(env_file, "/v2/search/resolve", request)
    api_filters = {key: resolved.get(key) or [] for key in RESOLVE_KEYS.values()}
    notes = list(payload.get("notes") or [])
    for card_key, api_key in RESOLVE_KEYS.items():
        names = list(dict.fromkeys(_label(item) for item in api_filters[api_key]))
        returned = {name.casefold() for name in names}
        missing = [name for name in request[card_key] if not any(label.startswith(name.casefold()) for label in returned)]
        if missing:
            notes.append(f"resolve did not return {card_key}: {', '.join(missing)}")
        filters.pop(card_key, None)
        if names:
            filters[card_key] = names
    return ({**payload, "role_search_filters": filters, "api_filters": api_filters, "notes": notes},
            resolved.get("title_clusters") or [], resolved.get("adjacent_title_clusters") or [])


def _resolved_ids(env_file: Path | None, filters: dict[str, Any], resolved: dict[str, Any],
                set_id: str | None) -> dict[str, list[str]]:
    ids: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}
    for card_key, api_key in RESOLVE_KEYS.items():
        records = resolved.get(api_key) or []
        known = {str(_label(item)).casefold() for item in records}
        names = filters.get(card_key) or []
        selected = {name.casefold() for name in names}
        ids[api_key] = [item["id"] for item in records if str(_label(item)).casefold() in selected]
        missing = list(dict.fromkeys(name for name in names if name.casefold() not in known))
        if missing and card_key in ENTITY_KEYS:
            added[card_key] = missing
        elif missing:
            ids[api_key].extend(missing)
    if added:
        response = powerset_api.call(env_file, "/v2/search/resolve", {**added, "set_id": set_id})
        for card_key in added:
            api_key = ENTITY_KEYS[card_key]
            ids[api_key].extend(item["id"] for item in response.get(api_key) or [])
    return {key: list(dict.fromkeys(values)) for key, values in ids.items()}


def api_request(env_file: Path | None, payload: dict[str, Any], *, query: str, set_id: str | None, limit: int,
                conversation_id: str | None = None) -> dict[str, Any]:
    """The edited card as /v2/search/execute fields, with every selected resolved id."""
    filters = payload.get("role_search_filters") or {}
    request: dict[str, Any] = _resolved_ids(env_file, filters, payload.get("api_filters") or {}, set_id)
    for key in LIST_FILTERS:
        request[key] = [item["id"] if isinstance(item, dict) else item for item in filters.get(key) or []]
    for key in SCALAR_FILTERS:
        if filters.get(key) is not None:
            request[key] = filters[key]
    for key in ("role_core_patterns", "role_adjacent_patterns"):
        request[key] = [{"regex": pattern["regex"], "examples": pattern.get("examples") or []}
                        for pattern in filters.get(key) or []]
    request.update(role_semantic_query=filters.get("semantic_query", ""),
                   role_bm25_queries=filters.get("bm25_queries") or [],
                   education_op=filters.get("education_op") or "or")
    if request.get("role_semantic_query") and not request.get("role_core_patterns"):
        request.pop("role_semantic_query")  # the API takes a role query only with its patterns
    traits = []
    for item in payload.get("traits") or []:
        trait = {"value": item} if isinstance(item, str) else item
        traits.append({"value": trait["value"], "temporal": trait.get("temporal") or "all",
                       "meaning": trait.get("meaning") or "general"})
    domain = bool(payload.get("has_domain_intent"))
    return {**request, "query": query, "traits": traits,
            "has_domain_intent": domain, "search_summary": True, "search_company_signal": domain, "set_id": set_id,
            "limit": min(limit or LIMIT, LIMIT), "rerank": bool(conversation_id), "skip_llm_filter": not conversation_id,
            # A ranked search is answered only once every result is saved, so there is no saving to wait for.
            "conversation_id": conversation_id, "save_before_response": bool(conversation_id)}


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


def get(env_file: Path | None, path: str, *, tries: int = 3) -> dict[str, Any]:
    """One read of the API, tried again when the network or the server fails (never on a 4xx)."""
    for attempt in range(1, tries + 1):
        try:
            return powerset_api.call(env_file, path)
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == tries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, EOFError, json.JSONDecodeError):
            if attempt == tries:
                raise
        time.sleep(attempt)
    raise AssertionError("unreachable")


def download(env_file: Path | None, conversation_id: str, kind: str, out: Path, *, first_page: bool = False,
             tries: int = 3) -> int:
    """The conversation's good or bad results into the gzip `out`, best first, 500 a request, one
    JSON row a line exactly as the API returned it. Each person is written once. The file appears
    only when whole (written beside itself, then renamed), and a whole download holds as many
    people as the API counts, else it is downloaded again, so a short or doubled file is never
    left behind. first_page stops after the best 500."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    for attempt in range(1, tries + 1):
        seen: set[str] = set()
        expected, offset = 0, 0
        with gzip.open(part, "wt", encoding="utf-8") as handle:
            while True:
                page = get(env_file, f"/v2/conversations/{conversation_id}/results?filter={kind}&limit={PAGE}&offset={offset}")
                expected = int(page.get(f"{kind}_count") or 0)
                for row in page.get("results") or []:
                    person_id = str(row.get("person_id") or "")
                    if person_id and person_id not in seen:
                        seen.add(person_id)
                        handle.write(json.dumps(row) + "\n")
                if first_page or not page.get("has_more"):
                    break
                offset += PAGE
        if first_page or len(seen) == expected:
            os.replace(part, out)
            return len(seen)
    part.unlink(missing_ok=True)
    raise RuntimeError(f"downloaded {len(seen)} of the {expected} {kind} results the Powerset API counts; run the search again")


def read_results(path: Path) -> list[dict[str, Any]]:
    """A downloaded results file's rows, best first."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def person_rows(path: Path) -> list[dict[str, Any]]:
    """A downloaded results file's people shaped like persons rows (what hydrate_people normalizes):
    each API row carries the person's whole profile, so nothing else is fetched for them."""
    return [{**row, "id": row["person_id"], "hydrated_context": row} for row in read_results(path)]


def _out_dir(state_path: Path, state: dict[str, Any]) -> Path:
    return (state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem) / "cloud_search").resolve()


def _record_ranked(args: argparse.Namespace, state_path: Path, results_path: Path, output: dict[str, Any],
                   started: float) -> dict[str, Any]:
    """Record the search and rank steps over the downloaded good results: the file is the only
    thing read, here and by every later step."""
    good = read_results(results_path)
    if args.limit:
        good = good[:args.limit]
    ranking = results_path.with_name("query_results.csv")
    write_ranking(ranking, good)
    ids = [row["person_id"] for row in good]
    output = {**output, "backend": "powerset_api", "results_path": str(results_path), "candidate_count": len(ids),
              "candidate_ids": ids,
              "candidates": [{"person_id": row["person_id"], "score": row.get("final_score"),
                              "vertical_sources": row.get("vertical_sources") or []} for row in good]}
    if args.write_state:
        elapsed_ms = int((time.time() - started) * 1000)
        record(state_path, "execute_role_search", output, elapsed_ms)
        record(state_path, "llm_rerank_candidates", {
            "model": "powerset_api", "conversation_id": output["conversation_id"], "ranked_count": len(ids),
            "ranked_candidate_ids": ids, "artifacts": {"query_results_csv": str(ranking)}}, elapsed_ms)
    return {key: value for key, value in output.items() if key not in ("candidates", "candidate_ids")}


def run_ranked(args: argparse.Namespace, state_path: Path, state: dict[str, Any], env_file: Path | None,
               payload: dict[str, Any], set_id: str | None, started: float) -> dict[str, Any]:
    """The API searches, filters, ranks and saves, and answers only once every result is saved;
    the best 500 good results are then downloaded at once, so the page has people to show.
    --rerank-rest brings the rest. The people in the search's own answer are not used: every row
    comes from the download."""
    query = str(state.get("query") or "")
    conversation_id = str(powerset_api.call(env_file, "/v2/conversations", {"title": query[:200] or "Search", "set_id": set_id})["id"])
    request = api_request(env_file, payload, query=query, set_id=set_id, limit=args.limit, conversation_id=conversation_id)
    response = powerset_api.call(env_file, "/v2/search/execute", request, timeout=args.timeout)
    if not response.get("results_saved"):
        raise RuntimeError("the Powerset API answered before saving the results (it does not know save_before_response yet)")
    good_count, bad_count = int(response.get("good_count") or 0), int(response.get("bad_count") or 0)
    results_path = _out_dir(state_path, state) / "good_results.jsonl.gz"
    download(env_file, conversation_id, "good", results_path, first_page=True)
    return _record_ranked(args, state_path, results_path, {
        "set_id": set_id, "conversation_id": conversation_id, "total_count": response.get("total_count"),
        "good_count": good_count, "bad_count": bad_count, "partial": True, "request": request}, started)


def run_ranked_rest(args: argparse.Namespace, state_path: Path, state: dict[str, Any], env_file: Path | None,
                    started: float) -> dict[str, Any]:
    """Every good result of the search --rerank started; the bad ones follow in a background
    process, since the search is done without them."""
    searched = next(step["output"] for step in reversed(state["steps"]) if step.get("id") == "execute_role_search")
    conversation_id, bad_count = searched["conversation_id"], int(searched.get("bad_count") or 0)
    out_dir = _out_dir(state_path, state)
    good_count = download(env_file, conversation_id, "good", out_dir / "good_results.jsonl.gz")
    bad_dir = out_dir / "bad"
    worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--bad-for", conversation_id, "--out", str(bad_dir),
                               "--state", str(state_path), "--env-file", str(env_file) if env_file else ""],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / BAD_PID).write_text(str(worker.pid))  # the page tells a download still running from one that died
    return _record_ranked(args, state_path, out_dir / "good_results.jsonl.gz", {
        **{key: searched.get(key) for key in ("set_id", "conversation_id", "total_count", "request")},
        "good_count": good_count, "bad_count": bad_count, "partial": False,
        "bad_results": {"jsonl": str(bad_dir / BAD_ROWS), "profiles_path": str(bad_dir / "profiles.jsonl.gz"),
                        "error": str(bad_dir / BAD_ERROR), "pid": str(bad_dir / BAD_PID), "count": bad_count}}, started)


BAD_ROWS, BAD_ERROR, BAD_PID = "bad_results.jsonl", "error.txt", "pid"


def export_bad(env_file: Path | None, conversation_id: str, out_dir: Path, state: dict[str, Any]) -> int:
    """The bad results as the very artifacts a run's good results become: downloaded to a gzip like
    them, each profile normalized by the same function hydrate_people uses, and the rows built by
    the same function the export uses. The rows file appears last, and only when whole."""
    from packs.search.primitives.hydrate_people.hydrate_people import normalize_hydrated_context
    from packs.search.primitives.persist_search_results import results_io

    results_path = out_dir / "bad_results.jsonl.gz"
    (out_dir / BAD_ERROR).unlink(missing_ok=True)  # an earlier try's failure
    download(env_file, conversation_id, "bad", results_path)
    bad = read_results(results_path)
    ranking, profiles = out_dir / "query_results.csv", out_dir / "profiles.jsonl.gz"
    write_ranking(ranking, bad)
    with gzip.open(profiles, "wt", encoding="utf-8") as handle:
        handle.writelines(json.dumps(normalize_hydrated_context(row)) + "\n" for row in person_rows(results_path))
    rows = results_io.result_rows({"task_id": state.get("task_id"), "query": state.get("query"), "steps": [
        {"id": "hydrate_people", "output": {"profiles_path": str(profiles)}},
        {"id": "llm_rerank_candidates", "output": {"ranked_candidate_ids": [row["person_id"] for row in bad],
                                                   "artifacts": {"query_results_csv": str(ranking)}}}]})
    part = out_dir / (BAD_ROWS + ".part")
    results_io.write_jsonl(part, rows)
    os.replace(part, out_dir / BAD_ROWS)
    return len(rows)


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
    parser.add_argument("--bad-for", metavar="CONVERSATION_ID", help="Only download this conversation's bad results into the folder --out")
    parser.add_argument("--out")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--set-id")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--rerank", action="store_true", help="The API filters, ranks and stores the results in a conversation")
    parser.add_argument("--rerank-rest", action="store_true", help="Download the rest of the results --rerank started")
    args = parser.parse_args(argv)
    if args.bad_for:  # the background half of --rerank-rest; a failure is left where the page finds it
        out_dir = Path(args.out)
        try:
            count = export_bad(Path(args.env_file) if args.env_file else None, args.bad_for, out_dir,
                               json.loads(Path(args.state).read_text()) if args.state else {})
        except (Exception, SystemExit) as error:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / BAD_ERROR).write_text(f"{type(error).__name__}: {error}")
            return 1
        print(json.dumps({"bad_count": count}))
        return 0
    if not args.state:
        parser.error("--state is required")
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
