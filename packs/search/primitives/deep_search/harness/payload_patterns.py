"""The pattern pass that edits a compiled payload before the pond runs.

`_llm_pattern_defaults` is the shipped path: it retrieves recruiter-edit
precedents, asks for a small edit list, checkpoints the raw response, and
applies it through `_apply_pattern_proposal`. When that call fails for any
reason it falls back to the deterministic `_pattern_defaults` rules and labels
every change as the fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

try:  # direct script execution
    from precedents import retrieve_payload_edits
    from harness.artifacts import _read_json, _response_usage, _write_json
    from harness.prompts import PATTERN_DEFAULT_PROMPT
    from harness.summary import _save
except ImportError:  # pragma: no cover - module execution
    from ..precedents import retrieve_payload_edits
    from .artifacts import _read_json, _response_usage, _write_json
    from .prompts import PATTERN_DEFAULT_PROMPT
    from .summary import _save
from openai_client import make_openai_client

HARD_FILTER_FIELDS = ("fields_of_study", "sector_types", "entity_types")


def _pattern_defaults(payload: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    edited = deepcopy(payload)
    filters = edited["role_search_filters"]
    changes = []
    for field in HARD_FILTER_FIELDS:
        if filters.get(field):
            before = deepcopy(filters.pop(field))
            changes.append({"pattern": "drop_duplicate_hard_filter", "field": field,
                            "from": before, "to": None})
    role_trait = next((str(row.get("value") or "").casefold() for row in edited.get("traits") or []
                       if row.get("meaning") == "role"), "")
    bm25 = list(filters.get("bm25_queries") or [])
    if role_trait and len(filters.get("role_ids") or []) <= 1 and len(bm25) > 1:
        words = {word for word in re.findall(r"[a-z0-9]+", role_trait) if len(word) > 2}
        kept = [value for value in bm25
                if words and words <= set(re.findall(r"[a-z0-9]+", str(value).casefold()))]
        if kept and kept != bm25:
            filters["bm25_queries"] = kept
            changes.append({"pattern": "prune_keyword_fanout", "field": "bm25_queries",
                            "from": bm25, "to": kept})
    occupation = " ".join((str(plan.get("normalized_archetype") or ""), role_trait)).casefold()
    bands = list(filters.get("seniority_bands") or [])
    departments = {str(value).casefold() for value in filters.get("role_departments") or []}
    if ({"design", "engineering"} <= departments or
            any(word in occupation for word in ("assistant", "consultant", "banker"))):
        target = []
    elif any(word in occupation for word in ("recruit", "talent")):
        target = ["mid", "senior", "staff", "principal", "manager", "director", "vp"]
    elif any(word in occupation for word in ("engineer", "developer", "research")):
        target = ["mid", "senior", "staff", "principal"]
    else:
        target = bands
    if target != bands:
        if target:
            filters["seniority_bands"] = target
        else:
            filters.pop("seniority_bands", None)
        changes.append({"pattern": "retune_seniority", "field": "seniority_bands",
                        "from": bands or None, "to": target or None})
    return edited, changes


def _apply_pattern_proposal(payload: Mapping[str, Any], proposal: Mapping[str, Any]
                            ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    edited = deepcopy(payload)
    filters = edited["role_search_filters"]
    changes = []
    valid_bands = {"junior", "mid", "senior", "staff", "principal", "manager", "director", "vp"}
    for item in proposal.get("edits") or []:
        if not isinstance(item, Mapping):
            raise ValueError("pattern edit must be an object")
        pattern, field = str(item.get("pattern") or ""), str(item.get("field") or "")
        reason = " ".join(str(item.get("reason") or "").split())
        if not reason:
            raise ValueError("pattern edit needs a reason")
        before, target = deepcopy(filters.get(field)), item.get("to")
        if pattern == "drop_duplicate_hard_filter" and field in HARD_FILTER_FIELDS and target is None:
            filters.pop(field, None)
        elif pattern == "prune_keyword_fanout" and field in {"role_ids", "bm25_queries"}:
            if not isinstance(target, list) or not target or not set(target) <= set(before or []):
                raise ValueError("keyword pruning must keep a non-empty subset")
            filters[field] = target
        elif pattern == "retune_seniority" and field == "seniority_bands":
            if target is not None and (not isinstance(target, list) or not set(target) <= valid_bands):
                raise ValueError("invalid seniority proposal")
            if target:
                filters[field] = target
            else:
                filters.pop(field, None)
        else:
            raise ValueError("unsupported pattern edit")
        after = deepcopy(filters.get(field))
        if before != after:
            changes.append({"pattern": pattern, "field": field, "from": before,
                            "to": after, "reason": reason, "source": "llm_precedent"})
    return edited, changes


def _llm_pattern_defaults(
    *, payload: Mapping[str, Any], plan: Mapping[str, Any], results: dict[str, Any],
    run_dir: Path, pond_n: int, query: str, client: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checkpoint = run_dir / "ponds" / f"pond-{pond_n:02d}" / "pattern-defaults.raw.json"
    try:
        precedents = retrieve_payload_edits(
            title=str(results.get("title") or ""), brief=results.get("brief") or {}, query=query)
        context = {
            "job": {"title": results.get("title"), "brief": results.get("brief"),
                    "target_level": plan.get("target_level")},
            "query": query, "compiled_payload": payload,
            "prior_pool": ((results.get("iterations") or [{}])[-1].get("pool_stats")
                           if results.get("iterations") else None),
            "retrieved_precedents": precedents,
        }
        input_sha = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        if checkpoint.is_file() and _read_json(checkpoint).get("input_sha") == input_sha:
            record = _read_json(checkpoint)
        else:
            os.environ["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
            os.environ["POWERPACKS_USAGE_STAGE"] = f"search_harness.pond_{pond_n:02d}.pattern_defaults"
            os.environ["OPENAI_SERVICE_TIER"] = "flex"
            response = (client or make_openai_client(os.environ.get("OPENAI_API_KEY"))).chat.completions.create(
                model="gpt-5.6-terra", reasoning_effort="medium", service_tier="flex",
                messages=[{"role": "system", "content": PATTERN_DEFAULT_PROMPT},
                          {"role": "user", "content": json.dumps(context, indent=2)}],
                response_format={"type": "json_object"},
            )
            record = {"input_sha": input_sha, "raw": response.choices[0].message.content or "{}",
                      "usage": _response_usage(response), "precedents": precedents}
            _write_json(checkpoint, record)
        raw_record = {"kind": "pattern_defaults", "pond_n": pond_n, **record}
        replaced = False
        for index, row in enumerate(results.get("raw_model_responses") or []):
            if row.get("kind") == "pattern_defaults" and row.get("pond_n") == pond_n:
                results["raw_model_responses"][index] = raw_record
                replaced = True
                break
        if not replaced:
            results["raw_model_responses"].append(raw_record)
        _save(results, run_dir)
        return _apply_pattern_proposal(payload, json.loads(str(record["raw"])))
    except Exception as exc:
        edited, changes = _pattern_defaults(payload, plan)
        for change in changes:
            change.update({"reason": "LLM proposal failed; applied the prior default.",
                           "source": "deterministic_fallback"})
        results["raw_model_responses"].append({
            "kind": "pattern_defaults_fallback", "pond_n": pond_n,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return edited, changes
