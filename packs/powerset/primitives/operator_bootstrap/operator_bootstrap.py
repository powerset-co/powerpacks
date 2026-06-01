#!/usr/bin/env python3
"""Sync a published operator bootstrap bundle into local Powerpacks state."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


CODE_ROOT = Path(__file__).resolve().parents[4]
ROOT = Path.cwd()
DEFAULT_SUMMARY_URI = os.environ.get(
    "POWERPACKS_OPERATOR_BOOTSTRAP_SUMMARY_URI",
    "gs://powerset-search-processing-artifacts/powerpacks/operator-bootstrap/summary.json",
)
DEFAULT_BUNDLE_DIR = Path(".powerpacks/operator-bootstrap/bundles")
DEFAULT_REGISTRY_DIR = Path(".powerpacks/operator-bootstrap/registry")
DEFAULT_CREDENTIALS_PATH = Path.home() / ".powerpacks" / "credentials.json"
REAUTH_MARKERS = [
    "problem refreshing your current auth tokens",
    "reauthentication failed",
    "cannot prompt during non-interactive execution",
    "invalid_grant",
]


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def redacted_error(text: str, limit: int = 1200) -> str:
    clipped = (text or "").strip()[-limit:]
    return re.sub(r"(?i)(token|secret|password|credential)[^ \n\t]*", "<redacted>", clipped)


def is_reauth_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in REAUTH_MARKERS)


def slugify(value: Any, fallback: str = "operator") -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-") or fallback


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def parse_exact_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://") or uri.endswith("/") or "*" in uri:
        raise ValueError("gcs-uri must be an exact object gs:// URI")
    rest = uri[5:]
    bucket, sep, object_name = rest.partition("/")
    if not bucket or not sep or not object_name:
        raise ValueError("gcs-uri must be an exact object gs:// URI")
    return bucket, object_name


def gcloud_auth_state() -> dict[str, Any]:
    if not shutil.which("gcloud"):
        return {
            "gcloud_installed": False,
            "gcloud_active_account": "",
            "reauth_required": False,
        }
    proc = subprocess.run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    state: dict[str, Any] = {
        "gcloud_installed": True,
        "gcloud_active_account": "",
        "reauth_required": False,
    }
    if proc.returncode != 0:
        combined = f"{proc.stdout}\n{proc.stderr}"
        state.update({
            "status": "failed",
            "reauth_required": is_reauth_error(combined),
            "stderr": redacted_error(combined),
        })
        if state["reauth_required"]:
            state["reauth_command"] = "gcloud auth login --no-launch-browser"
        return state
    state["gcloud_active_account"] = (proc.stdout or "").strip().splitlines()[0].strip() if proc.stdout.strip() else ""
    return state


def download_gcs_object(gcs_uri: str, output: Path) -> tuple[int, dict[str, Any]]:
    try:
        parse_exact_gcs_uri(gcs_uri)
    except ValueError as exc:
        return 2, {"status": "rejected", "reason": str(exc)}
    if not shutil.which("gcloud"):
        return 2, {
            "status": "skipped",
            "reason": "gcloud_not_installed",
            "auth": {"gcloud_installed": False},
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=str(output.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.unlink(missing_ok=True)
    proc = subprocess.run(
        ["gcloud", "storage", "cp", gcs_uri, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        combined = f"{proc.stdout}\n{proc.stderr}"
        if is_reauth_error(combined):
            return 20, {
                "status": "blocked_user_action",
                "reason": "gcloud_reauthentication_required",
                "reauth_command": "gcloud auth login --no-launch-browser",
                "stderr": redacted_error(combined),
            }
        return 2, {
            "status": "failed",
            "reason": "gcloud_storage_cp_failed",
            "stderr": redacted_error(combined),
        }
    tmp_path.replace(output)
    return 0, {
        "status": "ok",
        "download_backend": "gcloud",
        "output": display_path(output),
        "sha256": sha256_file(output),
    }


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    except Exception:
        return {}


def load_credentials_identity(path: Path) -> dict[str, str]:
    if not path.exists():
        return {"credentials_path": str(path), "status": "missing"}
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"credentials_path": str(path), "status": "unreadable", "error": str(exc)}
    token = str(creds.get("access_token") or "")
    payload = decode_jwt_payload(token) if token else {}
    email = (
        creds.get("email")
        or payload.get("email")
        or payload.get("https://api.powerset.dev/email")
        or payload.get("https://api.powerset.co/email")
        or ""
    )
    return {
        "credentials_path": str(path),
        "status": "ok" if token else "missing_access_token",
        "auth0_subject": str(payload.get("sub") or ""),
        "email": str(email or ""),
    }


def email_slug(email: str) -> str:
    local = str(email or "").split("@", 1)[0]
    return slugify(local.lower(), "")


def import_postgres_client() -> Any | None:
    lib_dir = CODE_ROOT / "packs/search/primitives/lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        import postgres_client  # type: ignore

        return postgres_client
    except Exception:
        return None


def has_database_url() -> bool:
    return any(os.environ.get(key) for key in ["DATABASE_URL", "SUPABASE_DATABASE_URL", "SUPABASE_DB_URL"])


def matching_user_row(rows: list[dict[str, Any]], subject: str, email: str) -> dict[str, Any]:
    subject = str(subject or "")
    email_lower = str(email or "").lower()
    for row in rows:
        if subject and str(row.get("user_id") or "") == subject:
            return row
    for row in rows:
        if email_lower and str(row.get("email") or "").lower() == email_lower:
            return row
    return {}


def resolve_user_from_postgres(identity: dict[str, str], env_file: Path | None) -> dict[str, Any]:
    subject = identity.get("auth0_subject", "")
    email = identity.get("email", "")
    if not subject and not email:
        return {"status": "skipped", "reason": "missing_credentials_identity"}
    pg = import_postgres_client()
    if pg is None:
        return {"status": "skipped", "reason": "postgres_client_unavailable"}
    try:
        pg.load_env_file(env_file)
        fixture = pg.fixture_rows("users")
        if fixture is not None:
            row = matching_user_row(fixture, subject, email)
            if not row:
                return {"status": "skipped", "reason": "no_matching_fixture_user"}
            return {
                "status": "ok",
                "source": "postgres_fixture_users",
                "operator_id": str(row.get("id") or ""),
                "auth0_subject": str(row.get("user_id") or ""),
                "email": str(row.get("email") or ""),
                "name": str(row.get("name") or ""),
            }
        if not has_database_url():
            return {"status": "skipped", "reason": "missing_database_url"}
        psycopg2 = pg.ensure_psycopg2()
        query = """
            SELECT id::text, user_id, email, name
            FROM users
            WHERE (%s <> '' AND user_id = %s)
               OR (%s <> '' AND lower(email) = lower(%s))
            ORDER BY CASE WHEN %s <> '' AND user_id = %s THEN 0 ELSE 1 END
            LIMIT 1
        """
        params = (subject, subject, email, email, subject, subject)
        with psycopg2.connect(pg.database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
        if not row:
            return {"status": "skipped", "reason": "no_matching_postgres_user"}
        return {
            "status": "ok",
            "source": "postgres_users",
            "operator_id": str(row[0] or ""),
            "auth0_subject": str(row[1] or ""),
            "email": str(row[2] or ""),
            "name": str(row[3] or ""),
        }
    except Exception as exc:
        return {"status": "skipped", "reason": "postgres_lookup_failed", "error": redacted_error(str(exc), 400)}


def resolve_single_default_set_operator(env_file: Path | None, credentials_path: Path) -> dict[str, Any]:
    pg = import_postgres_client()
    if pg is None:
        return {"status": "skipped", "reason": "postgres_client_unavailable"}
    try:
        pg.load_env_file(env_file)
        if not has_database_url() and pg.fixture_rows("sets") is None:
            return {"status": "skipped", "reason": "missing_database_url"}
        resolved = pg.fetch_set_operator_ids(env_file=env_file, credentials_path=credentials_path)
        operator_ids = [str(value) for value in resolved.get("operator_ids") or [] if value]
        if len(operator_ids) != 1:
            return {
                "status": "skipped",
                "reason": "default_set_operator_count_not_one",
                "operator_count": len(operator_ids),
                "set_id": resolved.get("set_id"),
            }
        member = (resolved.get("members") or [{}])[0]
        return {
            "status": "ok",
            "source": "default_set_single_operator",
            "operator_id": operator_ids[0],
            "set_id": resolved.get("set_id"),
            "set_name": resolved.get("set_name"),
            "email": str(member.get("email") or ""),
            "name": str(member.get("name") or ""),
        }
    except Exception as exc:
        return {"status": "skipped", "reason": "default_set_lookup_failed", "error": redacted_error(str(exc), 400)}


def add_unique(values: list[str], value: str) -> None:
    value = str(value or "").strip()
    if value and value not in values:
        values.append(value)


def resolve_operator(args: argparse.Namespace, identity: dict[str, str], auth: dict[str, Any]) -> dict[str, Any]:
    env_file = Path(args.env_file) if args.env_file else None
    operator_id_candidates: list[str] = []
    slug_candidates: list[str] = []
    email_candidates: list[str] = []
    sources: list[dict[str, Any]] = []

    for value in [
        getattr(args, "operator_id", ""),
        os.environ.get("POWERPACKS_OPERATOR_ID", ""),
        os.environ.get("POWERSET_OPERATOR_ID", ""),
    ]:
        add_unique(operator_id_candidates, value)
    for value in [
        getattr(args, "operator", ""),
        os.environ.get("POWERPACKS_OPERATOR_SLUG", ""),
        os.environ.get("POWERSET_OPERATOR_SLUG", ""),
    ]:
        add_unique(slug_candidates, slugify(value, ""))

    if not operator_id_candidates:
        user_row = resolve_user_from_postgres(identity, env_file)
        sources.append({"id": "postgres_user", **user_row})
        if user_row.get("operator_id"):
            add_unique(operator_id_candidates, str(user_row["operator_id"]))
        if user_row.get("email"):
            add_unique(email_candidates, str(user_row["email"]))
            add_unique(slug_candidates, email_slug(str(user_row["email"])))

    if not operator_id_candidates:
        default_set = resolve_single_default_set_operator(env_file, Path(args.credentials_path))
        sources.append({"id": "default_set", **default_set})
        if default_set.get("operator_id"):
            add_unique(operator_id_candidates, str(default_set["operator_id"]))
        if default_set.get("email"):
            add_unique(email_candidates, str(default_set["email"]))
            add_unique(slug_candidates, email_slug(str(default_set["email"])))

    if auth.get("gcloud_active_account"):
        email = str(auth.get("gcloud_active_account") or "")
        add_unique(email_candidates, email)
        add_unique(slug_candidates, email_slug(email))
    if identity.get("email"):
        add_unique(email_candidates, identity["email"])
        add_unique(slug_candidates, email_slug(identity["email"]))

    return {
        "status": "resolved" if operator_id_candidates or slug_candidates else "unresolved",
        "operator_id_candidates": operator_id_candidates,
        "operator_slug_candidates": slug_candidates,
        "email_candidates": email_candidates,
        "auth0_subject": identity.get("auth0_subject", ""),
        "gcloud_active_account": auth.get("gcloud_active_account", ""),
        "sources": sources,
    }


def iter_operator_entries(summary: Any) -> list[dict[str, Any]]:
    if isinstance(summary, dict):
        entries = summary.get("operators") or summary.get("entries") or []
    elif isinstance(summary, list):
        entries = summary
    else:
        entries = []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def matching_entry(summary: Any, resolution: dict[str, Any]) -> dict[str, Any] | None:
    entries = iter_operator_entries(summary)
    ids = {str(value) for value in resolution.get("operator_id_candidates") or [] if value}
    slugs = {slugify(value, "") for value in resolution.get("operator_slug_candidates") or [] if value}
    if ids:
        for entry in entries:
            if str(entry.get("operator_id") or "") in ids:
                return entry
    if slugs:
        for entry in entries:
            if slugify(entry.get("operator"), "") in slugs:
                return entry
    return None


def bundle_uri_for_entry(entry: dict[str, Any]) -> str:
    gcs = entry.get("gcs") if isinstance(entry.get("gcs"), dict) else {}
    return str(gcs.get("bundle") or entry.get("bundle_gcs_uri") or entry.get("bundle_uri") or "")


def run_sync(args: argparse.Namespace) -> int:
    identity = load_credentials_identity(Path(args.credentials_path))
    auth = gcloud_auth_state()
    resolution = resolve_operator(args, identity, auth)

    registry_dir = ROOT / args.registry_dir
    summary_path = registry_dir / "summary.json"
    summary_code, summary_download = download_gcs_object(args.summary_uri, summary_path)
    if summary_code == 20:
        emit({
            "status": "blocked_user_action",
            "reason": "gcloud_reauthentication_required",
            "setup_command": "$powerset setup",
            "reauth_command": "gcloud auth login --no-launch-browser",
            "operator_resolution": resolution,
            "summary_download": summary_download,
        })
        return 20
    if summary_code != 0:
        emit({
            "status": "skipped",
            "reason": "bootstrap_summary_unavailable",
            "summary_uri": args.summary_uri,
            "operator_resolution": resolution,
            "gcloud": auth,
            "summary_download": summary_download,
        })
        return 0

    try:
        summary = read_json(summary_path)
    except Exception as exc:
        emit({
            "status": "failed",
            "reason": "bootstrap_summary_unreadable",
            "summary": display_path(summary_path),
            "error": str(exc),
            "operator_resolution": resolution,
        })
        return 2

    entry = matching_entry(summary, resolution)
    if not entry:
        emit({
            "status": "skipped",
            "reason": "no_matching_operator_bootstrap",
            "summary": display_path(summary_path),
            "operator_resolution": resolution,
            "available_operator_count": len(iter_operator_entries(summary)),
        })
        return 0

    bundle_uri = bundle_uri_for_entry(entry)
    if not bundle_uri:
        emit({
            "status": "failed",
            "reason": "matching_bootstrap_entry_has_no_bundle_uri",
            "entry": {
                "operator": entry.get("operator"),
                "operator_id": entry.get("operator_id"),
            },
            "summary": display_path(summary_path),
        })
        return 2

    operator_slug = slugify(entry.get("operator") or entry.get("operator_id"), "operator")
    bundle_path = ROOT / args.bundle_dir / f"{operator_slug}.operator-bootstrap.tar.gz"
    bundle_code, bundle_download = download_gcs_object(bundle_uri, bundle_path)
    if bundle_code == 20:
        emit({
            "status": "blocked_user_action",
            "reason": "gcloud_reauthentication_required",
            "setup_command": "$powerset setup",
            "reauth_command": "gcloud auth login --no-launch-browser",
            "operator_resolution": resolution,
            "bundle_download": bundle_download,
        })
        return 20
    if bundle_code != 0:
        emit({
            "status": "failed",
            "reason": "operator_bootstrap_bundle_download_failed",
            "operator": entry.get("operator"),
            "operator_id": entry.get("operator_id"),
            "bundle_uri": bundle_uri,
            "bundle_download": bundle_download,
        })
        return 2

    payload = {
        "status": "ok",
        "operator": entry.get("operator"),
        "operator_id": entry.get("operator_id"),
        "bundle": display_path(bundle_path),
        "bundle_sha256": sha256_file(bundle_path),
        "summary": display_path(summary_path),
        "summary_sha256": sha256_file(summary_path),
        "summary_uri": args.summary_uri,
        "bundle_uri": bundle_uri,
        "operator_resolution": resolution,
        "bundle_download": bundle_download,
    }
    write_json(registry_dir / "latest-sync.json", payload)
    emit(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sync = sub.add_parser("sync")
    sync.add_argument("--summary-uri", default=DEFAULT_SUMMARY_URI)
    sync.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE_DIR)
    sync.add_argument("--registry-dir", type=Path, default=DEFAULT_REGISTRY_DIR)
    sync.add_argument("--env-file", default=".env")
    sync.add_argument("--credentials-path", default=str(DEFAULT_CREDENTIALS_PATH))
    sync.add_argument("--operator-id", default="")
    sync.add_argument("--operator", default="")
    sync.set_defaults(func=run_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
