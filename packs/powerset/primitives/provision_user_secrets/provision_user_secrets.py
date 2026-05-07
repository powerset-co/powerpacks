#!/usr/bin/env python3
"""Provision per-user Powerpacks secrets in GCP Secret Manager.

For each user email, this primitive:

1. Reads source values from existing prod shared secrets (or assembles them,
   e.g. a Postgres URL from host/port/db/user/password).
2. Creates per-user Secret Manager resources following the convention
   `powerpacks-users-<slug>-<capability>` so the user-scoped flow in
   `provision_runtime_env probe` / `pull` can read them.
3. Grants `roles/secretmanager.secretAccessor` on each per-user secret to
   only that user.

Stdlib-only. Idempotent: re-running `apply` skips already-correct resources
and is safe.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PROJECT = "powerset-search"


# Per-capability provisioning recipe. `env_key` is the .env variable name. The
# per-user Secret Manager id is computed as
# `powerpacks-users-<slug>-<env_key_suffix>` where `env_key_suffix` is shared
# with the convention used by `provision_runtime_env.per_user_secret_id`.
PROVISION_PLAN: list[dict[str, Any]] = [
    {
        "env_key": "TURBOPUFFER_API_KEY",
        "secret_suffix": "turbopuffer-api-key",
        "capability": "turbopuffer",
        "profile": "search-core",
        "source": {"kind": "secret", "name": "turbopuffer-api-key"},
    },
    {
        "env_key": "OPENAI_API_KEY",
        "secret_suffix": "openai-api-key",
        "capability": "openai",
        "profile": "search-core",
        "source": {"kind": "secret", "name": "openai-api-key"},
    },
    {
        "env_key": "OPENROUTER_API_KEY",
        "secret_suffix": "openrouter-api-key",
        "capability": "openrouter",
        "profile": "search-core",
        "source": {"kind": "secret", "name": "openrouter-api-key"},
    },
    {
        "env_key": "PARALLEL_API_KEY",
        "secret_suffix": "parallel-api-key",
        "capability": "parallel",
        "profile": "search-core",
        "source": {"kind": "secret", "name": "parallel-api-key"},
    },
    {
        "env_key": "DATABASE_URL",
        "secret_suffix": "database-url",
        "capability": "database",
        "profile": "search-core",
        "source": {
            "kind": "assemble_postgres_url",
            "secrets": {
                "host": "postgres-host",
                "port": "postgres-port",
                "db": "postgres-db",
                "user": "postgres-user",
                "password": "postgres-password",
            },
        },
    },
    {
        "env_key": "RAPIDAPI_LINKEDIN_KEY",
        "secret_suffix": "rapidapi-linkedin-key",
        "capability": "rapidapi-linkedin",
        "profile": "sales-nav",
        "source": {"kind": "secret", "name": "rapidapi-key"},
    },
    {
        "env_key": "RAPIDAPI_TWITTER_KEY",
        "secret_suffix": "rapidapi-twitter-key",
        "capability": "rapidapi-twitter",
        "profile": "twitter",
        "source": {"kind": "secret", "name": "rapidapi-twitter-key"},
    },
    {
        "env_key": "SUPABASE_SERVICE_ROLE_KEY",
        "secret_suffix": "supabase-service-role-key",
        "capability": "supabase",
        "profile": "supabase-admin",
        "source": {"kind": "secret", "name": "supabase-secret-key"},
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def email_slug(email: str) -> str:
    """Same convention as provision_runtime_env._email_slug.

    `arthur@powerset.co` -> `arthur`. ASCII letters/digits only, dashes for
    `.`/`_`.
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


def per_user_secret_id(secret_suffix: str, email: str) -> str:
    return f"powerpacks-users-{email_slug(email)}-{secret_suffix}"


def gcloud_run(args: list[str], *, input_bytes: bytes | None = None,
               capture: bool = True) -> tuple[int, str, str]:
    """Run gcloud, return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        args,
        input=input_bytes,
        capture_output=capture,
        text=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return proc.returncode, stdout, stderr


def gcloud_text(args: list[str]) -> str:
    code, out, err = gcloud_run(args)
    if code != 0:
        raise RuntimeError((err or out or "gcloud failed").strip())
    return out.strip()


def assert_gcloud_available() -> None:
    if not shutil.which("gcloud"):
        raise RuntimeError("gcloud is not on PATH; install Google Cloud SDK first")


def active_account() -> str:
    return gcloud_text([
        "gcloud", "auth", "list",
        "--filter=status:ACTIVE",
        "--format=value(account)",
    ]).splitlines()[0].strip()


# ---------------------------------------------------------------------------
# Source value resolution
# ---------------------------------------------------------------------------

def access_secret(name: str, project: str) -> str:
    return gcloud_text([
        "gcloud", "secrets", "versions", "access", "latest",
        "--secret", name,
        "--project", project,
    ])


def assemble_postgres_url(secrets: dict[str, str], project: str) -> str:
    host = access_secret(secrets["host"], project).strip()
    port = access_secret(secrets["port"], project).strip()
    db = access_secret(secrets["db"], project).strip()
    user = access_secret(secrets["user"], project).strip()
    password = access_secret(secrets["password"], project).strip()
    enc_user = urllib.parse.quote(user, safe="")
    enc_pw = urllib.parse.quote(password, safe="")
    return f"postgresql://{enc_user}:{enc_pw}@{host}:{port}/{db}"


def resolve_source_value(spec: dict[str, Any], project: str) -> str:
    kind = spec["kind"]
    if kind == "secret":
        return access_secret(spec["name"], project)
    if kind == "assemble_postgres_url":
        return assemble_postgres_url(spec["secrets"], project)
    raise ValueError(f"unsupported source kind: {kind}")


def source_summary(spec: dict[str, Any]) -> dict[str, Any]:
    """Redacted description of the source (no values)."""
    if spec["kind"] == "secret":
        return {"kind": "secret", "name": spec["name"]}
    if spec["kind"] == "assemble_postgres_url":
        return {"kind": "assemble_postgres_url", "secrets": spec["secrets"]}
    return {"kind": spec["kind"]}


# ---------------------------------------------------------------------------
# GCP Secret Manager mutations
# ---------------------------------------------------------------------------

def secret_exists(name: str, project: str) -> bool:
    code, _, _ = gcloud_run([
        "gcloud", "secrets", "describe", name,
        "--project", project,
        "--format=value(name)",
    ])
    return code == 0


def latest_secret_value(name: str, project: str) -> str | None:
    code, out, _ = gcloud_run([
        "gcloud", "secrets", "versions", "access", "latest",
        "--secret", name,
        "--project", project,
    ])
    if code != 0:
        return None
    # gcloud appends a trailing newline; strip only if present.
    return out.rstrip("\n")


def create_secret(name: str, project: str, labels: dict[str, str]) -> None:
    label_args = []
    if labels:
        joined = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        label_args = ["--labels", joined]
    args = [
        "gcloud", "secrets", "create", name,
        "--project", project,
        "--replication-policy", "automatic",
        *label_args,
    ]
    code, out, err = gcloud_run(args)
    if code != 0:
        raise RuntimeError(f"create secret {name} failed: {(err or out).strip()}")


def add_secret_version(name: str, project: str, value: str) -> None:
    args = [
        "gcloud", "secrets", "versions", "add", name,
        "--project", project,
        "--data-file=-",
    ]
    code, out, err = gcloud_run(args, input_bytes=value.encode("utf-8"))
    if code != 0:
        raise RuntimeError(f"add version to {name} failed: {(err or out).strip()}")


def grant_accessor(name: str, project: str, email: str) -> None:
    args = [
        "gcloud", "secrets", "add-iam-policy-binding", name,
        "--project", project,
        "--member", f"user:{email}",
        "--role", "roles/secretmanager.secretAccessor",
        "--condition=None",
    ]
    code, out, err = gcloud_run(args)
    if code != 0:
        raise RuntimeError(f"iam binding for {name} -> {email} failed: {(err or out).strip()}")


def revoke_accessor(name: str, project: str, email: str) -> None:
    args = [
        "gcloud", "secrets", "remove-iam-policy-binding", name,
        "--project", project,
        "--member", f"user:{email}",
        "--role", "roles/secretmanager.secretAccessor",
        "--condition=None",
    ]
    code, out, err = gcloud_run(args)
    if code != 0:
        raise RuntimeError(f"revoke iam for {name} -> {email} failed: {(err or out).strip()}")


# ---------------------------------------------------------------------------
# Plan / apply
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def parse_users(raw: str) -> list[str]:
    out: list[str] = []
    for token in raw.replace(";", ",").replace(" ", ",").split(","):
        email = token.strip().lower()
        if not email:
            continue
        if not EMAIL_RE.match(email):
            raise ValueError(f"invalid email: {email}")
        if email not in out:
            out.append(email)
    if not out:
        raise ValueError("--users is required and must list at least one email")
    return out


def build_user_plan(email: str, project: str, *, capabilities: list[str] | None = None) -> dict[str, Any]:
    """Plan describing what would be created for a single user (no GCP writes)."""
    items: list[dict[str, Any]] = []
    for entry in PROVISION_PLAN:
        if capabilities and entry["capability"] not in capabilities:
            continue
        secret_name = per_user_secret_id(entry["secret_suffix"], email)
        items.append({
            "env_key": entry["env_key"],
            "secret_id": secret_name,
            "capability": entry["capability"],
            "profile": entry["profile"],
            "source": source_summary(entry["source"]),
            "labels": {
                "managed-by": "powerpacks",
                "owner": email_slug(email),
                "owner-email": email_slug(email),  # GCP labels can't contain @
                "capability": entry["capability"],
                "profile": entry["profile"],
            },
        })
    return {"email": email, "slug": email_slug(email), "project": project, "items": items}


def diff_for_user(plan: dict[str, Any]) -> dict[str, Any]:
    """Compare plan to current GCP state, no writes."""
    project = plan["project"]
    actions: list[dict[str, Any]] = []
    for item in plan["items"]:
        secret_id = item["secret_id"]
        exists = secret_exists(secret_id, project)
        action = {
            "env_key": item["env_key"],
            "secret_id": secret_id,
            "capability": item["capability"],
            "exists": exists,
            "create_secret": not exists,
            "add_version": True,
            "grant_iam": True,
        }
        if exists:
            existing_value = latest_secret_value(secret_id, project)
            action["latest_version_present"] = existing_value is not None
        actions.append(action)
    return {"email": plan["email"], "project": project, "actions": actions}


def apply_user_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Apply the plan idempotently. Returns a per-item result list."""
    project = plan["project"]
    email = plan["email"]
    results: list[dict[str, Any]] = []

    for item, source_entry in zip(plan["items"], _filtered_plan_entries(plan)):
        secret_id = item["secret_id"]
        result: dict[str, Any] = {
            "env_key": item["env_key"],
            "secret_id": secret_id,
            "capability": item["capability"],
            "created_secret": False,
            "added_version": False,
            "granted_iam": False,
            "skipped_version": False,
            "errors": [],
        }
        try:
            if not secret_exists(secret_id, project):
                create_secret(secret_id, project, item["labels"])
                result["created_secret"] = True

            new_value = resolve_source_value(source_entry["source"], project)
            existing_value = latest_secret_value(secret_id, project)
            if existing_value == new_value:
                result["skipped_version"] = True
            else:
                add_secret_version(secret_id, project, new_value)
                result["added_version"] = True

            grant_accessor(secret_id, project, email)
            result["granted_iam"] = True
        except RuntimeError as exc:
            result["errors"].append(str(exc))
        results.append(result)
    return {"email": email, "project": project, "results": results}


def _filtered_plan_entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return PROVISION_PLAN entries in the same order as plan['items']."""
    by_suffix = {entry["secret_suffix"]: entry for entry in PROVISION_PLAN}
    out = []
    for item in plan["items"]:
        for entry in PROVISION_PLAN:
            secret_id = per_user_secret_id(entry["secret_suffix"], plan["email"])
            if secret_id == item["secret_id"]:
                out.append(entry)
                break
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> int:
    assert_gcloud_available()
    users = parse_users(args.users)
    capabilities = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()] or None
    plans = [build_user_plan(u, args.project, capabilities=capabilities) for u in users]
    if args.with_diff:
        plans = [{"plan": p, "diff": diff_for_user(p)} for p in plans]
    emit({
        "primitive": "provision_user_secrets",
        "command": "plan",
        "project": args.project,
        "users": users,
        "capabilities": capabilities or [e["capability"] for e in PROVISION_PLAN],
        "plans": plans,
        "generated_at": now_iso(),
    })
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.confirm:
        emit({
            "primitive": "provision_user_secrets",
            "command": "apply",
            "status": "refused",
            "error": "apply requires --confirm",
        })
        return 1
    assert_gcloud_available()
    account = active_account()
    if not account.endswith("@powerset.co") and not args.allow_non_powerset_account:
        emit({
            "primitive": "provision_user_secrets",
            "command": "apply",
            "status": "refused",
            "error": f"refusing to provision from non-powerset.co gcloud account: {account}",
        })
        return 1
    users = parse_users(args.users)
    capabilities = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()] or None
    aggregated: list[dict[str, Any]] = []
    overall_ok = True
    for email in users:
        plan = build_user_plan(email, args.project, capabilities=capabilities)
        result = apply_user_plan(plan)
        aggregated.append(result)
        if any(r["errors"] for r in result["results"]):
            overall_ok = False
    emit({
        "primitive": "provision_user_secrets",
        "command": "apply",
        "status": "ok" if overall_ok else "partial",
        "project": args.project,
        "operator": account,
        "users": users,
        "capabilities": capabilities or [e["capability"] for e in PROVISION_PLAN],
        "results": aggregated,
        "applied_at": now_iso(),
    })
    return 0 if overall_ok else 2


def cmd_revoke(args: argparse.Namespace) -> int:
    if not args.confirm:
        emit({
            "primitive": "provision_user_secrets",
            "command": "revoke",
            "status": "refused",
            "error": "revoke requires --confirm",
        })
        return 1
    assert_gcloud_available()
    users = parse_users(args.users)
    capabilities = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()] or None
    out: list[dict[str, Any]] = []
    overall_ok = True
    for email in users:
        plan = build_user_plan(email, args.project, capabilities=capabilities)
        per_user: list[dict[str, Any]] = []
        for item in plan["items"]:
            secret_id = item["secret_id"]
            try:
                revoke_accessor(secret_id, args.project, email)
                per_user.append({"secret_id": secret_id, "revoked": True})
            except RuntimeError as exc:
                per_user.append({"secret_id": secret_id, "revoked": False, "error": str(exc)})
                overall_ok = False
        out.append({"email": email, "results": per_user})
    emit({
        "primitive": "provision_user_secrets",
        "command": "revoke",
        "status": "ok" if overall_ok else "partial",
        "project": args.project,
        "users": users,
        "capabilities": capabilities or [e["capability"] for e in PROVISION_PLAN],
        "results": out,
    })
    return 0 if overall_ok else 2


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--users", required=True,
                   help="Comma-separated user emails (e.g. arthur@powerset.co,jake@powerset.co)")
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--capabilities",
                   help="Optional comma-separated capability filter "
                        "(turbopuffer,openai,database,rapidapi,supabase). "
                        "Default: all in PROVISION_PLAN.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision per-user Powerpacks secrets in GCP")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Show what would be created (no writes)")
    add_common(plan)
    plan.add_argument("--with-diff", action="store_true",
                      help="Also probe GCP to mark which resources already exist")
    plan.set_defaults(func=cmd_plan)

    apply = sub.add_parser("apply", help="Create secrets, add versions, bind IAM")
    add_common(apply)
    apply.add_argument("--confirm", action="store_true")
    apply.add_argument("--allow-non-powerset-account", action="store_true",
                       help="Skip the @powerset.co operator check (for emergency runs only)")
    apply.set_defaults(func=cmd_apply)

    revoke = sub.add_parser("revoke", help="Remove IAM bindings (does not delete secrets)")
    add_common(revoke)
    revoke.add_argument("--confirm", action="store_true")
    revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except RuntimeError as exc:
        emit({
            "primitive": "provision_user_secrets",
            "command": args.command,
            "status": "failed",
            "error": str(exc),
        })
        raise SystemExit(1)


if __name__ == "__main__":
    main()
