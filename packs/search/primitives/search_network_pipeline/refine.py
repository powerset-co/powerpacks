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
from packs.search.primitives.hydrate_people.hydrate_people import fetch_api_person_rows
from packs.search.primitives.deep_search.results_web.model import original_quick_step, refine_interrupted, select_quick_step
from packs.search.primitives.lib.powerpacks_contracts import normalize_hydrated_context
from packs.search.primitives.lib.usage_pricing import load_prices, row_cost_usd
from packs.search.primitives.persist_search_results.results_io import hydrated_profiles, now_iso, read_jsonl, step_output
from packs.shared import models
from packs.search.primitives.shared.openai_client import _usage_row as usage_row, make_openai_client
from packs.search.primitives.search_network_pipeline.refine_prompt import build_prompt, candidate_xml, cohort_summary

PRICES = load_prices()
MIN_RERANK_SCORE = 0.5
MIN_INITIAL_SCORE = 0.3
BATCH_SIZE = 2
# Cloud api_v2/search/agent_rerank.py:367,430 caps concurrent batches at 200.
MAX_CONCURRENT_BATCHES = 200
# The tables a `--sql` query sees: the cloud QueryEngine's four (api_v2/agents/tools/query_engine.py),
# same column names, from the local hydrated profile. Enum-like text is lower-cased as the cloud does,
# so the cloud prompt's values ('series_b', 'c-suite', 'venture_backed_startup') match here too.
TABLES = {
    "candidates": {"person_id": "VARCHAR", "name": "VARCHAR", "headline": "VARCHAR", "summary": "VARCHAR",
                   "location": "VARCHAR", "all_current_titles": "VARCHAR", "linkedin_followers": "INTEGER",
                   "linkedin_connections": "INTEGER", "x_twitter_followers": "INTEGER", "total_interactions": "INTEGER",
                   "inferred_age": "INTEGER", "inferred_birth_year": "INTEGER", "total_years_experience": "DOUBLE",
                   "tech_skills": "VARCHAR[]", "base_score": "DOUBLE"},
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
LOWER = ("seniority_band", "company_stage", "role_track")
LOWER_LISTS = ("company_sector_types", "company_entity_types")


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
    if models.takes_reasoning_effort(model):
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
    usage = usage_row(model, response, 0)
    return scores, query_type, (row_cost_usd(usage, PRICES) or 0) if usage else 0


def _lower(value):
    return str(value).strip().lower() if value else None


def _table_rows(inputs):
    """The four tables' rows, one profile at a time."""
    rows = {name: [] for name in TABLES}
    for profile in inputs:
        pid = profile["person_id"]
        positions = profile.get("positions") or []
        current = [" at ".join(str(part) for part in (position.get("position_title"), position.get("company_name")) if part)
                   for position in positions if position.get("is_current")]
        rows["candidates"].append({**{key: profile.get(key) for key in TABLES["candidates"]}, "person_id": pid,
                                   "all_current_titles": " | ".join(title for title in current if title) or None,
                                   "total_years_experience": profile.get("years_of_experience")})
        matched = set(profile.get("matched_position_indexes") or [])
        for index, position in enumerate(positions):
            index = position.get("position_index", index)
            row = {key: position.get(key) for key in TABLES["positions"]}
            row.update({key: _lower(row[key]) for key in LOWER})
            row.update({key: [_lower(item) for item in row[key] or []] for key in LOWER_LISTS})
            rows["positions"].append({**row, "person_id": pid, "position_index": index, "is_matched": index in matched})
        for school in profile.get("education") or []:
            rows["education"].append({**{key: school.get(key) for key in TABLES["education"]}, "person_id": pid})
        for trait, value in (profile.get("trait_scores") or {}).items():
            score = value.get("score") if isinstance(value, dict) else value
            if isinstance(score, (int, float)):
                rows["trait_scores"].append({"person_id": pid, "trait_name": trait, "score": score})
    return rows


def _sql_rows(inputs, sql):
    """person_id -> reasoning (the query's `reasoning` column, or None) for the rows a SQL query
    returns, in its order, from the four tables built in memory."""
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
        at, why = columns.index("person_id"), columns.index("reasoning") if "reasoning" in columns else None
        ordered = {}
        for row in result.fetchall():
            ordered.setdefault(row[at], row[why] if why is not None else None)
        return ordered


def _pool(project, ledger, current, *, screened, run_dir, env_file):
    """The rows showing and each one's hydrated profile, the judge's and the SQL tables' input.
    `screened` drops the first step's rows the page hides: under the 0.3 cut or with a zero trait."""
    state = json.loads((project / ledger["state"]).read_text())
    profiles = hydrated_profiles(state)
    if not profiles:
        raise ValueError("quick run has no hydrated profiles")
    rows = read_jsonl(project / current["jsonl"])
    if screened:
        rows = [row for row in rows if float(row["final_score"]) >= MIN_INITIAL_SCORE and not _has_zero_trait_score(row.get("trait_scores"))]
    if step_output(state, "execute_role_search").get("set_id"):
        full = {person["id"]: normalize_hydrated_context(person) for person in
                fetch_api_person_rows([row["person_id"] for row in rows], env_file, cache_dir=run_dir / "people")}
        for row in rows:
            pid = row["person_id"]
            profiles[pid] = dict(full[pid], total_interactions=profiles[pid].get("total_interactions"))
    inputs = []
    for row in rows:
        profile = dict(profiles[row["person_id"]], person_id=row["person_id"], base_score=float(row["final_score"]))
        for key, default in (("matched_position_indexes", []), ("trait_scores", {}), ("vertical_sources", [])):
            profile[key] = _decoded(row.get(key), default)
        profile["trait_scores"] = profile["trait_scores"].get("trait_scores", profile["trait_scores"])
        inputs.append(profile)
    return rows, inputs


def _sql_step(rows, inputs, sql):
    """The rows a SQL query keeps, in its order; a `reasoning` column replaces the row's."""
    found = _sql_rows(inputs, sql)
    order = {pid: index for index, pid in enumerate(found)}
    kept = sorted((dict(row, overall_reasoning=found[row["person_id"]]) if found[row["person_id"]] else row
                   for row in rows if row["person_id"] in found), key=lambda row: order[row["person_id"]])
    return kept, "sort" if len(kept) == len(rows) else "filter", 0


def _judge_step(rows, inputs, query, criteria, model, env):
    """The rows the judge keeps. It only keeps or drops: kept rows keep their trait score and the
    order they had (only a SQL sort reorders). Also the judge's query type and what the calls cost."""
    summary = cohort_summary(inputs)
    batches = [inputs[i:i + BATCH_SIZE] for i in range(0, len(inputs), BATCH_SIZE)]
    with make_openai_client(api_key=env["OPENAI_API_KEY"], api_base=env.get("OPENAI_API_BASE") or env.get("OPENAI_BASE_URL")) as client, \
            ThreadPoolExecutor(max_workers=max(1, min(len(batches), MAX_CONCURRENT_BATCHES))) as executor:
        scored = list(executor.map(lambda batch: _score_batch(client, model, batch, query, criteria, summary), batches))
    votes = Counter(query_type for _, query_type, _ in scored)
    judged = {pid: (score, reasoning) for scores, _, _ in scored for pid, score, reasoning in scores}
    kept = [dict(row, overall_reasoning=judged[row["person_id"]][1]) for row in rows
            if row["person_id"] in judged and judged[row["person_id"]][0] >= MIN_RERANK_SCORE]
    return kept, votes.most_common(1)[0][0] if votes else "rerank", sum(cost for _, _, cost in scored)


def rerank(run_dir: Path, *, criteria: str | None, use: str | None, model: str, env: dict[str, str], sql: str | None = None,
           env_file: Path | None = None):
    """Go back to a saved step (`use`), or add one from the rows showing: by SQL, else by the judge."""
    run_dir = run_dir.expanduser().resolve()
    if run_dir.parent.name != "search" or run_dir.parent.parent.name != ".powerpacks":
        raise ValueError("rerank requires a quick run under .powerpacks/search/")
    project, root, run_id = run_dir.parent.parent.parent, run_dir.parent.parent / "deep-search", run_dir.name
    catalog_path = root / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    card = catalog[run_id]
    if card["status"] == "running" and not refine_interrupted(card):
        raise ValueError("quick run is still running")
    result_path = root / run_id / "results.json"
    payload = json.loads(result_path.read_text())
    steps = payload.get("steps") or [original_quick_step(payload)]
    parent = payload.get("current_step", "s1")
    if use is not None:
        step = next((step for step in steps if step["id"] == use), None)
        if step is None:
            raise ValueError(f"unknown step: {use}")
        return _save(payload, steps, step, result_path, card, catalog_path, catalog, run_id, use)
    ledger = json.loads((run_dir / "pipeline.ledger.json").read_text())
    current = next(step for step in steps if step["id"] == parent)
    if sql is None and not env.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required")
    rows, inputs = _pool(project, ledger, current, screened=parent == "s1" and sql is None, run_dir=run_dir, env_file=env_file)
    if sql is not None:
        kept, query_type, cost = _sql_step(rows, inputs, sql)
    else:
        card.update(status="running", refine_pid=os.getpid(), updated_at=now_iso())
        write_json(catalog_path, catalog)
        try:
            kept, query_type, cost = _judge_step(rows, inputs, payload["iterations"][0]["query"], criteria, model, env)
        except BaseException:
            card.pop("refine_pid", None)
            card.update(status="failed", updated_at=now_iso())
            write_json(catalog_path, catalog)
            raise
    step_id = f"s{len(steps) + 1}"
    jsonl = (project / steps[0]["jsonl"]).with_suffix(f".{step_id}.jsonl")
    jsonl.write_text("".join(json.dumps(row) + "\n" for row in kept))
    step = dict(id=step_id, parent=parent, criteria=criteria, sql=sql, query_type=query_type, shown=len(kept),
                scored=len(rows), jsonl=str(jsonl.relative_to(project)), cost_usd=cost, created_at=now_iso())
    steps.append(step)
    return _save(payload, steps, step, result_path, card, catalog_path, catalog, run_id, use)


def _save(payload, steps, step, result_path, card, catalog_path, catalog, run_id, use):
    payload["steps"] = steps
    select_quick_step(payload, step)
    write_json(result_path, payload)
    card.pop("refine_pid", None)
    card.update(status="completed", candidates=step["shown"], updated_at=now_iso())
    write_json(catalog_path, catalog)
    print(f'search: {step["shown"]} kept of {step["scored"]} for "{step["criteria"]}"', file=sys.stderr)
    return {"primitive": "search_network_pipeline", "status": "completed",
            "step": {"run_id": run_id, **{key: step[key] for key in ("id", "parent", "criteria", "query_type", "shown", "scored")},
                     "cost_usd": 0 if use is not None else step["cost_usd"]}}
