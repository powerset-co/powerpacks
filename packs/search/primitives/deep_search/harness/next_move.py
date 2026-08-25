"""One diagnosis and one next move per pond.

  decide -> build the anonymized pond context (aggregate counts, no identities)
         -> ask for {diagnosis, action, next_query, source, rationale}
         -> reject a duplicate query, an ungrounded source, a diagnosis that
            contradicts the human, or a stop the user did not ask for; retry once
         -> a second rejected proposal falls back to a deterministic move
         -> the accepted action sets the next status: a query action queues the
            next pond, ranking_fix reruns the same pond, anything else completes

Choice 3 is the human stop and never calls the model.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

try:  # direct script execution
    from precedents import retrieve_next_moves
    from harness.artifacts import _price_usage_log, _read_json, _response_usage
    from harness.plan_review import _source_occupation
    from harness.pond import LOCATION_FIELDS
    from harness.pond_stats import _pond_costs
    from harness.prompts import (
        NEXT_SEARCH_ACTIONS, NEXT_SEARCH_DIAGNOSES, NEXT_SEARCH_PROMPT, NEXT_SEARCH_QUERY_ACTIONS,
    )
    from harness.summary import _save
except ImportError:  # pragma: no cover - module execution
    from ..precedents import retrieve_next_moves
    from .artifacts import _price_usage_log, _read_json, _response_usage
    from .plan_review import _source_occupation
    from .pond import LOCATION_FIELDS
    from .pond_stats import _pond_costs
    from .prompts import (
        NEXT_SEARCH_ACTIONS, NEXT_SEARCH_DIAGNOSES, NEXT_SEARCH_PROMPT, NEXT_SEARCH_QUERY_ACTIONS,
    )
    from .summary import _save
from openai_client import make_openai_client


def _next_move_context(results: Mapping[str, Any], iteration: Mapping[str, Any],
                       diagnosis: str | None, note: str,
                       user_requested_another_round: bool = False) -> dict[str, Any]:
    stats = iteration["pool_stats"]
    iterations = results.get("iterations") or []
    used = {str(row["query"]).casefold() for row in iterations}
    remaining = [row for row in results.get("frozen_initial_queries") or []
                 if str(row.get("query") or "").casefold() not in used]
    return {
        "job": {"title": results["title"], "hiring_company": results["company"] or "unknown",
                "destination_context": None},
        "current_query": iteration["query"], "frozen_brief": results["brief"],
        "pond_chain": [
            {
                "pond_n": int(row.get("pond_n") or 0),
                "query": str(row.get("query") or ""),
                "reviewed_count": int((row.get("pool_stats") or {}).get("reviewed_count") or 0),
                "diagnosis": (diagnosis if row is iteration and diagnosis else row.get("diagnosis")),
                "action": (row.get("next_move") or {}).get("action"),
            }
            for row in iterations
        ],
        "candidate_populations": results.get("candidate_populations") or [],
        "network_floors": [
            row["label"] for row in (results.get("network_floors") or {}).get("floors") or []
        ],
        "comp_band": results.get("comp_band"),
        "frozen_initial_queries_remaining": remaining,
        "relaxation_order": [
            "prefer one change at a time, but geography and population may change together",
            "the network is predominantly US-based, so expect non-US local ponds to be thin",
            "for non-US roles, widen country to region to global early and consider relocation-plausible US candidates",
            "broaden to someone who could feasibly do the work or a feeder career when useful",
            "never relax the defining capability",
            "use corpus_sparse when the available network is the limit",
        ],
        "human_diagnosis": ({"category": diagnosis, "note": note or None}
                            if diagnosis else None),
        "user_requested_another_round": user_requested_another_round,
        "retrieved_precedents": retrieve_next_moves(
            title=str(results.get("title") or ""), brief=results.get("brief") or {},
            query=str(iteration.get("query") or ""), diagnosis=diagnosis or ""),
        "pool": {key: stats[key] for key in
                 ("result_count", "reviewed_count", "score_histogram", "level_mix", "geo_mix", "top_companies")},
        "anonymized_observations": [
            {"title": row.get("title") or "unknown", "company": row.get("company") or "unknown"}
            for row in iteration.get("shortlist_grades") or []
        ][:20],
    }


def _parse_next_move(raw: str) -> dict[str, Any]:
    proposal = json.loads(raw)
    if set(proposal) != {"diagnosis", "action", "next_query", "source", "rationale"}:
        raise ValueError("next move must contain diagnosis, action, next_query, source, and rationale")
    if str(proposal["diagnosis"]) not in NEXT_SEARCH_DIAGNOSES:
        raise ValueError("next move diagnosis is invalid")
    action = str(proposal["action"])
    if action not in NEXT_SEARCH_ACTIONS:
        raise ValueError("next move action is invalid")
    if action in NEXT_SEARCH_QUERY_ACTIONS:
        query = " ".join(str(proposal.get("next_query") or "").split())
        source = " ".join(str(proposal.get("source") or "").split())
        if len(query) < 10 or not source:
            raise ValueError("next search action needs a self-contained query and grounded source")
        proposal["next_query"] = query
        proposal["source"] = source
    elif proposal.get("next_query") is not None or proposal.get("source") is not None:
        raise ValueError("non-search next move must not contain a query or source")
    return proposal


def decide(*, run_dir: Path, choice: int | None = None, diagnosis: str | None = None,
           note: str = "", autonomous: bool = False, model: str = "gpt-5.6-luna",
           reasoning_effort: str = "medium", client: Any | None = None) -> Path:
    results = _read_json(run_dir / "results.json")
    status = results.get("status")
    user_continue = choice == 2
    if (status != "awaiting_diagnosis" and
            not (status == "awaiting_payload_review" and choice == 3) and
            not (status == "completed" and user_continue)):
        raise ValueError("search must await diagnosis")
    if autonomous == (choice is not None):
        raise ValueError("use either --autonomous or an interactive choice")
    if choice not in {None, 2, 3}:
        raise ValueError("interactive choice must be 2 or 3")
    iteration = results["iterations"][-1]
    if choice == 3:
        selected = str(diagnosis or "other")
        if selected not in NEXT_SEARCH_DIAGNOSES:
            raise ValueError("unknown diagnosis")
        iteration["diagnosis"] = selected
        iteration["human_override"] = {"choice": 3, "diagnosis": selected, "note": note}
        iteration["next_move"] = {"action": "stop", "next_query": None,
                                  "source": None, "rationale": note or "Human stopped the search."}
        iteration["proposal_delta"] = {
            "proposal": None,
            "actual": {"diagnosis": selected, "action": "stop", "next_query": None},
            "changed": True,
        }
        results["status"] = "completed"
        _save(results, run_dir)
        return run_dir / "results.json"
    selected = None if autonomous or diagnosis is None else str(diagnosis)
    if selected is not None and selected not in NEXT_SEARCH_DIAGNOSES:
        raise ValueError("unknown diagnosis")
    if not autonomous:
        if selected is not None:
            iteration["diagnosis"] = selected
        iteration["human_override"] = {"choice": 2, "diagnosis": selected, "note": note}
        if status == "completed":
            results["status"] = "awaiting_diagnosis"
        _save(results, run_dir)
    os.environ["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
    os.environ["POWERPACKS_USAGE_STAGE"] = f"search_harness.pond_{int(iteration['pond_n']):02d}.next_move"
    os.environ["OPENAI_SERVICE_TIER"] = "flex"
    client = client or make_openai_client(os.environ.get("OPENAI_API_KEY"))
    next_context = _next_move_context(
        results, iteration, selected, note,
        user_requested_another_round=user_continue,
    )
    messages = [{"role": "system", "content": NEXT_SEARCH_PROMPT},
                {"role": "user", "content": json.dumps(next_context, indent=2)}]
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model, reasoning_effort=reasoning_effort, service_tier="flex",
            messages=messages, response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        results["raw_model_responses"].append({
            "kind": "next_move", "pond_n": iteration["pond_n"], "attempt": attempt + 1,
            "raw": raw, "usage": _response_usage(response),
        })
        iteration["next_move_precedents"] = next_context["retrieved_precedents"]
        _save(results, run_dir)
        proposal = _parse_next_move(raw)
        proposed_query = " ".join(str(proposal.get("next_query") or "").split()).casefold()
        duplicate_query = (
            proposal["action"] in NEXT_SEARCH_QUERY_ACTIONS and
            any(" ".join(str(row["query"]).split()).casefold() == proposed_query
                for row in next_context["pond_chain"])
        )
        source_options = {"inferred"}
        source_options.update(
            str(row.get("population") or "").strip().casefold()
            for row in next_context.get("candidate_populations") or []
            if (isinstance(row, Mapping) and
                row.get("hint_kind") not in {"ranking-boost", "comp-band-anchor"})
        )
        source_options.update(
            str(row.get("source") or "").strip().casefold()
            for row in next_context.get("retrieved_precedents") or [] if isinstance(row, Mapping)
        )
        invalid_source = (
            proposal["action"] in NEXT_SEARCH_QUERY_ACTIONS and
            str(proposal.get("source") or "").casefold() not in source_options
        )
        conflicting_diagnosis = selected is not None and proposal["diagnosis"] != selected
        stopping_on_continue = (user_continue and
                                proposal["action"] in {"stop", "corpus_sparse"})
        if (not duplicate_query and not invalid_source and
                not conflicting_diagnosis and not stopping_on_continue):
            break
        if attempt == 0:
            rejection = (
                "Reject that move because the user explicitly requested another round. Return a "
                "non-stopping action; stop and corpus_sparse are not allowed."
                if stopping_on_continue else
                "Reject that next_query because it duplicates a query already in pond_chain. "
                "Return a query with different normalized full text."
                if duplicate_query else
                "Reject that source citation because it does not name an exact candidate-population "
                "phrase or retrieved precedent source. Return a grounded source, or inferred only when "
                "neither menu contains a credible pond."
                if invalid_source else
                f"Reject that move because the human selected diagnosis '{selected}'. Return that "
                "diagnosis exactly and choose an action that addresses it."
            )
            messages.extend([
                {"role": "assistant", "content": raw},
                {"role": "user", "content": rejection},
            ])
            continue
        if stopping_on_continue:
            current_query = str(iteration["query"])
            matches = list(re.finditer(r"\s+in\s+", current_query, flags=re.I))
            widened = (current_query[:matches[-1].start()].strip() if matches else
                       f"{current_query} globally")
            proposal = {
                "diagnosis": selected or proposal["diagnosis"],
                "action": "widen_geography", "next_query": widened,
                "source": _source_occupation(current_query) or "inferred",
                "rationale": ("The user requested another round; widened geography after two "
                              "stopping proposals."),
            }
        elif duplicate_query:
            filters = (iteration.get("input") or {}).get("filters") or {}
            bounded = any(filters.get(field) for field in LOCATION_FIELDS)
            matches = list(re.finditer(r"\s+in\s+", str(iteration["query"]), flags=re.I))
            widened = str(iteration["query"])[:matches[-1].start()].strip() if bounded and matches else ""
            proposal = {
                "diagnosis": selected or proposal["diagnosis"],
                "action": "widen_geography" if widened else "stop",
                "next_query": widened or None,
                "source": _source_occupation(iteration["query"]) if widened else None,
                "rationale": ("Both proposals duplicated a searched query; widened the current "
                              "pond's geography instead."
                              if widened else
                              "Both proposals duplicated a searched query and geography was already unbounded."),
            }
        else:
            proposal = {
                "diagnosis": selected or proposal["diagnosis"], "action": "stop", "next_query": None,
                "source": None,
                "rationale": ("Stopped for human review after two proposals used an ungrounded source."
                              if invalid_source else
                              "Stopped for human review after two proposals conflicted with the selected diagnosis."),
            }
    proposed_diagnosis = str(proposal["diagnosis"])
    selected = selected or proposed_diagnosis
    action = str(proposal["action"])
    if action in NEXT_SEARCH_QUERY_ACTIONS:
        query = str(proposal["next_query"])
        pond_n = max((int(row.get("pond_n") or 0) for row in results["iterations"]), default=0) + 1
        results["pending_query"] = {"key": f"pond_{pond_n:02d}", "query": query}
        results["status"] = "ready_to_compile"
    elif action == "ranking_fix":
        prior_payload = _read_json(Path(iteration["arm"]["payload_json"]))
        results["pending_payload"] = {
            "pond_n": int(iteration["pond_n"]), "query": iteration["query"],
            "payload_json": iteration["arm"]["payload_json"], "ledger": iteration["arm"]["ledger"],
            "payload": prior_payload,
            "rerank_exclusions": list((iteration.get("input") or {}).get("rerank_exclusions") or []),
            "rerank_only": True, "pattern_default_edits": [],
        }
        results["status"] = "awaiting_payload_review"
    else:
        results["status"] = "completed"
    move = {key: proposal[key] for key in ("action", "next_query", "source", "rationale")}
    iteration["diagnosis"] = selected
    iteration["next_move"] = move
    iteration["proposal_delta"] = {
        "proposal": {"diagnosis": proposed_diagnosis, "action": proposal["action"],
                     "next_query": proposal.get("next_query"), "source": proposal.get("source")},
        "actual": {"diagnosis": selected, "action": proposal["action"],
                   "next_query": proposal.get("next_query"), "source": proposal.get("source")},
        "changed": proposed_diagnosis != selected,
    }
    _price_usage_log(run_dir / "usage.jsonl")
    iteration["cost_usd"] = _pond_costs(run_dir).get(int(iteration["pond_n"]), 0.0)
    _save(results, run_dir)
    return run_dir / "results.json"
