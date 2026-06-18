#!/usr/bin/env python3
"""Download the operator's Powerset candidate catalog into a local CSV.

Stdlib-only. Uses the JWT saved by `powerset_auth login` to call
`<api_base_url>/v2/contacts` paginated, and writes a flat CSV that the
`match_local_candidates` primitive consumes.

On auth/network failure, falls back to the existing cache file when present
and exits 0; on a server-shaped failure (5xx body / unparseable response) it
exits non-zero so an agent can surface the diagnostic.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.shared.csv_io import CsvIO


CATALOG_HEADERS = [
    "id",
    "name",
    "linkedin_url",
    "phone_number",
    "emails",
    "public_identifier",
]

CONTACTS_INCLUDE_FIELDS = ",".join(
    [
        "id",
        "display_name",
        "first_name",
        "last_name",
        "confirmed_linkedin_url",
        "public_profile_url",
        "phone_number",
        "emails",
        "public_identifier",
    ]
)


DEFAULT_API_BASE_URL = os.environ.get(
    "POWERPACKS_API_BASE_URL", "https://search-api-7wk4uhe77q-uw.a.run.app"
)
DEFAULT_LOCAL_API_BASE_URL = os.environ.get(
    "POWERPACKS_LOCAL_API_BASE_URL", "http://localhost:8000"
)
DEFAULT_CREDENTIALS_PATH = Path(
    os.environ.get(
        "POWERPACKS_CREDENTIALS_PATH",
        str(Path.home() / ".powerpacks" / "credentials.json"),
    )
)
DEFAULT_CATALOG_PATH = Path("powerset_contacts.csv")
DEFAULT_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_access_token(creds_path: Path) -> tuple[str | None, dict[str, Any] | None, str | None]:
    if not creds_path.exists():
        return None, None, "credentials file missing; run powerset_auth login"
    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, None, f"credentials file unreadable: {exc}"
    expires_at = float(creds.get("expires_at", 0))
    if time.time() > expires_at - 60:
        return None, creds, "access token expired; run powerset_auth token to refresh"
    token = creds.get("access_token")
    if not token:
        return None, creds, "credentials file has no access_token"
    return token, creds, None


def _candidate_name(row: dict[str, Any]) -> str:
    display = (row.get("display_name") or "").strip()
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part).strip()
    if first and last:
        return full
    if display:
        return display
    if full:
        return full
    return (row.get("public_identifier") or "").strip()


def _http_get_json(url: str, *, headers: dict[str, str], timeout: int = 60) -> tuple[int, Any, str]:
    req = urllib.request.Request(url, method="GET", headers={**headers, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8")), ""
            except json.JSONDecodeError:
                return resp.status, None, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed, raw
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def fetch_candidates(
    api_base_url: str,
    access_token: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    operator_id: str | None = None,
    timeout: int = 60,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Paginate /v2/contacts and return (rows, diagnostics).

    Returns Powerset-shaped candidates {id, name, linkedin_url, phone_number,
    emails, public_identifier}.
    """
    diagnostics: dict[str, Any] = {
        "pages": 0,
        "raw_rows": 0,
        "deduped_rows": 0,
        "total_count": None,
        "errors": [],
    }
    out: dict[str, dict[str, Any]] = {}
    page = 0
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        params = {
            "page": page,
            "page_size": page_size,
            "sort_field": "first_name",
            "sort_dir": "asc",
            "include_fields": CONTACTS_INCLUDE_FIELDS,
        }
        if operator_id:
            params["operator_id"] = operator_id
        url = api_base_url.rstrip("/") + "/v2/contacts?" + urllib.parse.urlencode(params)
        status, payload, raw = _http_get_json(url, headers=headers, timeout=timeout)
        if status == 401:
            raise SystemExit("authentication expired; run powerset_auth login")
        if status != 200:
            raise RuntimeError(f"GET /v2/contacts failed (HTTP {status}): {(raw or '')[:200]}")
        if not isinstance(payload, dict):
            raise RuntimeError("unexpected /v2/contacts response (not an object)")

        rows = payload.get("data") or []
        diagnostics["raw_rows"] += len(rows)
        diagnostics["pages"] += 1
        if diagnostics["total_count"] is None:
            diagnostics["total_count"] = payload.get("total_count")

        for row in rows:
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            name = _candidate_name(row)
            if not name:
                continue
            incoming = {
                "id": cid,
                "name": name,
                "linkedin_url": (row.get("confirmed_linkedin_url") or row.get("public_profile_url") or "").strip() or None,
                "phone_number": (row.get("phone_number") or "").strip() or None,
                "public_identifier": (row.get("public_identifier") or "").strip() or None,
                "emails": [e for e in (row.get("emails") or []) if e],
            }
            existing = out.get(cid)
            if not existing:
                out[cid] = incoming
                continue
            if len(incoming["name"]) > len(existing["name"]):
                existing["name"] = incoming["name"]
            for key in ("linkedin_url", "phone_number", "public_identifier"):
                existing[key] = existing.get(key) or incoming.get(key)
            existing["emails"] = existing.get("emails") or incoming.get("emails")

        if not rows:
            break
        seen = (page + 1) * page_size
        if diagnostics["total_count"] is not None and seen >= int(diagnostics["total_count"]):
            break
        page += 1

    candidates = sorted(out.values(), key=lambda r: r["id"])
    diagnostics["deduped_rows"] = len(candidates)
    return candidates, diagnostics


def write_catalog(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CATALOG_HEADERS)
        for row in rows:
            writer.writerow([
                row.get("id") or "",
                row.get("name") or "",
                row.get("linkedin_url") or "",
                row.get("phone_number") or "",
                ";".join(row.get("emails") or []),
                row.get("public_identifier") or "",
            ])
    return len(rows)


def cmd_sync(args: argparse.Namespace) -> int:
    api_base_url = (args.api_base_url or DEFAULT_LOCAL_API_BASE_URL if args.local else args.api_base_url) or DEFAULT_API_BASE_URL
    catalog_path = Path(args.output)
    manifest_path = Path(args.manifest) if args.manifest else catalog_path.with_suffix(catalog_path.suffix + ".manifest.json")

    access_token, creds, token_error = _load_access_token(args.credentials_path)
    started = time.time()

    if args.use_cached:
        if not catalog_path.exists():
            emit({
                "primitive": "sync_powerset_candidates",
                "command": "sync",
                "status": "failed",
                "error": "--use-cached requested but cache file missing",
                "catalog_path": str(catalog_path),
            })
            return 1
        existing_rows = list(_iter_catalog_rows(catalog_path))
        manifest = {
            "primitive": "sync_powerset_candidates",
            "command": "sync",
            "status": "cached",
            "api_base_url": api_base_url,
            "catalog_path": str(catalog_path),
            "rows": len(existing_rows),
            "manifest_path": str(manifest_path),
        }
        write_json(manifest_path, manifest)
        emit(manifest)
        return 0

    if not access_token:
        # Fall back to cache when present.
        if catalog_path.exists():
            existing_rows = list(_iter_catalog_rows(catalog_path))
            manifest = {
                "primitive": "sync_powerset_candidates",
                "command": "sync",
                "status": "cached_after_auth_error",
                "auth_error": token_error,
                "api_base_url": api_base_url,
                "catalog_path": str(catalog_path),
                "rows": len(existing_rows),
                "manifest_path": str(manifest_path),
            }
            write_json(manifest_path, manifest)
            emit(manifest)
            return 0
        manifest = {
            "primitive": "sync_powerset_candidates",
            "command": "sync",
            "status": "failed",
            "error": token_error or "not logged in",
            "api_base_url": api_base_url,
            "manifest_path": str(manifest_path),
        }
        write_json(manifest_path, manifest)
        emit(manifest)
        return 1

    try:
        candidates, diagnostics = fetch_candidates(
            api_base_url,
            access_token,
            page_size=args.page_size,
            operator_id=args.operator_id,
            timeout=args.timeout,
        )
    except SystemExit as exc:
        if catalog_path.exists():
            existing_rows = list(_iter_catalog_rows(catalog_path))
            manifest = {
                "primitive": "sync_powerset_candidates",
                "command": "sync",
                "status": "cached_after_auth_error",
                "auth_error": str(exc),
                "api_base_url": api_base_url,
                "catalog_path": str(catalog_path),
                "rows": len(existing_rows),
                "manifest_path": str(manifest_path),
            }
            write_json(manifest_path, manifest)
            emit(manifest)
            return 0
        emit({
            "primitive": "sync_powerset_candidates",
            "command": "sync",
            "status": "failed",
            "error": str(exc),
            "api_base_url": api_base_url,
        })
        return 1
    except ConnectionError as exc:
        if catalog_path.exists():
            existing_rows = list(_iter_catalog_rows(catalog_path))
            manifest = {
                "primitive": "sync_powerset_candidates",
                "command": "sync",
                "status": "cached_after_network_error",
                "network_error": str(exc),
                "api_base_url": api_base_url,
                "catalog_path": str(catalog_path),
                "rows": len(existing_rows),
                "manifest_path": str(manifest_path),
            }
            write_json(manifest_path, manifest)
            emit(manifest)
            return 0
        emit({
            "primitive": "sync_powerset_candidates",
            "command": "sync",
            "status": "failed",
            "error": f"network: {exc}",
            "api_base_url": api_base_url,
        })
        return 1
    except RuntimeError as exc:
        emit({
            "primitive": "sync_powerset_candidates",
            "command": "sync",
            "status": "failed",
            "error": f"server: {exc}",
            "api_base_url": api_base_url,
        })
        return 1

    rows_written = write_catalog(catalog_path, candidates)
    elapsed_ms = int((time.time() - started) * 1000)
    manifest = {
        "primitive": "sync_powerset_candidates",
        "command": "sync",
        "status": "ok",
        "api_base_url": api_base_url,
        "operator_id": args.operator_id,
        "catalog_path": str(catalog_path),
        "manifest_path": str(manifest_path),
        "rows": rows_written,
        "elapsed_ms": elapsed_ms,
        "diagnostics": diagnostics,
        "credentials_email": (creds or {}).get("email"),
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def _iter_catalog_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = CsvIO.dict_reader(handle)
        for row in reader:
            yield row


def main() -> None:
    parser = argparse.ArgumentParser(description="Download the operator's Powerset candidate catalog")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Refresh local candidate CSV from /v2/contacts")
    sync.add_argument("--credentials-path", type=Path, default=DEFAULT_CREDENTIALS_PATH)
    sync.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    sync.add_argument("--local", action="store_true", help=f"Use {DEFAULT_LOCAL_API_BASE_URL}")
    sync.add_argument("--operator-id", help="Operator id (admin only)")
    sync.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    sync.add_argument("--timeout", type=int, default=60)
    sync.add_argument("--output", "-o", default=str(DEFAULT_CATALOG_PATH),
                      help="Path to write the candidate CSV")
    sync.add_argument("--manifest", help="Path to write the run manifest JSON")
    sync.add_argument("--use-cached", action="store_true",
                      help="Do not call the API; only validate the cache exists")
    sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
