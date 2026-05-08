#!/usr/bin/env python3
"""Sync operator-scoped messages deep-research cache from the processing GCS bucket.

This primitive is intentionally read-mostly: it downloads the server-side
messages research profile cache into the local Powerpacks artifact layout so
`deep_research_contacts` can skip handles that were already researched.

Default local layout:
  .powerpacks/messages/research/<handle>/01_research_parallel.json
  .powerpacks/messages/research_cache/output/<operator_id>/phone_contacts_to_enrich.csv

Remote layout mirrors network-search-api's processing mount:
  gs://powerset-search-processing-artifacts/data/messages_research_profiles/<operator_id>/
  gs://powerset-search-processing-artifacts/pipeline_output/messages_research/<operator_id>/

Stdlib-only; shells out to `gcloud storage rsync` (or gsutil when requested).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_API_URL = "https://search-api-7wk4uhe77q-uw.a.run.app"
DEFAULT_BUCKET = "powerset-search-processing-artifacts"
DEFAULT_PROFILES_DIR = Path(".powerpacks/messages/research")
DEFAULT_OUTPUT_ROOT = Path(".powerpacks/messages/research_cache/output")
DEFAULT_MANIFEST = Path(".powerpacks/messages/research_cache/sync_manifest.json")


class SyncError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def auth_token_from_powerpacks() -> str:
    auth_py = repo_root() / "packs/powerset/primitives/auth/auth.py"
    result = subprocess.run(
        [sys.executable, str(auth_py), "token", "--bearer-only"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip()
        raise SyncError("could not get Powerset token; run `$powerset login` first" + (f": {detail}" if detail else ""))
    token = result.stdout.strip()
    if not token:
        raise SyncError("Powerset auth primitive returned an empty token")
    return token


def api_get_json(api_url: str, path: str, token: str, *, query: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    url = api_url.rstrip("/") + path
    if query:
        cleaned = {k: v for k, v in query.items() if v not in (None, "")}
        if cleaned:
            url += "?" + urllib.parse.urlencode(cleaned)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SyncError(f"API request failed ({exc.code}): {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"API request failed: {exc}") from exc


def resolve_skip_status(args: argparse.Namespace) -> dict[str, Any]:
    token = args.token or auth_token_from_powerpacks()
    return api_get_json(
        args.api_url,
        "/v2/messages-research/skip-status",
        token,
        query={"operator_id": args.operator_id},
        timeout=args.timeout,
    )


def count_local_profiles(profiles_dir: Path) -> dict[str, int]:
    if not profiles_dir.exists():
        return {"profile_dirs": 0, "research_parallel_json": 0, "final_profile_json": 0, "network_review_json": 0, "legacy_network_review_json": 0}
    return {
        "profile_dirs": sum(1 for p in profiles_dir.iterdir() if p.is_dir()),
        "research_parallel_json": sum(1 for _ in profiles_dir.glob("*/01_research_parallel.json")),
        "final_profile_json": sum(1 for _ in profiles_dir.glob("*/04_final_profile.json")),
        "network_review_json": sum(1 for _ in profiles_dir.glob("*/03_network_review.json")),
        "legacy_network_review_json": sum(1 for _ in profiles_dir.glob("*/06_network_review.json")),
    }


def build_paths(bucket: str, operator_id: str, profiles_dir: Path, output_root: Path) -> dict[str, str]:
    base = f"gs://{bucket}"
    return {
        "remote_profiles": f"{base}/data/messages_research_profiles/{operator_id}",
        "remote_output": f"{base}/pipeline_output/messages_research/{operator_id}",
        "local_profiles": str(profiles_dir),
        "local_output": str(output_root / operator_id),
    }


def run_sync_command(args: argparse.Namespace, src: str, dst: str) -> dict[str, Any]:
    if args.sync_tool == "gsutil":
        cmd = ["gsutil", "-m", "rsync", "-r", src, dst]
        if args.dry_run:
            cmd.insert(3, "-n")
    else:
        cmd = ["gcloud", "storage", "rsync", "--recursive", src, dst]
        if args.dry_run:
            cmd.insert(3, "--dry-run")

    started = time.monotonic()
    if args.print_commands or args.dry_run:
        print(" ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }


def cmd_status(args: argparse.Namespace) -> int:
    try:
        skip = resolve_skip_status(args)
        operator_id = skip.get("operator_id") or args.operator_id
        if not operator_id:
            raise SyncError("could not resolve operator_id")
        profiles_dir = Path(args.profiles_dir)
        paths = build_paths(args.bucket, operator_id, profiles_dir, Path(args.output_root))
        emit({
            "primitive": "sync_messages_research_cache",
            "command": "status",
            "status": "ok",
            "operator_id": operator_id,
            "email_hint": args.email_hint,
            "remote_skip_status": skip,
            "local_profiles": count_local_profiles(profiles_dir),
            "paths": paths,
        })
        return 0
    except SyncError as exc:
        emit({"primitive": "sync_messages_research_cache", "command": "status", "status": "failed", "error": str(exc)})
        return 1


def cmd_download(args: argparse.Namespace) -> int:
    try:
        skip = resolve_skip_status(args)
        operator_id = skip.get("operator_id") or args.operator_id
        if not operator_id:
            raise SyncError("could not resolve operator_id")
        profiles_dir = Path(args.profiles_dir)
        output_root = Path(args.output_root)
        paths = build_paths(args.bucket, operator_id, profiles_dir, output_root)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        Path(paths["local_output"]).mkdir(parents=True, exist_ok=True)

        before = count_local_profiles(profiles_dir)
        syncs: list[dict[str, Any]] = []
        syncs.append(run_sync_command(args, paths["remote_profiles"], paths["local_profiles"]))
        if not args.skip_output:
            syncs.append(run_sync_command(args, paths["remote_output"], paths["local_output"]))
        failed = [s for s in syncs if s["returncode"] != 0]
        after = count_local_profiles(profiles_dir)
        payload = {
            "primitive": "sync_messages_research_cache",
            "command": "download",
            "status": "failed" if failed else "ok",
            "dry_run": bool(args.dry_run),
            "synced_at": now_iso(),
            "operator_id": operator_id,
            "remote_skip_status": skip,
            "local_before": before,
            "local_after": after,
            "paths": paths,
            "syncs": syncs,
        }
        write_json(Path(args.manifest), payload)
        emit({k: v for k, v in payload.items() if k != "syncs"} | {"manifest": str(args.manifest), "sync_returncodes": [s["returncode"] for s in syncs]})
        return 1 if failed else 0
    except SyncError as exc:
        emit({"primitive": "sync_messages_research_cache", "command": "download", "status": "failed", "error": str(exc)})
        return 1


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator-id", help="Operator UUID; omitted means current effective operator from Powerset auth")
    parser.add_argument("--api-url", default=os.getenv("POWERPACKS_API_URL") or os.getenv("POWERSET_API_URL") or DEFAULT_API_URL)
    parser.add_argument("--token", help="Powerset bearer token; defaults to cached `$powerset login` token")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--bucket", default=os.getenv("PROCESSING_ARTIFACT_BUCKET") or DEFAULT_BUCKET)
    parser.add_argument("--profiles-dir", default=str(DEFAULT_PROFILES_DIR), help="Local profile cache dir used by deep_research_contacts --output-dir")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Local root for synced server pipeline_output/messages_research/<operator_id>")
    parser.add_argument("--email-hint", default="", help="Optional human-readable note for manifests; not used for auth")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync messages deep-research cache from GCS into .powerpacks")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Resolve operator + show remote skip status and local cache counts")
    add_common(status)
    status.set_defaults(func=cmd_status)

    download = sub.add_parser("download", help="Download profile cache and server pipeline output from GCS")
    add_common(download)
    download.add_argument("--sync-tool", choices=["gcloud", "gsutil"], default=os.getenv("PROCESSING_SYNC_TOOL") or "gcloud")
    download.add_argument("--dry-run", action="store_true")
    download.add_argument("--print-commands", action="store_true")
    download.add_argument("--skip-output", action="store_true", help="Only sync data/messages_research_profiles/<operator_id>")
    download.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    download.set_defaults(func=cmd_download)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
