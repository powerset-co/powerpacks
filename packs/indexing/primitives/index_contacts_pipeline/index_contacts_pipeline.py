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
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ACCOUNTS = Path(".powerpacks/ingestion/accounts.json")
DEFAULT_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/search-index")
DEFAULT_ARTIFACT_DIR = Path(".powerpacks/network-import/index/contacts")
DEFAULT_MANIFEST = DEFAULT_ARTIFACT_DIR / "manifest.json"
DEFAULT_AUTO_SPEND_LIMIT_USD = 10.0


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def run_json_command(cmd: list[str], *, timeout: int) -> tuple[int, dict[str, Any], str]:
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
        "--limit-mode",
        str(args.limit_mode),
        "--existing-duckdb",
        str(Path(args.output_dir) / "local-search.duckdb"),
    ]
    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])
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
        "--force",
    ]


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
                shutil.copy2(src, dst)
                promoted[f"merged_{name}"] = str(dst.relative_to(ROOT))

    duckdb = artifacts.get("duckdb")
    if duckdb:
        src = ROOT / Path(str(duckdb))
        if src.exists():
            dest_dir = ROOT / ".powerpacks/network-import/duckdb"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / "network.duckdb"
            shutil.copy2(src, dst)
            promoted["network_duckdb"] = str(dst.relative_to(ROOT))

    duckdb_manifest = artifacts.get("duckdb_manifest")
    if duckdb_manifest:
        src = ROOT / Path(str(duckdb_manifest))
        if src.exists():
            dest_dir = ROOT / ".powerpacks/network-import/duckdb"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / "manifest.json"
            shutil.copy2(src, dst)
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
    if args.include_existing_artifacts:
        candidates.append(base / "merged" / "people.csv")
    for source in ["gmail", "linkedin", "messages"]:
        manifest_people = read_manifest_people_csv(base / "import" / source / "manifest.json")
        if manifest_people:
            candidates.append(manifest_people)
        candidates.append(base / "import" / source / "people.csv")
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


def run_fan_in(args: argparse.Namespace, *, started_at: str | None = None) -> tuple[dict[str, Any], int]:
    started_at = started_at or now_iso()
    manifest_path = Path(args.manifest)
    inputs = fan_in_input_paths(args)
    if not inputs:
        payload = {
            "status": "not_ready",
            "stage": "index_contacts_pipeline",
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
        return payload, 0

    merge_cmd = merge_command(args, inputs)
    merge_code, merge_payload, merge_stderr = run_json_command(merge_cmd, timeout=60 * 60)
    if merge_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "step": "merge_network_sources",
            "command": command_text(merge_cmd),
            "inputs": [str(path) for path in inputs],
            "merge": merge_payload,
            "error": tail(merge_stderr) or merge_payload,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        return payload, 1

    duck_cmd = network_duckdb_command(args)
    duck_code, duck_payload, duck_stderr = run_json_command(duck_cmd, timeout=60 * 60)
    if duck_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
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
        return payload, 1

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
        "step": "fan_in",
        "artifact_dir": str(args.artifact_dir),
        "inputs": [str(path) for path in inputs],
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


def estimate_has_known_pricing(estimate: dict[str, Any]) -> bool:
    costs = estimate.get("estimated_costs") if isinstance(estimate.get("estimated_costs"), dict) else {}
    return bool(costs.get("known_pricing", True))


def write_manifest(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
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


def run_pipeline(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = now_iso()
    manifest_path = Path(args.manifest)

    fan_in_payload, fan_in_code = run_fan_in(args, started_at=started_at)
    if fan_in_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
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
            "reason": "missing_people_csv",
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "missing": [str(args.people_csv)],
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        return payload, 0

    preflight_duckdb = maybe_materialize_existing_records(args)
    if preflight_duckdb.get("status") == "failed":
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
            "step": "local_duckdb_preflight",
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "local_duckdb": preflight_duckdb,
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        return payload, 1

    estimate_code, estimate, estimate_stderr = run_json_command(processing_args(args, dry_run=True, allow_paid=False), timeout=60 * 60)
    if estimate_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
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
        return payload, 1

    paid_calls = estimated_paid_calls(estimate)
    total_cost = estimated_cost_usd(estimate)
    known_pricing = estimate_has_known_pricing(estimate)
    spend_limit = float(args.auto_spend_limit_usd)
    needs_paid_approval = paid_calls > 0 and (not known_pricing or total_cost is None or total_cost >= spend_limit)
    if total_cost is not None and total_cost >= spend_limit:
        needs_paid_approval = True
    if needs_paid_approval and not args.approve_provider_spend:
        payload = {
            "status": "blocked_approval",
            "stage": "index_contacts_pipeline",
            "step": "index_processing",
            "reason": "estimated_processing_cost_requires_approval",
            "people_csv": str(args.people_csv),
            "people_sha256": sha256_file(people_path),
            "auto_spend_limit_usd": spend_limit,
            "estimated_cost_usd": total_cost,
            "estimated_paid_calls": estimate.get("estimated_paid_calls", {}),
            "processing_estimate": estimate,
            "fan_in": fan_in_payload,
            "promoted": promoted,
            "preflight_duckdb": preflight_duckdb,
            "approve_command": command_text(processing_args(args, dry_run=False, allow_paid=True)),
            "started_at": started_at,
            "updated_at": now_iso(),
        }
        write_manifest(manifest_path, payload)
        return payload, 20

    allow_paid = bool(args.approve_provider_spend or paid_calls > 0 or (total_cost and total_cost > 0))
    process_code, processing, processing_stderr = run_json_command(processing_args(args, dry_run=False, allow_paid=allow_paid), timeout=6 * 60 * 60)
    if process_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
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
        return payload, 1

    duckdb_code, duckdb_payload, duckdb_stderr = run_json_command(duckdb_command(args), timeout=60 * 60)
    if duckdb_code != 0:
        payload = {
            "status": "failed",
            "stage": "index_contacts_pipeline",
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
        return payload, 1

    payload = {
        "status": "ready",
        "stage": "index_contacts_pipeline",
        "people_csv": str(args.people_csv),
        "people_sha256": sha256_file(people_path),
        "output_dir": str(args.output_dir),
        "duckdb": duckdb_payload.get("duckdb", str(Path(args.output_dir) / "local-search.duckdb")) if isinstance(duckdb_payload, dict) else str(Path(args.output_dir) / "local-search.duckdb"),
        "manifest": str(manifest_path),
        "auto_spend_limit_usd": spend_limit,
        "auto_approved_paid_calls": paid_calls if allow_paid else 0,
        "provider_spend_approved": bool(args.approve_provider_spend),
        "estimated_cost_usd": total_cost,
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
        "artifact_dir": str(args.artifact_dir),
        "manifest": str(args.manifest),
        "people_csv": str(args.people_csv),
        "output_dir": str(args.output_dir),
        "fan_in_inputs": [str(path) for path in inputs],
        "commands": {
            "fan_in_merge": command_text(merge_command(args, inputs)) if inputs else "",
            "fan_in_network_duckdb": command_text(network_duckdb_command(args)),
            "processing_dry_run": command_text(processing_args(args, dry_run=True, allow_paid=False)),
            "processing_run": command_text(processing_args(args, dry_run=False, allow_paid=False)),
            "processing_run_approved": command_text(processing_args(args, dry_run=False, allow_paid=True)),
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
        s.add_argument("--input", action="append", default=[], help="Additional people.csv input to include in fan-in.")
        s.add_argument("--include-existing-artifacts", action=argparse.BooleanOptionalAction, default=True)
        s.add_argument("--limit", type=int)
        s.add_argument("--limit-mode", choices=["all", "missing"], default="missing")
        s.add_argument("--auto-spend-limit-usd", type=float, default=DEFAULT_AUTO_SPEND_LIMIT_USD)
        s.add_argument("--approve-provider-spend", action="store_true")

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
    emit(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
