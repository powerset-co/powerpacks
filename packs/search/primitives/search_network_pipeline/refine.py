"""Narrow, sort or score the current quick-search rows and append or select a saved step.

Two tools, the cloud agent's split (api_v2/agents/react_tools.py query_results / score_results):
a hard criterion (age, place, school, company, seniority, dates, counts) is SQL over the rows
showing, free and exact; a qualitative one is the OpenAI judge.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile

import duckdb

from packs.indexing.lib.io import write_json
from packs.search.primitives.deep_search.results_web.model import original_quick_step, select_quick_step
from packs.search.primitives.lib.usage_pricing import load_prices, row_cost_usd
from packs.search.primitives.persist_search_results.results_io import hydrated_profiles, now_iso, read_jsonl
from packs.search.primitives.shared.openai_client import make_openai_client
from packs.search.primitives.search_network_pipeline.refine_prompt import build_prompt, candidate_xml, cohort_summary

MIN_RERANK_SCORE = 0.5
MIN_INITIAL_SCORE = 0.3
BATCH_SIZE = 2
# Cloud api_v2/search/agent_rerank.py:367,430 caps concurrent batches at 200.
MAX_CONCURRENT_BATCHES = 200
# The tables a `--sql` query sees: the cloud QueryEngine's four, from the local hydrated profile.
TABLES = {
    "candidates": {"person_id": "VARCHAR", "name": "VARCHAR", "headline": "VARCHAR", "location": "VARCHAR",
                   "inferred_age": "INTEGER", "inferred_birth_year": "INTEGER", "years_of_experience": "DOUBLE",
                   "total_interactions": "INTEGER", "tech_skills": "VARCHAR[]", "base_score": "DOUBLE"},
    "positions": {"person_id": "VARCHAR", "position_index": "INTEGER", "is_matched": "BOOLEAN", "is_current": "BOOLEAN",
                  "position_title": "VARCHAR", "company_name": "VARCHAR", "company_domain": "VARCHAR",
                  "seniority_band": "VARCHAR", "role_track": "VARCHAR", "company_headcount": "INTEGER",
                  "company_funding_total": "DOUBLE", "company_stage": "VARCHAR", "company_sector_types": "VARCHAR[]",
                  "company_entity_types": "VARCHAR[]", "investor_names": "VARCHAR[]", "location": "VARCHAR",
                  "start_date": "VARCHAR", "end_date": "VARCHAR", "tenure_years": "DOUBLE"},
    "education": {"person_id": "VARCHAR", "school_name": "VARCHAR", "degree": "VARCHAR", "field_of_study": "VARCHAR",
                  "start_year": "INTEGER", "end_year": "INTEGER", "graduation_year": "INTEGER"},
    "trait_scores": {"person_id": "VARCHAR", "trait_name": "VARCHAR", "score": "DOUBLE"},
}


def _decoded(value, default):
    return json.loads(value) if isinstance(value, str) and value.strip() else value or default


def _has_zero_trait_score(traits):
    traits = _decoded(traits, {})
    if not isinstance(traits, dict):
        return False
    traits = traits.get("trait_scores", traits)
    if not isinstance(traits, dict):
        return False
    for name, value in traits.items():
        if name in ("overall_reasoning", "matched_position_indexes", "vertical_sources"):
            continue
        score = value.get("score") if isinstance(value, dict) else value
        if isinstance(score, (int, float)) and score == 0:
            return True
    return False


def _score_batch(client, model, batch, query, criteria, summary):
    xml = "\n".join(["<candidates>", *(candidate_xml(profile) for profile in batch), "</candidates>"])
    params = {"model": model, "messages": [{"role": "user", "content": build_prompt(query, criteria, summary, xml)}]}
    if "gpt-5" in model.lower():
        params.update(max_completion_tokens=4096, reasoning_effort="low")
    else:
        params.update(max_tokens=4096, temperature=0.1)
    response = client.chat.completions.create(**params)
    content = response.choices[0].message.content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()
    parsed = json.loads(content)
    results, query_type = (parsed, "rerank") if isinstance(parsed, list) else (parsed.get("results", []), parsed.get("query_type", "rerank"))
    valid_ids = {profile["person_id"] for profile in batch}
    scores = []
    for item in results:
        pid = item.get("person_id", "")
        if pid not in valid_ids:
            if len(pid) > 36 and pid[:36] in valid_ids:
                pid = pid[:36]
            else:
                continue
        scores.append((pid, float(item.get("score", 0)), item.get("reasoning", "")))
    return scores, query_type


def _table_rows(inputs):
    """The four tables' rows, one profile at a time."""
    rows = {name: [] for name in TABLES}
    for profile in inputs:
        pid = profile["person_id"]
        rows["candidates"].append({**{key: profile.get(key) for key in TABLES["candidates"]}, "person_id": pid})
        matched = set(profile.get("matched_position_indexes") or [])
        for index, position in enumerate(profile.get("positions") or []):
            rows["positions"].append({**{key: position.get(key) for key in TABLES["positions"]},
                                      "person_id": pid, "position_index": index, "is_matched": index in matched})
        for school in profile.get("education") or []:
            rows["education"].append({**{key: school.get(key) for key in TABLES["education"]}, "person_id": pid})
        for trait, value in (profile.get("trait_scores") or {}).items():
            score = value.get("score") if isinstance(value, dict) else value
            if isinstance(score, (int, float)):
                rows["trait_scores"].append({"person_id": pid, "trait_name": trait, "score": score})
    return rows


def _sql_person_ids(inputs, sql):
    """The person_ids a SQL query returns, in its order, from the four tables built in memory."""
    with tempfile.TemporaryDirectory() as raw, duckdb.connect() as conn:
        for name, rows in _table_rows(inputs).items():
            path = Path(raw) / f"{name}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            conn.execute(f"CREATE TABLE {name} AS SELECT * FROM read_json(?, format='newline_delimited', columns=?)",
                         [str(path), TABLES[name]])
        result = conn.execute(sql)
        columns = [column[0] for column in result.description]
        if "person_id" not in columns:
            raise ValueError("the query must return person_id")
        at = columns.index("person_id")
        return list(dict.fromkeys(row[at] for row in result.fetchall()))


def rerank(run_dir: Path, *, criteria: str | None, use: str | None, model: str, env: dict[str, str], sql: str | None = None):
    run_dir = run_dir.expanduser().resolve()
    if run_dir.parent.name != "search" or run_dir.parent.parent.name != ".powerpacks":
        raise ValueError("rerank requires a quick run under .powerpacks/search/")
    ledger = json.loads((run_dir / "pipeline.ledger.json").read_text())
    root = run_dir.parent.parent / "deep-search"
    run_id = run_dir.name
    catalog_path = root / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    card = catalog[run_id]
    if card["status"] == "running":
        raise ValueError("quick run is still running")
    result_path = root / run_id / "results.json"
    payload = json.loads(result_path.read_text())
    steps = payload.get("steps") or [original_quick_step(payload)]
    parent = payload.get("current_step", "s1")
    if use is not None:
        step = next((step for step in steps if step["id"] == use), None)
        if step is None:
            raise ValueError(f"unknown step: {use}")
    else:
        project = run_dir.parent.parent.parent
        profiles = hydrated_profiles(json.loads((project / ledger["state"]).read_text()))
        if not profiles:
            raise ValueError("quick run has no hydrated profiles")
        current = next(step for step in steps if step["id"] == parent)
        rows = read_jsonl(project / current["jsonl"])
        if parent == "s1" and sql is None:
            rows = [row for row in rows if float(row["final_score"]) >= MIN_INITIAL_SCORE and not _has_zero_trait_score(row.get("trait_scores"))]
        inputs = []
        for row in rows:
            profile = dict(profiles[row["person_id"]], person_id=row["person_id"], base_score=float(row["final_score"]))
            for key, default in (("matched_position_indexes", []), ("trait_scores", {}), ("vertical_sources", [])):
                profile[key] = _decoded(row.get(key), default)
            profile["trait_scores"] = profile["trait_scores"].get("trait_scores", profile["trait_scores"])
            inputs.append(profile)
        step_id = f"s{len(steps) + 1}"
        jsonl = (project / steps[0]["jsonl"]).with_suffix(f".{step_id}.jsonl")
        if sql is not None:
            order = {pid: index for index, pid in enumerate(_sql_person_ids(inputs, sql))}
            kept = sorted((row for row in rows if row["person_id"] in order), key=lambda row: order[row["person_id"]])
            jsonl.write_text("".join(json.dumps(row) + "\n" for row in kept))
            step = dict(id=step_id, parent=parent, criteria=criteria, sql=sql,
                        query_type="sort" if len(kept) == len(rows) else "filter", shown=len(kept),
                        scored=len(rows), jsonl=str(jsonl.relative_to(project)), cost_usd=0, created_at=now_iso())
            steps.append(step)
            return _save(payload, steps, step, result_path, card, catalog_path, catalog, run_id, use)
        if not env.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is required")
        usage_path = Path(env.get("POWERPACKS_USAGE_LOG") or run_dir / f"refine.{step_id}.usage.jsonl")
        offset = usage_path.stat().st_size if usage_path.exists() else 0
        # The shared client captures the chosen log path when it is constructed.
        previous_log = os.environ.get("POWERPACKS_USAGE_LOG")
        os.environ["POWERPACKS_USAGE_LOG"] = str(usage_path)
        try:
            with make_openai_client(api_key=env["OPENAI_API_KEY"], api_base=env.get("OPENAI_API_BASE") or env.get("OPENAI_BASE_URL")) as client:
                card.update(status="running", updated_at=now_iso())
                write_json(catalog_path, catalog)
                try:
                    summary = cohort_summary(inputs)
                    batches = [inputs[i:i + BATCH_SIZE] for i in range(0, len(inputs), BATCH_SIZE)]
                    with ThreadPoolExecutor(max_workers=max(1, min(len(batches), MAX_CONCURRENT_BATCHES))) as executor:
                        futures = [executor.submit(_score_batch, client, model, batch, payload["iterations"][0]["query"], criteria, summary) for batch in batches]
                        scored = [future.result() for future in futures]
                    votes = Counter(query_type for _, query_type in scored)
                    query_type = votes.most_common(1)[0][0] if votes else "rerank"
                    parents = {row["person_id"]: row for row in rows}
                    kept = []
                    for scores, _ in scored:
                        for pid, score, reasoning in scores:
                            row = parents[pid]
                            final = (float(row["final_score"]) if score >= MIN_RERANK_SCORE else 0) if query_type == "filter" else score
                            if final >= MIN_RERANK_SCORE:
                                kept.append(dict(row, final_score=final, overall_reasoning=reasoning))
                    kept.sort(key=lambda row: row["final_score"], reverse=True)
                    costs = []
                    if usage_path.exists():
                        with usage_path.open("rb") as handle:
                            handle.seek(offset)
                            prices = load_prices()
                            costs = [row_cost_usd(json.loads(line), prices) or 0 for line in handle if line.strip()]
                    jsonl.write_text("".join(json.dumps(row) + "\n" for row in kept))
                    step = dict(id=step_id, parent=parent, criteria=criteria, sql=None, query_type=query_type, shown=len(kept),
                                scored=len(rows), jsonl=str(jsonl.relative_to(project)), cost_usd=sum(costs), created_at=now_iso())
                    steps.append(step)
                except BaseException:
                    card.update(status="failed", updated_at=now_iso())
                    write_json(catalog_path, catalog)
                    raise
        finally:
            if previous_log is None:
                os.environ.pop("POWERPACKS_USAGE_LOG", None)
            else:
                os.environ["POWERPACKS_USAGE_LOG"] = previous_log
    return _save(payload, steps, step, result_path, card, catalog_path, catalog, run_id, use)


def _save(payload, steps, step, result_path, card, catalog_path, catalog, run_id, use):
    payload["steps"] = steps
    select_quick_step(payload, step)
    write_json(result_path, payload)
    card.update(status="completed", candidates=step["shown"], updated_at=now_iso())
    write_json(catalog_path, catalog)
    print(f'search: {step["shown"]} kept of {step["scored"]} for "{step["criteria"]}"', file=sys.stderr)
    return {"primitive": "search_network_pipeline", "status": "completed",
            "step": {"run_id": run_id, **{key: step[key] for key in ("id", "parent", "criteria", "query_type", "shown", "scored")},
                     "cost_usd": 0 if use is not None else step["cost_usd"]}}
