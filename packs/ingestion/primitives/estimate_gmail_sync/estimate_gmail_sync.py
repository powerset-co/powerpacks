#!/usr/bin/env python3
"""Estimate how much a Gmail msgvault sync would pull, per date window.

Read-only and free: it refreshes the msgvault OAuth token for each account and
counts message IDs matching the fixed scope query plus an ``after:`` date via
the Gmail API (no bodies, no attachments, no sync). It never writes to the
vault.

The scope query is the same one the discover/sync path uses
(``discovery.config.json`` -> sources.gmail.inputs.sync_query), so the estimate
matches what a real ``msgvault sync-full --query <scope> --after <date>`` would
ingest.

Counting method: paginate ``users.messages.list`` with ``fields=messages/id``
(500 ids/page, ~1 quota unit/page). Gmail's ``resultSizeEstimate`` is
deliberately not used -- it returns a flat, useless number.

Usage:
  estimate_gmail_sync.py estimate \
    --window 1y --window 2y --window 5y --window all \
    [--account you@gmail.com ...] [--home ~/.msgvault] \
    [--scope-query "..."] [--throughput 35] [--max-pages 300]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]

DEFAULT_HOME = Path.home() / ".msgvault"
DEFAULT_SCOPE_QUERY = "-category:social -category:promotions -category:forums -category:updates"
DEFAULT_THROUGHPUT = 35.0  # messages/sec, calibrated from msgvault sync_runs
DEFAULT_MAX_PAGES = 300  # 500 ids/page -> 150k message cap before reporting "+"
TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"
WINDOW_YEARS = {"1y": 1, "2y": 2, "5y": 5}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def scope_query_from_config() -> str:
    config = ROOT / "packs/ingestion/primitives/discover_contacts_pipeline/discovery.config.json"
    try:
        data = json.loads(config.read_text())
        value = data["sources"]["gmail"]["inputs"].get("sync_query")
        if value:
            return str(value).strip()
    except (OSError, KeyError, ValueError):
        pass
    return DEFAULT_SCOPE_QUERY


def client_secret(home: Path) -> dict[str, str]:
    raw = json.loads((home / "client_secret.json").read_text())
    return raw.get("installed") or raw.get("web") or raw


def access_token(home: Path, email: str) -> str:
    token = json.loads((home / "tokens" / f"{email}.json").read_text())
    secret = client_secret(home)
    refresh = token.get("refresh_token")
    if not refresh:
        raise RuntimeError("no refresh_token for account")
    data = urllib.parse.urlencode({
        "client_id": token.get("client_id") or secret.get("client_id"),
        "client_secret": token.get("client_secret") or secret.get("client_secret"),
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode()
    uri = token.get("token_uri") or secret.get("token_uri") or TOKEN_URI
    with urllib.request.urlopen(urllib.request.Request(uri, data=data), timeout=30) as resp:
        return json.load(resp)["access_token"]


def count_ids(token: str, query: str, max_pages: int) -> tuple[int, bool]:
    """Count message ids matching the query. Returns (count, truncated)."""
    total = 0
    page_token: str | None = None
    pages = 0
    while True:
        params = {"maxResults": "500", "fields": "messages/id,nextPageToken", "q": query}
        if page_token:
            params["pageToken"] = page_token
        url = GMAIL_BASE + "messages?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        total += len(data.get("messages", []))
        pages += 1
        page_token = data.get("nextPageToken")
        if not page_token or pages >= max_pages:
            break
    return total, page_token is not None


def window_query(scope: str, window: str, now: datetime) -> str:
    if window == "all":
        return scope
    years = WINDOW_YEARS.get(window)
    if not years:
        raise ValueError(f"unknown window: {window}")
    after = (now - timedelta(days=365 * years)).strftime("%Y/%m/%d")
    return f"{scope} after:{after}"


def estimate_time(messages: int, throughput: float) -> dict[str, Any]:
    seconds = messages / throughput if throughput > 0 else 0
    return {"est_seconds": round(seconds), "est_minutes": max(1, round(seconds / 60)) if messages else 0}


def discover_accounts(home: Path) -> list[str]:
    tokens_dir = home / "tokens"
    if not tokens_dir.is_dir():
        return []
    return sorted(p.stem for p in tokens_dir.glob("*.json"))


def cmd_estimate(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser()
    scope = args.scope_query if args.scope_query is not None else scope_query_from_config()
    windows = args.window or ["1y", "2y", "5y", "all"]
    accounts = args.account or discover_accounts(home)
    now = datetime.now(timezone.utc)

    account_results: list[dict[str, Any]] = []
    totals: dict[str, dict[str, int]] = {w: {"messages": 0} for w in windows}
    for email in accounts:
        entry: dict[str, Any] = {"email": email, "windows": {}}
        try:
            token = access_token(home, email)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            entry["error"] = f"token: {exc}"
            account_results.append(entry)
            continue
        for window in windows:
            try:
                count, truncated = count_ids(token, window_query(scope, window, now), args.max_pages)
            except (urllib.error.URLError, ValueError) as exc:
                entry["windows"][window] = {"error": str(exc)}
                continue
            entry["windows"][window] = {
                "messages": count,
                "truncated": truncated,
                **estimate_time(count, args.throughput),
            }
            totals[window]["messages"] += count
        account_results.append(entry)

    for window, agg in totals.items():
        agg.update(estimate_time(agg["messages"], args.throughput))

    emit({
        "status": "completed",
        "scope_query": scope,
        "throughput_msg_per_sec": args.throughput,
        "windows": windows,
        "accounts": account_results,
        "totals": totals,
        "generated_at": now.replace(microsecond=0).isoformat(),
    })
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    """List Gmail accounts msgvault manages — the single source of truth.

    Backed by ``msgvault list-accounts --json`` (its sources table), so it
    reflects add-account / remove-account exactly and never drifts from
    accounts.json."""
    home = Path(args.home).expanduser()
    cmd = ["msgvault"]
    default_home = DEFAULT_HOME
    if home != default_home:
        cmd.extend(["--home", str(home)])
    cmd.extend(["list-accounts", "--json"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        emit({"status": "failed", "error": f"msgvault: {exc}", "accounts": []})
        return 1
    try:
        raw = json.loads(result.stdout or "[]")
    except ValueError:
        # msgvault prints a plain-text "No accounts found..." line instead of []
        # when no accounts are authorized yet. That is an empty list, not a
        # failure (mirrors status_payload's handling).
        if "No accounts found" in (result.stdout or ""):
            emit({"status": "completed", "accounts": []})
            return 0
        emit({"status": "failed", "error": result.stderr.strip() or "list-accounts not JSON", "accounts": []})
        return 1
    accounts = [
        {
            "email": str(row.get("email") or ""),
            "message_count": int(row.get("message_count") or 0),
            "last_sync": row.get("last_sync") or "",
        }
        for row in raw
        if str(row.get("type") or "gmail") == "gmail" and row.get("email")
    ]
    emit({"status": "completed", "accounts": accounts})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Estimate Gmail msgvault sync size per date window")
    sub = parser.add_subparsers(dest="command", required=True)
    acc = sub.add_parser("accounts", help="List msgvault Gmail accounts (the source of truth)")
    acc.add_argument("--home", default=str(DEFAULT_HOME), help="msgvault home dir")
    acc.set_defaults(func=cmd_accounts)
    est = sub.add_parser("estimate", help="Estimate message counts + time per window")
    est.add_argument("--account", action="append", help="Account email (repeatable); default: all msgvault tokens")
    est.add_argument("--window", action="append", choices=["1y", "2y", "5y", "all"], help="Window (repeatable); default: all four")
    est.add_argument("--home", default=str(DEFAULT_HOME), help="msgvault home dir")
    est.add_argument("--scope-query", default=None, help="Override scope query (default: discovery.config.json)")
    est.add_argument("--throughput", type=float, default=DEFAULT_THROUGHPUT, help="messages/sec for time estimate")
    est.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="page cap (500 ids/page) before truncating")
    est.set_defaults(func=cmd_estimate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
