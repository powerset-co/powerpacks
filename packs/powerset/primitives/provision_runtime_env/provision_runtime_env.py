#!/usr/bin/env python3
"""Provision a local Powerpacks .env from GCP Secret Manager.

The primitive never prints secret values. It writes only allowlisted runtime
keys and reports redacted metadata for auditability.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


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
    # RapidAPI: split per-product so a future rotation can give them
    # separate keys without changing call sites. For now both source from
    # the same prod secret, see provision_user_secrets.PROVISION_PLAN.
    "RAPIDAPI_LINKEDIN_KEY": "powerpacks-rapidapi-linkedin-key",
    "RAPIDAPI_TWITTER_KEY": "powerpacks-rapidapi-twitter-key",
}
PROFILES = {
    "search-core": [
        # TURBOPUFFER_REGION is a static config value (default `gcp-us-central1`)
        # in env.example, not a per-user secret, so it is not in this profile.
        "TURBOPUFFER_API_KEY",
        "DATABASE_URL",
        "OPENAI_API_KEY",
        # Messages workflows are now part of the default Powerpacks setup;
        # keep these in the default env pull so `$powerset login` / `$powerset env pull`
        # works for LLM review + Parallel research without a second sync.
        "OPENROUTER_API_KEY",
        "PARALLEL_API_KEY",
        "RAPIDAPI_LINKEDIN_KEY",
    ],
    "messages": [
        "OPENROUTER_API_KEY",
        "PARALLEL_API_KEY",
        "RAPIDAPI_LINKEDIN_KEY",
    ],
    "sales-nav": [
        "RAPIDAPI_LINKEDIN_KEY",
    ],
    "twitter": [
        "RAPIDAPI_TWITTER_KEY",
    ],
    "supabase-admin": [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    ],
}
PROFILES["all"] = list(dict.fromkeys(key for keys in PROFILES.values() for key in keys))
ALLOWED_KEYS = set(DEFAULT_SECRET_MAP)


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run_text(args: list[str]) -> str:
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
    return proc.stdout.strip()


def gcloud_account() -> str:
    return run_text(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])


def assert_powerset_gcloud_account() -> str:
    account = gcloud_account().splitlines()[0].strip()
    if not account:
        raise RuntimeError("no active gcloud account; run gcloud auth login")
    if not account.endswith("@powerset.co"):
        raise PermissionError(f"refusing to provision secrets for non-powerset.co gcloud account: {account}")
    return account


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


def fetch_from_gcp(
    keys: list[str],
    mapping: dict[str, str],
    project: str | None,
    *,
    best_effort: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    secrets: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key in keys:
        command = ["gcloud", "secrets", "versions", "access", "latest", "--secret", mapping[key]]
        if project:
            command.extend(["--project", project])
        proc = subprocess.run(command, text=True, capture_output=True)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "command failed").strip()
            if not best_effort:
                raise RuntimeError(message)
            errors[key] = message[:300]
            continue
        value = proc.stdout.strip()
        if value:
            secrets[key] = value
        elif best_effort:
            errors[key] = "secret value was empty"
    return secrets, errors


# ---------------------------------------------------------------------------
# Per-user Secret Manager scope
# ---------------------------------------------------------------------------

def _email_slug(email: str) -> str:
    """Derive the per-user secret-name slug from an email.

    The local part (before `@`) is lower-cased and reduced to ASCII letters,
    digits, and dashes so it composes safely with Secret Manager's flat
    namespace (e.g. `arthur@powerset.co` -> `arthur`).
    """
    local = (email or "").split("@", 1)[0].lower()
    cleaned = []
    for ch in local:
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in (".", "_", "-"):
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    return slug or "unknown"


def per_user_secret_id(default_id: str, email: str) -> str:
    """Compose a per-user Secret Manager id from a base id + email.

    `powerpacks-turbopuffer-api-key` + `arthur@powerset.co`
        -> `powerpacks-users-arthur-turbopuffer-api-key`
    """
    slug = _email_slug(email)
    suffix = default_id
    if suffix.startswith("powerpacks-"):
        suffix = suffix[len("powerpacks-"):]
    return f"powerpacks-users-{slug}-{suffix}"


def per_user_secret_map(
    overrides: list[str] | None, email: str
) -> dict[str, str]:
    """Like `secret_map`, but every default id is rewritten as a per-user id."""
    base = secret_map(overrides)
    return {key: per_user_secret_id(value, email) for key, value in base.items()}


def _gcloud_describe_secret(name: str, project: str | None) -> dict[str, Any]:
    """Read-only IAM probe for a Secret Manager id.

    Returns `{exists, accessible, error}`. Uses `secrets describe` so the
    secret value is never fetched. `accessible=True` means the active
    gcloud account can read the secret resource (does not mean it can read
    the latest version, but that grant is normally bundled).
    """
    command = ["gcloud", "secrets", "describe", name, "--format=value(name)"]
    if project:
        command.extend(["--project", project])
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode == 0:
        return {"exists": True, "accessible": True, "error": None}
    raw_err = (proc.stderr or proc.stdout or "").strip()
    err = raw_err.lower()
    if (
        "problem refreshing your current auth tokens" in err
        or "reauthentication failed" in err
        or "gcloud auth login" in err
    ):
        return {"exists": False, "accessible": False, "error": "gcloud_auth_error", "detail": raw_err[:200]}
    if "permission_denied" in err or "permission denied" in err or "forbidden" in err:
        return {"exists": True, "accessible": False, "error": "permission_denied"}
    if "not_found" in err or "not found" in err or "does not exist" in err:
        return {"exists": False, "accessible": False, "error": "not_found"}
    return {
        "exists": False,
        "accessible": False,
        "error": (proc.stderr or proc.stdout or "unknown").strip()[:200],
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
        "source": "gcp",
        "gcp_project": args.gcp_project,
        "note": "pull requires active @powerset.co gcloud auth and --confirm",
    })
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """IAM-probe per-user Secret Manager resources without writing anything.

    Resolves expected per-user secret ids for `--email` (or the active
    gcloud account) and reports which the user can read. No values are
    fetched. Always exits 0; the structured payload is the answer.
    """
    keys = requested_keys(args.profile, args.include)
    base_map = secret_map(getattr(args, "secret", None))

    email = args.email
    if not email:
        try:
            email = gcloud_account().splitlines()[0].strip()
        except RuntimeError as exc:
            emit({
                "primitive": "provision_runtime_env",
                "command": "probe",
                "status": "gcloud_missing",
                "profile": args.profile,
                "requested_keys": keys,
                "error": str(exc),
                "message": (
                    "gcloud is not signed in. Run `gcloud auth login` and"
                    " re-run, or pass --email to probe a specific user."
                ),
            })
            return 0

    results: list[dict[str, Any]] = []
    accessible_ids: list[str] = []
    denied_ids: list[str] = []
    missing_ids: list[str] = []
    auth_error_ids: list[str] = []
    for key in keys:
        secret_id = per_user_secret_id(base_map[key], email)
        probe = _gcloud_describe_secret(secret_id, args.gcp_project)
        results.append({"key": key, "secret_id": secret_id, **probe})
        if probe.get("accessible"):
            accessible_ids.append(key)
        elif probe.get("error") == "not_found":
            missing_ids.append(key)
        elif probe.get("error") == "gcloud_auth_error":
            auth_error_ids.append(key)
        else:
            denied_ids.append(key)

    if accessible_ids and not denied_ids and not missing_ids and not auth_error_ids:
        status = "ok"
    elif accessible_ids:
        status = "partial"
    elif auth_error_ids and not denied_ids and not missing_ids:
        status = "gcloud_auth_error"
    elif missing_ids and not denied_ids:
        status = "not_provisioned"
    else:
        status = "not_privileged"

    payload = {
        "primitive": "provision_runtime_env",
        "command": "probe",
        "status": status,
        "profile": args.profile,
        "email": email,
        "slug": _email_slug(email),
        "gcp_project": args.gcp_project,
        "results": results,
        "accessible": accessible_ids,
        "denied": denied_ids,
        "not_provisioned": missing_ids,
        "auth_error": auth_error_ids,
    }
    if status == "gcloud_auth_error":
        payload["message"] = "gcloud is signed in but its credentials need reauthentication; run `gcloud auth login` and retry."
    elif status != "ok":
        payload["message"] = _CONTACT_MESSAGE
    emit(payload)
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


_CONTACT_MESSAGE = (
    "You are signed in but do not have access to Powerset's shared runtime"
    " secrets. Contact a Powerpacks maintainer (#powerpacks on Slack) to be"
    " added, or bring your own keys via .env."
)


def cmd_pull(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise SystemExit("refusing to write secrets without --confirm")
    keys = requested_keys(args.profile, args.include)
    base_map = secret_map(args.secret)
    # Per-user is the production model. Each gcloud account reads only its
    # own `powerpacks-users-<slug>-<capability>` secrets. Pass --shared to
    # fall back to the legacy flat mapping (for emergency / shared-key debug).
    use_per_user = not args.shared

    # Best-effort: if gcloud is missing or the active account is not allowed,
    # don't error — emit a structured "not_privileged" status so the skill can
    # tell the user what to do without dropping them out of the flow.
    try:
        account = assert_powerset_gcloud_account()
    except RuntimeError as exc:  # gcloud not signed in
        emit({
            "primitive": "provision_runtime_env",
            "command": "pull",
            "status": "gcloud_missing",
            "profile": args.profile,
            "env_file": str(args.env_file),
            "requested_keys": keys,
            "error": str(exc),
            "message": (
                "gcloud is not signed in. Run `gcloud auth login` to populate"
                " Powerset's shared runtime keys, or skip this step and bring"
                " your own keys in .env."
            ),
        })
        return 0 if args.best_effort else 1
    except PermissionError as exc:
        emit({
            "primitive": "provision_runtime_env",
            "command": "pull",
            "status": "not_privileged",
            "profile": args.profile,
            "env_file": str(args.env_file),
            "requested_keys": keys,
            "error": str(exc),
            "message": _CONTACT_MESSAGE,
        })
        return 0 if args.best_effort else 1

    if use_per_user:
        scope_email = args.email or account
        mapping = {key: per_user_secret_id(base_map[key], scope_email) for key in keys}
        scope = {"mode": "per_user", "email": scope_email, "slug": _email_slug(scope_email)}
    else:
        mapping = base_map
        scope = {"mode": "shared"}

    try:
        secrets, fetch_errors = fetch_from_gcp(
            keys,
            mapping,
            args.gcp_project,
            best_effort=args.best_effort,
        )
    except RuntimeError as exc:
        # Could be IAM (denied on every secret), missing project, network, etc.
        emit({
            "primitive": "provision_runtime_env",
            "command": "pull",
            "status": "not_privileged",
            "profile": args.profile,
            "env_file": str(args.env_file),
            "requested_keys": keys,
            "gcloud_account": account,
            "gcp_project": args.gcp_project,
            "error": str(exc),
            "message": _CONTACT_MESSAGE,
        })
        return 0 if args.best_effort else 1

    missing = [key for key in keys if key not in secrets]
    result = write_env(args.env_file, secrets, overwrite=args.overwrite)
    if not secrets:
        status = "not_privileged"
    elif missing:
        status = "partial"
    else:
        status = "ok"
    payload = {
        "primitive": "provision_runtime_env",
        "command": "pull",
        "status": status,
        "source": "gcp",
        "profile": args.profile,
        "scope": scope,
        "gcloud_account": account,
        "gcp_project": args.gcp_project,
        "env_file": str(args.env_file),
        "secret_ids": dict(sorted(mapping.items())),
        "written": result["written"],
        "skipped_existing": result["skipped_existing"],
        "missing": missing,
        "fetch_errors": fetch_errors,
        "secrets": redact_keys(secrets),
    }
    if status == "not_privileged":
        payload["message"] = _CONTACT_MESSAGE
    emit(payload)
    if status == "ok":
        return 0
    if args.best_effort:
        return 0
    return 1


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=sorted(PROFILES), default="search-core")
    parser.add_argument("--include", action="append", choices=sorted(ALLOWED_KEYS))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--gcp-project", default=os.environ.get("POWERPACKS_GCP_PROJECT"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision Powerpacks runtime env")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Show required runtime keys without fetching secrets")
    add_common(plan)
    plan.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="Check whether the local env file has required keys")
    add_common(check)
    check.set_defaults(func=cmd_check)

    probe = sub.add_parser(
        "probe",
        help=(
            "IAM-probe per-user Secret Manager resources without fetching"
            " values. Always exits 0; the structured payload describes"
            " exactly what the user can read."
        ),
    )
    add_common(probe)
    probe.add_argument("--email", help="Email to scope the probe to (default: active gcloud account)")
    probe.set_defaults(func=cmd_probe)

    pull = sub.add_parser("pull", help="Fetch secrets and merge them into an env file")
    add_common(pull)
    pull.add_argument("--confirm", action="store_true")
    pull.add_argument("--overwrite", action="store_true")
    pull.add_argument("--secret", action="append", help="Override Secret Manager id as KEY=SECRET_ID")
    pull.add_argument(
        "--email",
        help=(
            "User email scope for per-user secrets (default: active gcloud account)."
            " Ignored with --shared."
        ),
    )
    pull.add_argument(
        "--shared",
        action="store_true",
        help=(
            "Fall back to the legacy flat shared-secret mapping. Default is"
            " per-user (powerpacks-users-<slug>-<capability>)."
        ),
    )
    pull.add_argument(
        "--best-effort",
        action="store_true",
        help=(
            "Exit 0 even when gcloud is missing or the user is not privileged."
            " Used by the $powerset login flow so unprivileged users still get"
            " their Auth0 JWT and a clear contact-us message."
        ),
    )
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
