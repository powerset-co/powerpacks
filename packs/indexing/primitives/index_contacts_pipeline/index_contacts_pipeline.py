#!/usr/bin/env python3
"""Fan in confirmed network people and build the local contacts search index.

This is the stage-owned indexing entrypoint for local setup/app flows:

1. fan in source-specific import outputs into the canonical merged people.csv
2. run the processing/indexing pipeline against that canonical people.csv
3. materialize the local DuckDB search database

The lower-level record builders stay in build_processing_pipeline.py. This
wrapper owns orchestration and writes a stable stage manifest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
DEFAULT_ACCOUNTS = Path(".powerpacks/ingestion/accounts.json")
DEFAULT_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/search-index")
DEFAULT_ARTIFACT_DIR = Path(".powerpacks/network-import/index/contacts")
DEFAULT_MANIFEST = DEFAULT_ARTIFACT_DIR / "manifest.json"
CANONICAL_MERGED_PEOPLE_CSV = ".powerpacks/network-import/merged/people.csv"
ProgressCallback = Callable[[str, str, str, dict[str, Any] | None], None]

from packs.indexing.lib.openai_usage_tiers import (  # noqa: E402
    OPENAI_USAGE_TIER_PROFILES,
    openai_usage_tier_profile,
)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def progress(message: str) -> None:
    print(f"[index-contacts] {message}", file=sys.stderr, flush=True)


def notify_progress(progress_callback: ProgressCallback | None, stage_id: str, message: str, *, status: str = "running", payload: dict[str, Any] | None = None) -> None:
    if progress_callback:
        progress_callback(stage_id, message, status, payload or {})


def selected_openai_usage_tier(args: argparse.Namespace) -> dict[str, Any]:
    return openai_usage_tier_profile(getattr(args, "openai_usage_tier", None))


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {"path": str(path), "exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha256_file(path)}


def input_fingerprints(paths: list[Path]) -> dict[str, Any]:
    return {str(path): file_fingerprint(ROOT / path if not path.is_absolute() else path) for path in paths}


def payload_without_volatile_timestamps(payload: dict[str, Any]) -> dict[str, Any]:
    stable = dict(payload)
    stable.pop("updated_at", None)
    stable.pop("started_at", None)
    return stable


def copy_if_changed(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.is_file() and src.stat().st_size == dst.stat().st_size and sha256_file(src) == sha256_file(dst):
        return False
    shutil.copy2(src, dst)
    return True


def count_csv_rows(path: str | Path) -> int:
    target = ROOT / Path(path)
    if not target.exists():
        return 0
    with target.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def parse_json_fragment(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        pass
    for idx, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            parsed = json.loads(stripped[idx:])
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            continue
    return None


def run_json_command(cmd: list[str], *, timeout: int, stream_stderr: bool = False) -> tuple[int, dict[str, Any], str]:
    if stream_stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr_parts: list[str] = []
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                stderr_parts.append(line)
                print(line, end="", file=sys.stderr, flush=True)
            stdout = proc.stdout.read() if proc.stdout is not None else ""
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            stderr_parts.append(stderr or "")
            return 124, parse_json_fragment(stdout or "") or {}, "".join(stderr_parts)
        payload = parse_json_fragment(stdout or "") or {}
        return returncode, payload, "".join(stderr_parts)

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    payload = parse_json_fragment(proc.stdout) or {}
    return proc.returncode, payload, proc.stderr


def command_text(cmd: list[str]) -> str:
    return " ".join(cmd)


def merge_command(args: argparse.Namespace, input_paths: list[Path]) -> list[str]:
    cmd = [
        sys.executable,
        "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py",
        "run",
        "--output-dir",
        str(Path(args.artifact_dir) / "merged"),
    ]
    for input_path in input_paths:
        cmd.extend(["--input", str(input_path)])
    return cmd


def network_duckdb_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py",
        "--network-dir",
        str(Path(args.artifact_dir) / "merged"),
        "--output-dir",
        str(Path(args.artifact_dir) / "duckdb"),
        "--flavor",
        "local",
        "--force",
    ]


def processing_args(args: argparse.Namespace, *, dry_run: bool, allow_paid: bool) -> list[str]:
    cmd = [
        sys.executable,
        "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py",
        "run",
        "--input",
        str(args.people_csv),
        "--output-dir",
        str(args.output_dir),
        "--default-operator-id",
        str(args.operator_id),
    ]
    usage_tier = selected_openai_usage_tier(args)["tier"]
    cmd.extend(["--openai-usage-tier", usage_tier])
    if dry_run:
        cmd.append("--dry-run")
    if allow_paid:
        cmd.extend(["--allow-paid-role-provider", "--allow-paid-embeddings", "--allow-paid-company-provider"])
    return cmd


def duckdb_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "scripts/build-local-duckdb-shim.py",
        "--records-dir",
        str(args.output_dir),
        "--operator-id",
        str(args.operator_id),
        "--incremental",
    ]


def local_search_duckdb_path(args: argparse.Namespace) -> Path:
    return ROOT / Path(args.output_dir) / "local-search.duckdb"


def duckdb_input_paths(args: argparse.Namespace) -> list[Path]:
    output_dir = ROOT / Path(args.output_dir)
    candidates = [
        ROOT / Path(args.people_csv),
        output_dir / "unified/person_hashes.json",
        output_dir / "records/person_profiles.records.jsonl",
        output_dir / "records/people.records.jsonl",
        output_dir / "records/summaries.records.jsonl",
        output_dir / "records/companies.records.jsonl",
        output_dir / "records/education.records.jsonl",
        output_dir / "records/schools.records.jsonl",
        output_dir / "records/people.records.hashes.json",
        output_dir / "records/summaries.records.hashes.json",
        output_dir / "records/companies.records.hashes.json",
    ]
    return [path for path in candidates if path.exists()]


def duckdb_current_for_processing_hashes(args: argparse.Namespace) -> bool:
    duckdb = local_search_duckdb_path(args)
    if not duckdb.exists() or duckdb.stat().st_size <= 1024:
        return False
    duckdb_mtime = duckdb.stat().st_mtime_ns
    for input_path in duckdb_input_paths(args):
        if duckdb_mtime < input_path.stat().st_mtime_ns:
            return False
    return True


def duckdb_freshness_payload(args: argparse.Namespace) -> dict[str, Any]:
    duckdb = local_search_duckdb_path(args)
    if not duckdb.exists() or duckdb.stat().st_size <= 1024:
        return {"current_for_processing_hashes": False, "reason": "missing_or_small_duckdb", "checked_inputs": []}
    duckdb_mtime = duckdb.stat().st_mtime_ns
    inputs = duckdb_input_paths(args)
    stale = [str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in inputs if duckdb_mtime < path.stat().st_mtime_ns]
    if stale:
        return {"current_for_processing_hashes": False, "reason": "stale_duckdb_inputs", "stale_inputs": stale, "checked_inputs": len(inputs)}
    return {"current_for_processing_hashes": True, "checked_inputs": len(inputs)}


def promote_network_artifacts(artifacts: dict[str, Any]) -> dict[str, str]:
    promoted: dict[str, str] = {}
    merged_people = artifacts.get("merged_people_csv")
    if merged_people:
        source_dir = ROOT / Path(str(merged_people))
        source_dir = source_dir.parent
        dest_dir = ROOT / ".powerpacks/network-import/merged"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in [
            "people.csv",
            "people_harmonic_all.merged.csv",
            "network_contacts.csv",
            "network_contact_sources.csv",
            "network_companies.csv",
            "merge_manifest.json",
            "possible_duplicates_review.csv",
        ]:
            src = source_dir / name
            if src.exists():
                dst = dest_dir / name
                copy_if_changed(src, dst)
                promoted[f"merged_{name}"] = str(dst.relative_to(ROOT))

    duckdb = artifacts.get("duckdb")
    if duckdb:
        src = ROOT / Path(str(duckdb))
        if src.exists():
            dest_dir = ROOT / ".powerpacks/network-import/duckdb"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / "network.duckdb"
            copy_if_changed(src, dst)
            promoted["network_duckdb"] = str(dst.relative_to(ROOT))

    duckdb_manifest = artifacts.get("duckdb_manifest")
    if duckdb_manifest:
        src = ROOT / Path(str(duckdb_manifest))
        if src.exists():
            dest_dir = ROOT / ".powerpacks/network-import/duckdb"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / "manifest.json"
            copy_if_changed(src, dst)
            promoted["network_duckdb_manifest"] = str(dst.relative_to(ROOT))
    return promoted


def read_manifest_people_csv(path: Path) -> Path | None:
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
    value = outputs.get("people_csv")
    if not value:
        return None
    candidate = ROOT / Path(str(value))
    return candidate if candidate.exists() else None


def fan_in_input_paths(args: argparse.Namespace) -> list[Path]:
    base = ROOT / ".powerpacks/network-import"
    candidates: list[Path] = []
    source_candidates: list[Path] = []
    expected_source_people = [base / "import" / source / "people.csv" for source in ["gmail", "linkedin", "messages"]]
    for source in ["gmail", "linkedin", "messages"]:
        manifest_people = read_manifest_people_csv(base / "import" / source / "manifest.json")
        if manifest_people:
            source_candidates.append(manifest_people)
        source_candidates.append(base / "import" / source / "people.csv")
    source_inputs = [path for path in source_candidates if path.exists()]
    all_expected_sources_exist = all(path.exists() for path in expected_source_people)
    candidates.extend(source_inputs)
    if args.include_existing_artifacts and not all_expected_sources_exist:
        candidates.append(base / "merged" / "people.csv")
    for path in getattr(args, "input", []) or []:
        candidates.append(ROOT / Path(str(path)))
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = path.resolve()
        if not path.exists() or str(resolved) in seen:
            continue
        seen.add(str(resolved))
        try:
            out.append(path.relative_to(ROOT))
        except ValueError:
            out.append(path)
    return out


def fan_in_fingerprints_match(existing: Any, current: dict[str, Any]) -> bool:
    if existing == current:
        return True
    if not isinstance(existing, dict):
        return False
    existing_keys = set(existing)
    current_keys = set(current)
    extra_existing = existing_keys - current_keys
    if extra_existing and extra_existing != {CANONICAL_MERGED_PEOPLE_CSV}:
        return False
    return all(existing.get(key) == value for key, value in current.items())


def run_fan_in(args: argparse.Namespace, *, started_at: str | None = None, progress_callback: ProgressCallback | None = None) -> tuple[dict[str, Any], int]:
    started_at = started_at or now_iso()
    manifest_path = Path(args.manifest)
    inputs = fan_in_input_paths(args)
    fingerprints = input_fingerprints(inputs)
    existing = status_payload(argparse.Namespace(manifest=str(manifest_path)))
    existing_fan_in = existing if existing.get("step") == "fan_in" else existing.get("fan_in") if isinstance(existing.get("fan_in"), dict) else {}
    existing_artifacts = existing_fan_in.get("artifacts") if isinstance(existing_fan_in.get("artifacts"), dict) else {}
    existing_promoted = existing_fan_in.get("promoted") if isinstance(existing_fan_in.get("promoted"), dict) else {}
    if (
        existing_fan_in.get("status") == "completed"
        and existing_fan_in.get("step") == "fan_in"
        and fan_in_fingerprints_match(existing_fan_in.get("input_fingerprints"), fingerprints)
        and existing_artifacts.get("merged_people_csv")
        and (ROOT / Path(str(existing_artifacts.get("merged_people_csv")))).exists()
        and existing_promoted.get("network_duckdb")
        and (ROOT / Path(str(existing_promoted.get("network_duckdb")))).exists()
    ):
        payload = {
            **existing_fan_in,
            "openai_usage_tier": selected_openai_usage_tier(args),
            "noop": True,
            "reason": "fan_in_inputs_unchanged",
        }
        notify_progress(progress_callback, "merge_network", "Source people merge is current", status="completed", payload=payload.get("merge") if isinstance(payload.get("merge"), dict) else payload)
        notify_progress(progress_callback, "network_duckdb", "Contact lookup database is current", status="completed", payload=payload.get("network_duckdb") if isinstance(payload.get("network_duckdb"), dict) else payload)
        return payload, 0
    if not inputs:
        payload = {
            "status": "not_ready",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "fan_in",
            "reason": "missing_import_people_csvs",
            "inputs": [],
            "searched": [
                ".powerpacks/network-import/import/gmail/people.csv",
                ".powerpacks/network-import/import/linkedin/people.csv",
                ".powerpacks/network-import/import/messages/people.csv",
            ],
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "merge_network", "No source people CSVs found to merge", status="failed", payload=payload)
        return payload, 0

    merge_cmd = merge_command(args, inputs)
    notify_progress(progress_callback, "merge_network", "Merging source people CSVs", payload={"inputs": [str(path) for path in inputs]})
    merge_code, merge_payload, merge_stderr = run_json_command(merge_cmd, timeout=60 * 60)
    if merge_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "merge_network_sources",
            "command": command_text(merge_cmd),
            "inputs": [str(path) for path in inputs],
            "merge": merge_payload,
            "error": tail(merge_stderr) or merge_payload,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "merge_network", "Source people merge failed", status="failed", payload=payload)
        return payload, 1
    notify_progress(progress_callback, "merge_network", "Source people CSVs are merged", status="completed", payload=merge_payload)

    duck_cmd = network_duckdb_command(args)
    notify_progress(progress_callback, "network_duckdb", "Preparing contact lookup database from merged contacts", payload={"people_csv": merge_payload.get("people_csv")})
    duck_code, duck_payload, duck_stderr = run_json_command(duck_cmd, timeout=60 * 60)
    if duck_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "network_duckdb",
            "command": command_text(duck_cmd),
            "inputs": [str(path) for path in inputs],
            "merge": merge_payload,
            "network_duckdb": duck_payload,
            "error": tail(duck_stderr) or duck_payload,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "network_duckdb", "Contact lookup database build failed", status="failed", payload=payload)
        return payload, 1
    notify_progress(progress_callback, "network_duckdb", "Contact lookup database is ready", status="completed", payload=duck_payload)

    artifacts = {
        "merged_people_csv": merge_payload.get("people_csv"),
        "network_contacts_csv": merge_payload.get("network_contacts_csv"),
        "network_contact_sources_csv": merge_payload.get("network_contact_sources_csv"),
        "network_companies_csv": merge_payload.get("network_companies_csv"),
        "merge_manifest": merge_payload.get("manifest"),
        "duckdb": duck_payload.get("duckdb"),
        "duckdb_manifest": duck_payload.get("manifest"),
    }
    promoted = promote_network_artifacts(artifacts)
    payload = {
        "status": "completed",
        "stage": "index_contacts_pipeline",
        "openai_usage_tier": selected_openai_usage_tier(args),
        "step": "fan_in",
        "artifact_dir": str(args.artifact_dir),
        "inputs": [str(path) for path in inputs],
        "input_fingerprints": fingerprints,
        "artifacts": artifacts,
        "promoted": promoted,
        "merge": merge_payload,
        "network_duckdb": duck_payload,
        "people_csv": str(args.people_csv),
        "people_sha256": sha256_file(ROOT / Path(args.people_csv)) if (ROOT / Path(args.people_csv)).exists() else "",
        "started_at": started_at,
        "updated_at": now_iso(),
    }
    write_manifest(manifest_path, payload)
    return payload, 0


def estimated_paid_calls(estimate: dict[str, Any]) -> int:
    paid = estimate.get("estimated_paid_calls") if isinstance(estimate.get("estimated_paid_calls"), dict) else {}
    total = 0
    for value in paid.values():
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def estimated_cost_usd(estimate: dict[str, Any]) -> float | None:
    costs = estimate.get("estimated_costs") if isinstance(estimate.get("estimated_costs"), dict) else {}
    value = estimate.get("estimated_cost_usd")
    if value is None:
        value = costs.get("total_estimated_usd")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    estimate = payload.get("processing_estimate") if isinstance(payload.get("processing_estimate"), dict) else {}
    counts = estimate.get("counts") if isinstance(estimate.get("counts"), dict) else {}
    fan_in = payload.get("fan_in") if isinstance(payload.get("fan_in"), dict) else {}
    merge = fan_in.get("merge") if isinstance(fan_in.get("merge"), dict) else {}
    local_duckdb = payload.get("local_duckdb") if isinstance(payload.get("local_duckdb"), dict) else {}
    tables = local_duckdb.get("tables") if isinstance(local_duckdb.get("tables"), dict) else {}
    standard_tables = [
        "local_person_profiles",
        "local_people_positions",
        "local_summaries",
        "local_people_education",
        "local_education",
        "local_companies",
    ]
    merged_people_csv = str((fan_in.get("artifacts") if isinstance(fan_in.get("artifacts"), dict) else {}).get("merged_people_csv") or "")
    fan_in_people_rows = int(merge.get("people_rows") or merge.get("output_rows") or 0)
    if not fan_in_people_rows and merged_people_csv:
        fan_in_people_rows = count_csv_rows(merged_people_csv)
    summary: dict[str, Any] = {
        "status": payload.get("status"),
        "step": payload.get("step") or "",
        "people_csv": payload.get("people_csv") or "",
        "people": int(counts.get("total_people") or counts.get("people") or merge.get("people_rows") or merge.get("output_rows") or 0),
        "pending_people_before_run": int(counts.get("pending_people") or counts.get("people") or 0),
        "estimated_cost_usd": estimated_cost_usd(estimate) or payload.get("estimated_cost_usd") or 0.0,
        "estimated_paid_calls": estimated_paid_calls(estimate),
        "duckdb": payload.get("duckdb") or local_duckdb.get("duckdb") or "",
        "manifest": payload.get("manifest") or str(DEFAULT_MANIFEST),
    }
    if merge:
        summary["fan_in"] = {
            "input_rows": int(merge.get("input_rows") or 0),
            "people_rows": fan_in_people_rows,
            "company_rows": int(merge.get("company_rows") or 0),
        }
    if tables:
        summary["duckdb_tables"] = {key: tables[key] for key in standard_tables if key in tables}
    if local_duckdb.get("duckdb_update_mode"):
        summary["duckdb_update_mode"] = local_duckdb.get("duckdb_update_mode")
    table_diffs = local_duckdb.get("table_diffs") if isinstance(local_duckdb.get("table_diffs"), dict) else {}
    if table_diffs:
        summary["duckdb_table_diffs"] = {
            table: {
                key: diff.get(key)
                for key in ["mode", "inserted_rows", "updated_rows", "deleted_rows", "unchanged_rows", "old_hashes_present"]
                if key in diff
            }
            for table, diff in table_diffs.items()
            if isinstance(diff, dict)
        }
    if payload.get("error"):
        summary["error"] = payload.get("error")
    return summary


def write_manifest(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and payload_without_volatile_timestamps(existing) == payload_without_volatile_timestamps(payload):
            return existing
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def maybe_materialize_existing_records(args: argparse.Namespace) -> dict[str, Any]:
    records = ROOT / Path(args.output_dir) / "records"
    people_records = records / "people.records.jsonl"
    duckdb = ROOT / Path(args.output_dir) / "local-search.duckdb"
    if not people_records.exists() or people_records.stat().st_size <= 0:
        return {"status": "skipped", "reason": "missing_records"}
    if duckdb.exists() and duckdb.stat().st_size > 1024:
        return {"status": "skipped", "reason": "duckdb_exists", "duckdb": str(duckdb.relative_to(ROOT))}
    code, payload, stderr = run_json_command(duckdb_command(args), timeout=60 * 60)
    if code != 0:
        return {"status": "failed", "step": "local_duckdb", "payload": payload, "error": tail(stderr)}
    return {"status": "completed", "payload": payload}


def run_pipeline(args: argparse.Namespace, progress_callback: ProgressCallback | None = None) -> tuple[dict[str, Any], int]:
    started_at = now_iso()
    manifest_path = Path(args.manifest)

    fan_in_payload, fan_in_code = run_fan_in(args, started_at=started_at, progress_callback=progress_callback)
    if fan_in_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "fan_in",
            "fan_in": fan_in_payload,
            "error": fan_in_payload.get("error") or fan_in_payload,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        return payload, 1

    promoted = fan_in_payload.get("promoted") if isinstance(fan_in_payload.get("promoted"), dict) else {}
    people_path = ROOT / Path(args.people_csv)
    if not people_path.exists():
        payload = {
            "status": "not_ready",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "reason": "missing_people_csv",
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "missing": [str(args.people_csv)],
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "index_estimate", "Merged people CSV is missing", status="failed", payload=payload)
        return payload, 0

    progress("preflight: checking existing local records")
    preflight_duckdb = maybe_materialize_existing_records(args)
    if preflight_duckdb.get("status") == "failed":
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "local_duckdb_preflight",
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "local_duckdb": preflight_duckdb,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "search_duckdb", "Existing local search database materialization failed", status="failed", payload=payload)
        return payload, 1

    progress("processing: dry-run incremental estimate")
    notify_progress(progress_callback, "index_estimate", "Estimating local index work", payload={"people_csv": str(args.people_csv)})
    estimate_code, estimate, estimate_stderr = run_json_command(processing_args(args, dry_run=True, allow_paid=False), timeout=60 * 60)
    if estimate_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "processing_dry_run",
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "preflight_duckdb": preflight_duckdb,
            "processing_estimate": estimate,
            "error": tail(estimate_stderr) or estimate,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "index_estimate", "Local index estimate failed", status="failed", payload=payload)
        return payload, 1
    notify_progress(progress_callback, "index_estimate", "Local index work is estimated", status="completed", payload=estimate)

    paid_calls = estimated_paid_calls(estimate)
    total_cost = estimated_cost_usd(estimate)
    counts = estimate.get("counts") if isinstance(estimate.get("counts"), dict) else {}
    pending_people = int(counts.get("pending_people") or counts.get("people") or 0)
    existing_duckdb = local_search_duckdb_path(args)
    duckdb_current = duckdb_current_for_processing_hashes(args)
    if pending_people == 0 and paid_calls == 0 and duckdb_current:
        payload = {
            "status": "ready",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "noop",
            "reason": "processing_outputs_complete",
            "people_csv": str(args.people_csv),
            "people_sha256": sha256_file(people_path),
            "output_dir": str(args.output_dir),
            "duckdb": str(existing_duckdb.relative_to(ROOT)),
            "manifest": str(manifest_path),
            "estimated_cost_usd": total_cost,
            "estimated_paid_calls": estimate.get("estimated_paid_calls", {}),
            "processing_estimate": estimate,
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "preflight_duckdb": preflight_duckdb,
            "duckdb_freshness": duckdb_freshness_payload(args),
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "index_records", "Local search records are current", status="completed", payload={"reason": "processing_outputs_complete"})
        notify_progress(progress_callback, "search_duckdb", "Local search database is current", status="completed", payload={"duckdb": str(existing_duckdb.relative_to(ROOT))})
        return payload, 0
    if pending_people == 0 and paid_calls == 0:
        progress("duckdb: refreshing local search tables from current records")
        notify_progress(progress_callback, "index_records", "Local search records are current", status="completed", payload={"reason": "processing_outputs_complete"})
        notify_progress(progress_callback, "search_duckdb", "Refreshing local search database tables", payload={"people_csv": str(args.people_csv)})
        duckdb_code, duckdb_payload, duckdb_stderr = run_json_command(duckdb_command(args), timeout=60 * 60)
        if duckdb_code != 0:
            payload = {
                "status": "failed",
                "stage": "index_contacts_pipeline",
                "openai_usage_tier": selected_openai_usage_tier(args),
                "step": "local_duckdb_refresh",
                "people_csv": str(args.people_csv),
                "processing_estimate": estimate,
                "fan_in": fan_in_payload,
                "promoted": promoted,
                "preflight_duckdb": preflight_duckdb,
                "local_duckdb": duckdb_payload,
                "duckdb_freshness": duckdb_freshness_payload(args),
                "error": tail(duckdb_stderr) or duckdb_payload,
                "started_at": started_at,
                "updated_at": now_iso(),
            }
            write_manifest(manifest_path, payload)
            notify_progress(progress_callback, "search_duckdb", "Local search database refresh failed", status="failed", payload=payload)
            return payload, 1
        payload = {
            "status": "ready",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "local_duckdb_refresh",
            "reason": "processing_outputs_complete_duckdb_refreshed",
            "people_csv": str(args.people_csv),
            "people_sha256": sha256_file(people_path),
            "output_dir": str(args.output_dir),
            "duckdb": duckdb_payload.get("duckdb", str(Path(args.output_dir) / "local-search.duckdb")) if isinstance(duckdb_payload, dict) else str(Path(args.output_dir) / "local-search.duckdb"),
            "manifest": str(manifest_path),
            "estimated_cost_usd": total_cost,
            "estimated_paid_calls": estimate.get("estimated_paid_calls", {}),
            "processing_estimate": estimate,
            "local_duckdb": duckdb_payload,
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "preflight_duckdb": preflight_duckdb,
            "duckdb_freshness": duckdb_freshness_payload(args),
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "search_duckdb", "Local search database is ready", status="completed", payload=duckdb_payload if isinstance(duckdb_payload, dict) else {})
        return payload, 0
    allow_paid = bool(paid_calls > 0 or (total_cost and total_cost > 0))
    progress("processing: running fixed-output incremental pipeline")
    notify_progress(progress_callback, "index_records", "Building local search records", payload={"pending_people": pending_people, "estimated_paid_calls": estimate.get("estimated_paid_calls", {})})
    process_code, processing, processing_stderr = run_json_command(
        processing_args(args, dry_run=False, allow_paid=allow_paid),
        timeout=6 * 60 * 60,
        stream_stderr=True,
    )
    if process_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "index_processing",
            "people_csv": str(args.people_csv),
            "processing_estimate": estimate,
            "processing": processing,
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "error": tail(processing_stderr) or processing,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "index_records", "Local search record build failed", status="failed", payload=payload)
        return payload, 1
    notify_progress(progress_callback, "index_records", "Local search records are built", status="completed", payload=processing)

    progress("duckdb: materializing local search tables")
    notify_progress(progress_callback, "search_duckdb", "Updating local search database", payload={"people_csv": str(args.people_csv)})
    duckdb_code, duckdb_payload, duckdb_stderr = run_json_command(duckdb_command(args), timeout=60 * 60)
    if duckdb_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "openai_usage_tier": selected_openai_usage_tier(args),
            "step": "local_duckdb",
            "people_csv": str(args.people_csv),
            "processing_estimate": estimate,
            "processing": processing,
            "local_duckdb": duckdb_payload,
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "error": tail(duckdb_stderr) or duckdb_payload,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        notify_progress(progress_callback, "search_duckdb", "Local search database update failed", status="failed", payload=payload)
        return payload, 1

    payload = {
        "status": "ready",
        "stage": "index_contacts_pipeline",
        "openai_usage_tier": selected_openai_usage_tier(args),
        "people_csv": str(args.people_csv),
        "people_sha256": sha256_file(people_path),
        "output_dir": str(args.output_dir),
        "duckdb": duckdb_payload.get("duckdb", str(Path(args.output_dir) / "local-search.duckdb")) if isinstance(duckdb_payload, dict) else str(Path(args.output_dir) / "local-search.duckdb"),
        "manifest": str(manifest_path),
        "estimated_cost_usd": total_cost,
        "estimated_paid_calls": estimate.get("estimated_paid_calls", {}),
        "processing_estimate": estimate,
        "processing": processing,
        "local_duckdb": duckdb_payload,
        "fan_in": fan_in_payload,
        "promoted": promoted,
        "preflight_duckdb": preflight_duckdb,
        "started_at": started_at,
        "updated_at": now_iso(),
    }
    write_manifest(manifest_path, payload)
    notify_progress(progress_callback, "search_duckdb", "Local search database is ready", status="completed", payload=duckdb_payload if isinstance(duckdb_payload, dict) else {})
    return payload, 0


def status_payload(args: argparse.Namespace) -> dict[str, Any]:
    manifest = ROOT / Path(args.manifest)
    if not manifest.exists():
        return {"status": "missing", "manifest": str(args.manifest)}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"status": "failed", "manifest": str(args.manifest), "error": str(exc)}
    return payload if isinstance(payload, dict) else {"status": "failed", "manifest": str(args.manifest), "error": "manifest is not an object"}


def plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    inputs = fan_in_input_paths(args)
    return {
        "status": "plan",
        "stage": "index_contacts_pipeline",
        "openai_usage_tier": selected_openai_usage_tier(args),
        "artifact_dir": str(args.artifact_dir),
        "manifest": str(args.manifest),
        "people_csv": str(args.people_csv),
        "output_dir": str(args.output_dir),
        "fan_in_inputs": [str(path) for path in inputs],
        "commands": {
            "fan_in_merge": command_text(merge_command(args, inputs)) if inputs else "",
            "fan_in_network_duckdb": command_text(network_duckdb_command(args)),
            "processing_dry_run": command_text(processing_args(args, dry_run=True, allow_paid=False)),
            "processing_run": command_text(processing_args(args, dry_run=False, allow_paid=True)),
            "local_duckdb": command_text(duckdb_command(args)),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(s: argparse.ArgumentParser) -> None:
        s.add_argument("--operator-id", default="local")
        s.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS))
        s.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
        s.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
        s.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
        s.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
        s.add_argument("--openai-usage-tier", choices=sorted(OPENAI_USAGE_TIER_PROFILES), default=None)
        s.add_argument("--input", action="append", default=[], help="Additional people.csv input to include in fan-in.")
        s.add_argument("--include-existing-artifacts", action=argparse.BooleanOptionalAction, default=True)

    run = sub.add_parser("run")
    add_common(run)
    run.set_defaults(func=lambda args: run_pipeline(args))

    plan = sub.add_parser("plan")
    add_common(plan)
    plan.set_defaults(func=lambda args: (plan_payload(args), 0))

    fan_in = sub.add_parser("fan-in")
    add_common(fan_in)
    fan_in.set_defaults(func=lambda args: run_fan_in(args))

    status = sub.add_parser("status")
    status.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    status.set_defaults(func=lambda args: (status_payload(args), 0))

    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload, code = args.func(args)
    emit(compact_run_payload(payload) if args.cmd == "run" else payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
