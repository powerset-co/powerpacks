#!/usr/bin/env python3
"""Resumable local Powerpacks search-index processing pipeline."""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.artifacts import (  # noqa: E402
    build_company_corpus,
    build_education_corpus,
    build_location_corpus,
    build_summary_records,
)
from packs.indexing.lib.contracts import (  # noqa: E402
    count_defaulted_numeric,
    load_search_contract,
    normalize_record_for_contract,
    validate_jsonl,
)
from packs.indexing.lib.duckdb_artifacts import build_local_duckdb, validate_local_search_index  # noqa: E402
from packs.indexing.lib.io import emit_json, iter_jsonl, read_jsonl, write_json, write_jsonl  # noqa: E402
from packs.indexing.lib.ledger import load_ledger, mark_step, save_ledger  # noqa: E402
from packs.indexing.lib.manifest import (  # noqa: E402
    build_manifest,
    cache_dir,
    manifest_path,
    manifest_ready,
    promote_latest,
    read_manifest,
    restore_cache,
    store_cache,
    write_manifest,
)
from packs.indexing.lib.people import build_people_records, build_unified_profiles, flatten_people  # noqa: E402
from packs.indexing.lib.text import dense_text  # noqa: E402
from packs.search.primitives.lib.local_hydration_store import load_profiles_from_duckdb  # noqa: E402

STEPS = [
    "flatten_people",
    "build_roles",
    "build_company_corpus",
    "build_education_corpus",
    "build_location_corpus",
    "build_people_records",
    "build_unified_profiles",
    "build_summary_records",
    "validate_contracts",
    "build_local_duckdb",
    "validate_local_search_index",
    "validate_local_hydration",
]


def run_dir(output_dir: Path, run_id: str) -> Path:
    return output_dir / run_id


def paths(rd: Path) -> dict[str, Path]:
    return {
        "ledger": rd / "ledger.json",
        "flattened": rd / "unified/flattened_people.jsonl",
        "unified_csv": rd / "unified/unified_person.csv",
        "profiles": rd / "profiles/hydrated_profiles.jsonl",
        "raw_titles": rd / "roles/raw_titles.jsonl",
        "role_mapping": rd / "roles/role_mapping.csv",
        "roles_dense": rd / "roles/roles_with_dense_text.jsonl",
        "companies_corpus": rd / "company/companies_corpus.jsonl",
        "schools_corpus": rd / "education/schools_corpus.jsonl",
        "people_education": rd / "education/people_education.jsonl",
        "locations_corpus": rd / "location/locations_corpus.jsonl",
        "summary_internal": rd / "summaries/summary_records.jsonl",
        "people_records": rd / "records/people.records.jsonl",
        "companies_records": rd / "records/companies.records.jsonl",
        "schools_records": rd / "records/schools.records.jsonl",
        "education_records": rd / "records/education.records.jsonl",
        "summaries_records": rd / "records/summaries.records.jsonl",
        "duckdb": rd / "local-search.duckdb",
        "manifest": rd / "index-manifest.json",
    }


def stats_path(ledger: dict[str, Any], name: str) -> Path:
    return Path(ledger["run_dir"]) / "stats" / f"{name}.json"


def write_stats(ledger: dict[str, Any], name: str, payload: dict[str, Any]) -> None:
    write_json(stats_path(ledger, name), payload)


def default_ledger(input_path: Path, rd: Path, run_id: str, default_operator_id: str | None = None, limit: int | None = None) -> dict[str, Any]:
    return {
        "primitive": "build_processing_pipeline",
        "version": 1,
        "status": "pending",
        "run_id": run_id,
        "run_dir": str(rd),
        "input": str(input_path),
        "default_operator_id": default_operator_id,
        "limit": limit,
        "steps": [{"id": step, "status": "pending"} for step in STEPS],
        "artifacts": {},
    }


def step_flatten(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    people = flatten_people(ledger["input"])
    if ledger.get("limit") is not None:
        people = people[: int(ledger["limit"])]
    write_jsonl(ps["flattened"], people)
    stats = {"people": len(people)}
    write_stats(ledger, "flatten_people", stats)
    return {"flattened_people": str(ps["flattened"])}, stats


def step_roles(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    import hashlib

    by_hash: dict[str, dict[str, Any]] = {}
    for person in iter_jsonl(ps["flattened"]):
        for exp in person.get("work_experiences") or []:
            if not isinstance(exp, dict):
                continue
            title = str(exp.get("title") or exp.get("position_title") or exp.get("role") or "").strip()
            description = str(exp.get("description") or exp.get("summary") or "").strip()
            if not title:
                continue
            title_hash = hashlib.sha256(f"{title.lower()}|{description.lower()}".encode()).hexdigest()[:16]
            by_hash.setdefault(
                title_hash,
                {
                    "title_hash": title_hash,
                    "raw_title": title,
                    "description": description,
                    "expanded_title": title,
                    "role_ids": [],
                    "seniority_band": "",
                    "role_track": "",
                    "doc2query": [],
                    "inferred_skills": [],
                    "dense_text": dense_text([title, description, exp.get("company_name"), person.get("headline")]),
                },
            )
    roles = [by_hash[key] for key in sorted(by_hash)]
    write_jsonl(ps["raw_titles"], [{"title_hash": row["title_hash"], "raw_title": row["raw_title"], "description": row["description"]} for row in roles])
    write_jsonl(ps["roles_dense"], roles)
    ps["role_mapping"].parent.mkdir(parents=True, exist_ok=True)
    with ps["role_mapping"].open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["title_hash", "raw_title", "expanded_title", "seniority_band", "role_track"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in roles:
            writer.writerow({key: row[key] for key in fieldnames})
    stats = {"roles": len(roles)}
    write_stats(ledger, "build_roles", stats)
    return {"roles_with_dense_text": str(ps["roles_dense"])}, stats


def step_company(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    corpus = build_company_corpus(read_jsonl(ps["flattened"]), ledger.get("default_operator_id"))
    contract = load_search_contract("turbopuffer/companies.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in corpus]
    write_jsonl(ps["companies_corpus"], corpus)
    write_jsonl(ps["companies_records"], records)
    stats = {"companies": len(records), "defaulted_numeric_fields": {"companies": count_defaulted_numeric(corpus, contract)}}
    write_stats(ledger, "build_company_corpus", stats)
    return {"companies": str(ps["companies_records"])}, stats


def step_education(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    result = build_education_corpus(read_jsonl(ps["flattened"]), ledger.get("default_operator_id"))
    school_contract = load_search_contract("turbopuffer/schools.namespace.json")
    education_contract = load_search_contract("turbopuffer/education.namespace.json")
    school_records = [normalize_record_for_contract(row, school_contract) for row in result["schools"]]
    education_records = [normalize_record_for_contract(row, education_contract) | {"id": row["id"]} for row in result["education"]]
    write_jsonl(ps["schools_corpus"], result["schools"])
    write_jsonl(ps["people_education"], result["education"])
    write_jsonl(ps["schools_records"], school_records)
    write_jsonl(ps["education_records"], education_records)
    stats = {
        "schools": len(school_records),
        "education": len(education_records),
        "defaulted_numeric_fields": {
            "schools": count_defaulted_numeric(result["schools"], school_contract),
            "education": count_defaulted_numeric(result["education"], education_contract),
        },
    }
    write_stats(ledger, "build_education_corpus", stats)
    return {"schools": str(ps["schools_records"]), "education": str(ps["education_records"])}, stats


def step_location(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    locations = build_location_corpus([*iter_jsonl(ps["flattened"]), *iter_jsonl(ps["companies_corpus"])])
    write_jsonl(ps["locations_corpus"], locations)
    stats = {"locations": len(locations), "internal_only": True}
    write_stats(ledger, "build_location_corpus", stats)
    return {"locations": str(ps["locations_corpus"])}, stats


def step_people(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    records = build_people_records(read_jsonl(ps["flattened"]), default_operator_id=ledger.get("default_operator_id"))
    contract = load_search_contract("turbopuffer/people.namespace.json")
    normalized = [normalize_record_for_contract(row, contract) for row in records]
    write_jsonl(ps["people_records"], normalized)
    stats = {
        "people_records": len(normalized),
        "defaulted_numeric_fields": {"people": count_defaulted_numeric(records, contract)},
        "allowed_operator_ids_default": ledger.get("default_operator_id") or "local:user",
    }
    write_stats(ledger, "build_people_records", stats)
    return {"people": str(ps["people_records"])}, stats


def step_profiles(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    profiles = build_unified_profiles(read_jsonl(ps["flattened"]))
    write_jsonl(ps["profiles"], profiles)
    ps["unified_csv"].parent.mkdir(parents=True, exist_ok=True)
    with ps["unified_csv"].open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["id", "full_name", "linkedin_url", "headline", "location_raw"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for profile in profiles:
            writer.writerow(
                {
                    "id": profile["id"],
                    "full_name": profile.get("name", ""),
                    "linkedin_url": profile.get("linkedin_url") or "",
                    "headline": profile.get("headline") or "",
                    "location_raw": profile.get("location") or "",
                }
            )
    stats = {"profiles": len(profiles)}
    write_stats(ledger, "build_unified_profiles", stats)
    return {"profiles": str(ps["profiles"]), "unified_person": str(ps["unified_csv"])}, stats


def step_summary(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    result = build_summary_records(read_jsonl(ps["profiles"]), ledger.get("default_operator_id"))
    contract = load_search_contract("turbopuffer/summaries.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in result["summaries"]]
    write_jsonl(ps["summary_internal"], result["internal_text"])
    write_jsonl(ps["summaries_records"], records)
    stats = {"summaries": len(records), "defaulted_numeric_fields": {"summaries": count_defaulted_numeric(result["summaries"], contract)}}
    write_stats(ledger, "build_summary_records", stats)
    return {"summaries": str(ps["summaries_records"])}, stats


def step_validate(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    validation = {}
    for name, path, contract in [
        ("people", ps["people_records"], "turbopuffer/people.namespace.json"),
        ("companies", ps["companies_records"], "turbopuffer/companies.namespace.json"),
        ("schools", ps["schools_records"], "turbopuffer/schools.namespace.json"),
        ("education", ps["education_records"], "turbopuffer/education.namespace.json"),
        ("summaries", ps["summaries_records"], "turbopuffer/summaries.namespace.json"),
    ]:
        validation[name] = validate_jsonl(path, contract)
    validation["locations"] = {"internal_only": True, "path": str(ps["locations_corpus"])}
    write_stats(ledger, "validate_contracts", validation)
    return {}, {"validation": validation}


def step_local_duckdb(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    stats = build_local_duckdb(ledger["run_dir"], ps["duckdb"])
    return {"local_search_duckdb": str(ps["duckdb"]), "local_search_duckdb_sha256": stats["checksum"]}, stats


def step_validate_local_search(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    validation = validate_local_search_index(ps["duckdb"])
    write_stats(ledger, "validate_local_search_index", validation)
    return {}, validation


def step_validate_local_hydration(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    profiles = list(iter_jsonl(ps["profiles"]))
    requested = [str(p.get("person_id") or p.get("base_id") or p.get("id")) for p in profiles[:5] if p.get("person_id") or p.get("base_id") or p.get("id")]
    hydrated, source = load_profiles_from_duckdb(ps["duckdb"], requested)
    validation = {
        "hydration_parity_ok": len(hydrated) == len(requested),
        "requested": len(requested),
        "hydrated": len(hydrated),
        "profile_ids": [profile.get("person_id") or profile.get("base_id") or profile.get("id") for profile in hydrated],
        "source": source,
    }
    write_stats(ledger, "validate_local_hydration", validation)
    return {}, validation


STEP_FUNCTIONS = {
    "flatten_people": step_flatten,
    "build_roles": step_roles,
    "build_company_corpus": step_company,
    "build_education_corpus": step_education,
    "build_location_corpus": step_location,
    "build_people_records": step_people,
    "build_unified_profiles": step_profiles,
    "build_summary_records": step_summary,
    "validate_contracts": step_validate,
    "build_local_duckdb": step_local_duckdb,
    "validate_local_search_index": step_validate_local_search,
    "validate_local_hydration": step_validate_local_hydration,
}


def _contracts_ok(ledger: dict[str, Any]) -> bool:
    step = next((row for row in ledger.get("steps", []) if row.get("id") == "validate_contracts"), {})
    validation = (step.get("stats") or {}).get("validation") or {}
    return all((value.get("ok", True) if isinstance(value, dict) else True) for value in validation.values())


def _final_manifest(ledger: dict[str, Any]) -> dict[str, Any]:
    artifacts = dict(ledger.get("artifacts") or {})
    ps = paths(Path(ledger["run_dir"]))
    artifacts.update({"duckdb": str(ps["duckdb"]), "profiles": str(ps["profiles"]), "people_records": str(ps["people_records"])})
    stages = {step["id"]: {"status": step.get("status"), "stats": step.get("stats", {})} for step in ledger.get("steps", [])}
    search_stats = stages.get("validate_local_search_index", {}).get("stats", {})
    hydration_stats = stages.get("validate_local_hydration", {}).get("stats", {})
    validation = {
        "contracts_ok": _contracts_ok(ledger),
        "duckdb_opened": bool(search_stats.get("duckdb_opened")),
        "namespace_probes_ok": bool(search_stats.get("namespace_probes_ok")),
        "hydration_parity_ok": bool(hydration_stats.get("hydration_parity_ok")),
    }
    ready = ledger.get("status") == "completed" and all(validation.values())
    return build_manifest(
        run_id=ledger["run_id"],
        run_dir=ledger["run_dir"],
        input_path=ledger["input"],
        default_operator_id=ledger.get("default_operator_id"),
        limit=ledger.get("limit"),
        status="ready" if ready else "partial",
        stages=stages,
        artifacts=artifacts,
        validation=validation,
    )


def _replace_paths(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_paths(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, old, new) for key, item in value.items()}
    return value


def rewrite_materialized_metadata(rd: Path, *, input_path: Path, run_id: str, default_operator_id: str | None, limit: int | None) -> dict[str, Any]:
    ledger_path = paths(rd)["ledger"]
    ledger = load_ledger(ledger_path)
    old_run_dir = str(ledger.get("run_dir") or rd)
    ledger = _replace_paths(ledger, old_run_dir, str(rd))
    ledger["run_id"] = run_id
    ledger["run_dir"] = str(rd)
    ledger["input"] = str(input_path)
    ledger["default_operator_id"] = default_operator_id
    ledger["limit"] = limit
    save_ledger(ledger_path, ledger)
    manifest = _final_manifest(ledger)
    write_manifest(rd, manifest)
    return manifest


def execute(ledger_path: Path) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    existing_steps = {step.get("id") for step in ledger.get("steps", [])}
    for step in STEPS:
        if step not in existing_steps:
            ledger.setdefault("steps", []).append({"id": step, "status": "pending"})
    save_ledger(ledger_path, ledger)
    ps = paths(Path(ledger["run_dir"]))
    for step in STEPS:
        current = next(item for item in ledger["steps"] if item["id"] == step)
        if current.get("status") == "completed":
            continue
        artifacts, stats = STEP_FUNCTIONS[step](ledger, ps)
        ledger = mark_step(ledger_path, ledger, step, "completed", artifacts=artifacts, stats=stats)
    ledger["status"] = "completed"
    save_ledger(ledger_path, ledger)
    write_manifest(Path(ledger["run_dir"]), _final_manifest(ledger))
    return ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    plan = sub.add_parser("plan")
    plan.add_argument("--input", required=True)
    plan.add_argument("--output-dir", required=True)
    plan.add_argument("--run-id", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--default-operator-id", default=None)
    run.add_argument("--limit", type=int)
    run.add_argument("--force", action="store_true")
    run.add_argument("--no-cache", action="store_true", help="Do not reuse or populate the content-addressed local index cache")
    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", required=True)
    status = sub.add_parser("status")
    status.add_argument("--ledger", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "plan":
        rd = run_dir(Path(args.output_dir), args.run_id)
        ps = paths(rd)
        emit_json(
            {
                "run_dir": str(rd),
                "artifacts": {key: str(value) for key, value in ps.items()},
                "steps": STEPS,
                "dvc_scope_matrix": "ported local deterministic stages only",
                "disabled": ["remote writes", "network calls", "LLM spend", "embeddings"],
            }
        )
        return
    if args.cmd == "run":
        output_dir = Path(args.output_dir)
        rd = run_dir(output_dir, args.run_id)
        if rd.exists():
            if args.force:
                shutil.rmtree(rd)
            else:
                ledger_path = paths(rd)["ledger"]
                raise SystemExit(f"run directory already exists: {rd}. Use continue --ledger {ledger_path} or rerun with --force.")
        candidate = build_manifest(
            run_id=args.run_id,
            run_dir=rd,
            input_path=Path(args.input),
            default_operator_id=args.default_operator_id,
            limit=args.limit,
        )
        candidate_cache = cache_dir(output_dir, candidate)
        cached_manifest = read_manifest(candidate_cache)
        use_cache = not args.no_cache and not args.force
        if use_cache and manifest_ready(cached_manifest, candidate_cache):
            restore_cache(candidate_cache, rd)
            manifest = rewrite_materialized_metadata(
                rd,
                input_path=Path(args.input),
                run_id=args.run_id,
                default_operator_id=args.default_operator_id,
                limit=args.limit,
            )
            latest = promote_latest(rd, output_dir)
            ready = manifest_ready(manifest, rd)
            emit_json({"status": "completed", "ready": ready, "cache_hit": True, "cache_dir": str(candidate_cache), "run_dir": str(rd), "latest": latest if ready else {}, "manifest": str(manifest_path(rd)), "counts": {step: data.get("stats", {}) for step, data in manifest.get("stages", {}).items()}})
            return
        rd.mkdir(parents=True, exist_ok=True)
        ledger = default_ledger(Path(args.input), rd, args.run_id, args.default_operator_id, args.limit)
        ledger_path = paths(rd)["ledger"]
        save_ledger(ledger_path, ledger)
        ledger = execute(ledger_path)
        manifest = read_manifest(rd) or _final_manifest(ledger)
        ready = manifest_ready(manifest, rd)
        latest = promote_latest(rd, output_dir) if ready else {}
        cache_path = None
        if not args.no_cache and ready:
            cache_path = store_cache(rd, output_dir, manifest)
        emit_json({"status": ledger["status"], "ready": ready, "cache_hit": False, "cache_dir": str(cache_path) if cache_path else None, "run_dir": str(rd), "latest": latest, "manifest": str(manifest_path(rd)), "counts": {step["id"]: step.get("stats", {}) for step in ledger["steps"]}})
        return
    if args.cmd == "continue":
        ledger = execute(Path(args.ledger))
        rd = Path(ledger["run_dir"])
        manifest = read_manifest(rd) or _final_manifest(ledger)
        ready = manifest_ready(manifest, rd)
        latest = promote_latest(rd, rd.parent) if ready else {}
        emit_json({"status": ledger["status"], "ready": ready, "run_dir": ledger["run_dir"], "latest": latest, "manifest": str(manifest_path(rd))})
        return
    if args.cmd == "status":
        ledger = load_ledger(args.ledger)
        manifest = read_manifest(Path(args.ledger).parent)
        emit_json({"ledger": ledger, "manifest": manifest, "ready": manifest_ready(manifest, Path(args.ledger).parent)})
        return
    build_parser().error("subcommand required")


if __name__ == "__main__":
    main()
