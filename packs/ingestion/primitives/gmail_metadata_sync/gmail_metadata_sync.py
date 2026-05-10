#!/usr/bin/env python3
"""Local wrapper for server-side Gmail metadata sync.

This primitive never reads Gmail bodies/subjects locally. It uses the existing
Powerset bearer token to call a backend sync endpoint once that endpoint exists.
The backend is responsible for OAuth refresh tokens and metadata-only ingestion.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(".powerpacks/network-import/gmail/sync-run.json")
DEFAULT_API_URL = "https://search-api-7wk4uhe77q-uw.a.run.app"
DEFAULT_SYNC_PATH = "/v2/integrations/gmail-sync"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def powerset_token() -> str:
    cmd = ["uv", "run", "--project", str(repo_root()), "python", str(repo_root() / "packs/powerset/primitives/auth/auth.py"), "token", "--bearer-only"]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise SystemExit((completed.stderr or completed.stdout or "Powerset login required").strip())
    token = completed.stdout.strip()
    if not token:
        raise SystemExit("Powerset login required")
    return token


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def request_json(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> tuple[int, Any, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else None, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else None, raw
        except json.JSONDecodeError:
            return exc.code, None, raw


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def sync_url(args: argparse.Namespace, job_id: str = "") -> str:
    base = args.api_url.rstrip("/") + args.sync_path
    return base + (f"/{job_id}" if job_id else "")


def cmd_run(args: argparse.Namespace) -> int:
    ledger = {
        "primitive": "gmail_metadata_sync",
        "status": "blocked_approval",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "input": {
            "api_url": args.api_url,
            "sync_path": args.sync_path,
            "account_email": args.account_email,
            "metadata_only": True,
            "include_bodies": False,
        },
        "blocked": {"step": "trigger_backend_sync", "approval_type": "gmail_metadata_sync"},
    }
    save_ledger(Path(args.ledger), ledger)
    emit({
        "status": "blocked_approval",
        "ledger": args.ledger,
        "message": "Approve server-side Gmail metadata sync? This primitive never reads message bodies locally.",
        "continue_command": f"uv run --project . python packs/ingestion/primitives/gmail_metadata_sync/gmail_metadata_sync.py approve --ledger {args.ledger} && uv run --project . python packs/ingestion/primitives/gmail_metadata_sync/gmail_metadata_sync.py continue --ledger {args.ledger}",
    })
    return 20


def cmd_approve(args: argparse.Namespace) -> int:
    ledger = read_json(Path(args.ledger))
    ledger["approved_at"] = now_iso()
    ledger.pop("blocked", None)
    ledger["status"] = "approved"
    save_ledger(Path(args.ledger), ledger)
    emit({"status": "approved", "ledger": args.ledger})
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = read_json(ledger_path)
    if ledger.get("blocked"):
        emit({"status": "blocked_approval", "ledger": args.ledger, "blocked": ledger["blocked"]})
        return 20
    token = powerset_token()
    # Rehydrate args from ledger so status/continue works with only --ledger.
    class A: pass
    a = A()
    a.api_url = ledger.get("input", {}).get("api_url") or DEFAULT_API_URL
    a.sync_path = ledger.get("input", {}).get("sync_path") or DEFAULT_SYNC_PATH
    body = {"metadata_only": True, "include_bodies": False}
    if ledger.get("input", {}).get("account_email"):
        body["account_email"] = ledger["input"]["account_email"]
    status, payload, raw = request_json("POST", sync_url(a), token, body)
    ledger["triggered_at"] = now_iso()
    ledger["trigger_status_code"] = status
    ledger["trigger_response"] = payload if payload is not None else raw[:1000]
    if status in (200, 201, 202):
        ledger["status"] = "submitted"
        if isinstance(payload, dict) and (payload.get("job_id") or payload.get("run_id")):
            ledger["job_id"] = payload.get("job_id") or payload.get("run_id")
    else:
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "http_status": status, "response": ledger["trigger_response"], "ledger": args.ledger})
        return 1
    save_ledger(ledger_path, ledger)
    emit({"status": "submitted", "http_status": status, "response": payload, "ledger": args.ledger})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = read_json(Path(args.ledger))
    if not ledger:
        emit({"status": "missing", "ledger": args.ledger})
        return 1
    job_id = ledger.get("job_id")
    if args.remote and job_id:
        token = powerset_token()
        class A: pass
        a = A(); a.api_url = ledger.get("input", {}).get("api_url") or DEFAULT_API_URL; a.sync_path = ledger.get("input", {}).get("sync_path") or DEFAULT_SYNC_PATH
        status, payload, raw = request_json("GET", sync_url(a, job_id), token)
        ledger["remote_status_code"] = status
        ledger["remote_status"] = payload if payload is not None else raw[:1000]
        save_ledger(Path(args.ledger), ledger)
    emit(ledger)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trigger/status server-side Gmail metadata sync; no local message bodies")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--api-url", default=DEFAULT_API_URL)
    run.add_argument("--sync-path", default=DEFAULT_SYNC_PATH)
    run.add_argument("--account-email", default="")
    run.set_defaults(func=cmd_run)
    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.set_defaults(func=cmd_approve)
    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=cmd_continue)
    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.add_argument("--remote", action="store_true")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
