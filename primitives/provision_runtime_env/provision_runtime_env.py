#!/usr/bin/env python3
"""Provision a local Powerpacks .env from Powerset or GCP Secret Manager.

The primitive never prints secret values. It writes only allowlisted runtime
keys and reports redacted metadata for auditability.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "primitives" / "powerset_auth" / "powerset_auth.py"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".powerpacks" / "credentials.json"
DEFAULT_SEARCH_API_URL = os.environ.get("POWERPACKS_SEARCH_API_URL", "https://api.powerset.dev")
DEFAULT_SEARCH_API_ENDPOINT = os.environ.get(
    "POWERPACKS_SECRETS_ENDPOINT",
    "/v2/powerpacks/runtime-secrets",
)
DEFAULT_SECRET_MAP = {
    "TURBOPUFFER_API_KEY": "powerpacks-turbopuffer-api-key",
    "TURBOPUFFER_REGION": "powerpacks-turbopuffer-region",
    "DATABASE_URL": "powerpacks-database-url",
    "SUPABASE_DATABASE_URL": "powerpacks-supabase-database-url",
    "SUPABASE_URL": "powerpacks-supabase-url",
    "SUPABASE_SERVICE_ROLE_KEY": "powerpacks-supabase-service-role-key",
    "OPENAI_API_KEY": "powerpacks-openai-api-key",
    "OPENROUTER_API_KEY": "powerpacks-openrouter-api-key",
    "PARALLEL_API_KEY": "powerpacks-parallel-api-key",
    "RAPIDAPI_KEY": "powerpacks-rapidapi-key",
}
PROFILES = {
    "search-core": [
        "TURBOPUFFER_API_KEY",
        "TURBOPUFFER_REGION",
        "DATABASE_URL",
        "OPENAI_API_KEY",
    ],
    "messages": [
        "OPENROUTER_API_KEY",
        "PARALLEL_API_KEY",
    ],
    "sales-nav": [
        "RAPIDAPI_KEY",
    ],
    "supabase-admin": [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    ],
}
PROFILES["all"] = list(dict.fromkeys(key for keys in PROFILES.values() for key in keys))
ALLOWED_KEYS = set(DEFAULT_SECRET_MAP)


@dataclass
class AuthContext:
    email: str
    token: str


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run_json(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
    return json.loads(proc.stdout)


def run_text(args: list[str]) -> str:
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
    return proc.stdout.strip()


def decode_jwt_email(token: str) -> str | None:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email") or payload.get("https://api.powerset.dev/email")
    except Exception:
        return None


def auth_context(credentials_path: Path) -> AuthContext:
    whoami = run_json([sys.executable, str(AUTH), "whoami", "--credentials-path", str(credentials_path)])
    if whoami.get("status") != "logged_in":
        raise RuntimeError("not logged in; run powerset-login first")
    email = str(whoami.get("email") or "")
    token = run_text([
        sys.executable,
        str(AUTH),
        "token",
        "--bearer-only",
        "--credentials-path",
        str(credentials_path),
    ])
    token_email = decode_jwt_email(token)
    if token_email:
        email = token_email
    if not email.endswith("@powerset.co"):
        raise PermissionError(f"refusing to provision secrets for non-powerset.co account: {email or 'unknown'}")
    return AuthContext(email=email, token=token)


def requested_keys(profile: str, includes: list[str] | None) -> list[str]:
    keys = list(PROFILES[profile])
    for key in includes or []:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unsupported secret key: {key}")
        if key not in keys:
            keys.append(key)
    return keys


def secret_map(overrides: list[str] | None) -> dict[str, str]:
    result = dict(DEFAULT_SECRET_MAP)
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"expected KEY=SECRET_ID override, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unsupported secret key: {key}")
        if not value:
            raise ValueError(f"empty Secret Manager id for {key}")
        result[key] = value
    return result


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        return [], {}
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return lines, values


def quote_env_value(value: str) -> str:
    if value == "" or any(ch in value for ch in " \t\n#'\"$`\\"):
        return json.dumps(value)
    return value


def write_env(path: Path, secrets: dict[str, str], *, overwrite: bool) -> dict[str, Any]:
    lines, existing = read_env(path)
    next_lines = list(lines)
    written: list[str] = []
    skipped: list[str] = []
    index: dict[str, int] = {}
    for idx, line in enumerate(next_lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            index[stripped.split("=", 1)[0]] = idx

    for key, value in secrets.items():
        if key not in ALLOWED_KEYS:
            raise ValueError(f"refusing to write unsupported key: {key}")
        if key in existing and existing[key] and not overwrite:
            skipped.append(key)
            continue
        rendered = f"{key}={quote_env_value(value)}"
        if key in index:
            next_lines[index[key]] = rendered
        else:
            if next_lines and next_lines[-1] != "":
                next_lines.append("")
            next_lines.append(rendered)
        written.append(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
        tmp.write("\n".join(next_lines).rstrip() + "\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {"written": sorted(written), "skipped_existing": sorted(skipped)}


def redact_keys(values: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "present": bool(value),
            "length": len(value or ""),
            "redacted": "***" if value else "",
        }
        for key, value in sorted(values.items())
    ]


def fetch_from_gcp(keys: list[str], mapping: dict[str, str], project: str | None) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for key in keys:
        command = ["gcloud", "secrets", "versions", "access", "latest", "--secret", mapping[key]]
        if project:
            command.extend(["--project", project])
        value = run_text(command)
        if value:
            secrets[key] = value
    return secrets


def fetch_from_search_api(
    keys: list[str],
    *,
    token: str,
    base_url: str,
    endpoint: str,
    profile: str,
) -> dict[str, str]:
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    body = json.dumps({"profile": profile, "keys": keys}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"search-api provisioning failed (HTTP {exc.code}): {details}") from exc
    raw_secrets = payload.get("secrets") if isinstance(payload, dict) else None
    if not isinstance(raw_secrets, dict):
        raise RuntimeError("search-api response missing object field: secrets")
    return {
        str(key): str(value)
        for key, value in raw_secrets.items()
        if key in keys and key in ALLOWED_KEYS and value
    }


def cmd_plan(args: argparse.Namespace) -> int:
    keys = requested_keys(args.profile, args.include)
    _, existing = read_env(args.env_file)
    missing = [key for key in keys if not existing.get(key)]
    emit({
        "primitive": "provision_runtime_env",
        "command": "plan",
        "profile": args.profile,
        "env_file": str(args.env_file),
        "requested_keys": keys,
        "present": sorted(set(keys) - set(missing)),
        "missing": missing,
        "sources": ["search-api", "gcp"],
        "note": "pull requires powerset.co Auth0 login and --confirm",
    })
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    keys = requested_keys(args.profile, args.include)
    _, existing = read_env(args.env_file)
    values = {key: existing.get(key, "") for key in keys}
    missing = [key for key, value in values.items() if not value]
    emit({
        "primitive": "provision_runtime_env",
        "command": "check",
        "status": "ok" if not missing else "missing",
        "profile": args.profile,
        "env_file": str(args.env_file),
        "secrets": redact_keys(values),
        "missing": missing,
    })
    return 0 if not missing else 1


def cmd_pull(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise SystemExit("refusing to write secrets without --confirm")
    keys = requested_keys(args.profile, args.include)
    auth = auth_context(args.credentials_path)
    mapping = secret_map(args.secret)
    source = args.source

    errors: list[str] = []
    secrets: dict[str, str] = {}
    if source in {"auto", "search-api"}:
        try:
            secrets = fetch_from_search_api(
                keys,
                token=auth.token,
                base_url=args.search_api_url,
                endpoint=args.search_api_endpoint,
                profile=args.profile,
            )
            source = "search-api"
        except Exception as exc:
            if args.source == "search-api":
                raise
            errors.append(str(exc))
    if not secrets and source in {"auto", "gcp"}:
        secrets = fetch_from_gcp(keys, mapping, args.gcp_project)
        source = "gcp"

    missing = [key for key in keys if key not in secrets]
    result = write_env(args.env_file, secrets, overwrite=args.overwrite)
    emit({
        "primitive": "provision_runtime_env",
        "command": "pull",
        "status": "ok" if not missing else "partial",
        "source": source,
        "profile": args.profile,
        "email": auth.email,
        "env_file": str(args.env_file),
        "written": result["written"],
        "skipped_existing": result["skipped_existing"],
        "missing": missing,
        "fallback_errors": errors,
        "secrets": redact_keys(secrets),
    })
    return 0 if not missing else 1


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=sorted(PROFILES), default="search-core")
    parser.add_argument("--include", action="append", choices=sorted(ALLOWED_KEYS))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision Powerpacks runtime env")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Show required runtime keys without fetching secrets")
    add_common(plan)
    plan.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="Check whether the local env file has required keys")
    add_common(check)
    check.set_defaults(func=cmd_check)

    pull = sub.add_parser("pull", help="Fetch secrets and merge them into an env file")
    add_common(pull)
    pull.add_argument("--confirm", action="store_true")
    pull.add_argument("--overwrite", action="store_true")
    pull.add_argument("--source", choices=["auto", "search-api", "gcp"], default="auto")
    pull.add_argument("--credentials-path", type=Path, default=DEFAULT_CREDENTIALS_PATH)
    pull.add_argument("--search-api-url", default=DEFAULT_SEARCH_API_URL)
    pull.add_argument("--search-api-endpoint", default=DEFAULT_SEARCH_API_ENDPOINT)
    pull.add_argument("--gcp-project", default=os.environ.get("POWERPACKS_GCP_PROJECT"))
    pull.add_argument("--secret", action="append", help="Override Secret Manager id as KEY=SECRET_ID")
    pull.set_defaults(func=cmd_pull)

    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except (PermissionError, RuntimeError, ValueError) as exc:
        emit({
            "primitive": "provision_runtime_env",
            "command": args.command,
            "status": "failed",
            "error": str(exc),
        })
        raise SystemExit(1)


if __name__ == "__main__":
    main()
