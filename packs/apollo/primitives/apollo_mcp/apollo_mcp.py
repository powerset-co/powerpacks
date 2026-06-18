#!/usr/bin/env python3
"""Install/status helpers and local lead handoff for the Apollo.io MCP.

Powerpacks does not vendor an Apollo server. It registers the public stdio MCP
package with local hosts and keeps Apollo API keys out of chat output.

Stdlib-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_NAME = os.environ.get("POWERPACKS_APOLLO_MCP_NAME", "apollo")
DEFAULT_PACKAGE = os.environ.get("POWERPACKS_APOLLO_MCP_PACKAGE", "apollo-mcp@0.2.0")
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
DEFAULT_CLAUDE_SCOPE = os.environ.get("POWERPACKS_APOLLO_MCP_SCOPE", "user")
DEFAULT_HOST = os.environ.get("POWERPACKS_APOLLO_MCP_HOST", "codex")
SUPPORTED_HOSTS = ("codex", "claude")

LINKEDIN_COLUMNS = (
    "linkedin_url",
    "linkedin",
    "linkedin_profile_url",
    "profile_url",
)
EMAIL_COLUMNS = ("email", "email_address", "work_email", "business_email")
NAME_COLUMNS = ("name", "full_name", "person_name")
FIRST_NAME_COLUMNS = ("first_name", "firstname", "given_name")
LAST_NAME_COLUMNS = ("last_name", "lastname", "family_name", "surname")
TITLE_COLUMNS = ("title", "job_title", "headline", "current_title")
COMPANY_COLUMNS = ("organization_name", "company", "company_name", "current_company")
LOCATION_COLUMNS = ("present_raw_address", "location", "person_location")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run(cmd: list[str], *, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def api_key_from_env(env_file: Path | None) -> tuple[str | None, str | None]:
    key = os.environ.get("APOLLO_API_KEY")
    if key:
        return key.strip(), "environment"
    if env_file:
        values = load_dotenv(env_file)
        key = values.get("APOLLO_API_KEY")
        if key:
            return key.strip(), str(env_file)
    return None, None


def redact(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"


def redact_secret_text(text: str, secret: str | None = None) -> str:
    if not text:
        return text
    redacted = text
    if secret:
        redacted = redacted.replace(secret, "<REDACTED>")
    redacted = re.sub(r"APOLLO_API_KEY=[^\s,}]+", "APOLLO_API_KEY=<REDACTED>", redacted)
    redacted = re.sub(r'("APOLLO_API_KEY"\s*:\s*")[^"]+', r'\1<REDACTED>', redacted)
    redacted = re.sub(r'(APOLLO_API_KEY\s*=\s*")[^"]+', r'\1<REDACTED>', redacted)
    return redacted


def toml_string(value: str) -> str:
    return json.dumps(value)


def toml_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def codex_config_path() -> Path:
    return DEFAULT_CODEX_HOME / "config.toml"


def remove_toml_sections(text: str, section_names: set[str]) -> str:
    out: list[str] = []
    skip = False
    header_re = re.compile(r"^\[([^\]]+)\]\s*$")
    for line in text.splitlines():
        match = header_re.match(line.strip())
        if match:
            skip = match.group(1) in section_names
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + ("\n" if out else "")


def write_codex_stdio(name: str, package: str, api_key: str) -> Path:
    config_path = codex_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text() if config_path.exists() else ""
    section = f"mcp_servers.{name}"
    existing = remove_toml_sections(existing, {section})
    addition = (
        f"\n[{section}]\n"
        f"command = \"npx\"\n"
        f"args = {toml_array(['-y', package])}\n"
        f"startup_timeout_sec = 30\n"
        f"env = {{ APOLLO_API_KEY = {toml_string(api_key)} }}\n"
    )
    config_path.write_text(existing.rstrip() + addition)
    return config_path


def codex_status(name: str) -> dict[str, Any]:
    config_path = codex_config_path()
    if not config_path.exists():
        return {"host": "codex", "installed": False, "error": "Codex config not found"}
    text = config_path.read_text()
    section_re = re.compile(rf"^\[mcp_servers\.{re.escape(name)}\]\s*$", re.MULTILINE)
    if not section_re.search(text):
        return {"host": "codex", "installed": False, "config_path": str(config_path)}
    section_start = text.find(f"[mcp_servers.{name}]")
    next_section = text.find("\n[", section_start + 1)
    section_text = text[section_start:] if next_section == -1 else text[section_start:next_section]
    has_key = "APOLLO_API_KEY" in section_text
    package_match = re.search(r"args\s*=\s*\[(.*?)\]", section_text, re.DOTALL)
    return {
        "host": "codex",
        "installed": True,
        "config_path": str(config_path),
        "has_api_key_env": has_key,
        "args": package_match.group(1).strip()[:120] if package_match else None,
    }


def codex_install(name: str, package: str, api_key: str) -> dict[str, Any]:
    config_path = write_codex_stdio(name, package, api_key)
    return {
        "host": "codex",
        "ok": True,
        "name": name,
        "package": package,
        "config_path": str(config_path),
        "api_key": redact(api_key),
        "token_handling": "APOLLO_API_KEY stored in Codex MCP config env; re-install to rotate",
    }


def codex_remove(name: str) -> dict[str, Any]:
    config_path = codex_config_path()
    if not config_path.exists():
        return {"host": "codex", "ok": True, "skipped": True, "reason": "config not found"}
    existing = config_path.read_text()
    section = f"mcp_servers.{name}"
    updated = remove_toml_sections(existing, {section})
    config_path.write_text(updated)
    return {"host": "codex", "ok": True, "config_path": str(config_path)}


def host_cli(host: str) -> str | None:
    return shutil.which(host)


def claude_status(name: str) -> dict[str, Any]:
    if not host_cli("claude"):
        return {"host": "claude", "installed": False, "error": "claude CLI not on PATH"}
    code, out, err = run(["claude", "mcp", "get", name], timeout=15)
    if code != 0:
        return {"host": "claude", "installed": False, "error": redact_secret_text(err or out or "not registered").strip()[:200]}
    details = redact_secret_text(out.strip())
    return {"host": "claude", "installed": True, "details": details[:500]}


def claude_install(name: str, package: str, scope: str, api_key: str, *, replace: bool = False) -> dict[str, Any]:
    if not host_cli("claude"):
        return {"host": "claude", "ok": False, "error": "claude CLI not on PATH"}
    state = claude_status(name)
    replaced = bool(state.get("installed"))
    if replaced:
        if not replace:
            return {
                "host": "claude",
                "ok": False,
                "error": f"MCP server {name!r} is already installed; pass --replace or remove it first",
            }
        run(["claude", "mcp", "remove", "--scope", scope, name], timeout=15)
    add_cmd = [
        "claude",
        "mcp",
        "add",
        "--scope",
        scope,
        "--transport",
        "stdio",
        "--env",
        f"APOLLO_API_KEY={api_key}",
        name,
        "--",
        "npx",
        "-y",
        package,
    ]
    code, out, err = run(add_cmd, timeout=30)
    if code != 0:
        return {
            "host": "claude",
            "ok": False,
            "error": redact_secret_text(err or out or "claude mcp add failed", api_key).strip()[:400],
            "command_line": ["APOLLO_API_KEY=<REDACTED>" if a.startswith("APOLLO_API_KEY=") else a for a in add_cmd],
        }
    return {
        "host": "claude",
        "ok": True,
        "name": name,
        "package": package,
        "scope": scope,
        "replaced_existing": replaced,
        "token_handling": "APOLLO_API_KEY stored in Claude MCP config env; re-install to rotate",
    }


def claude_remove(name: str, scope: str) -> dict[str, Any]:
    if not host_cli("claude"):
        return {"host": "claude", "ok": False, "skipped": True, "reason": "claude CLI not on PATH"}
    state = claude_status(name)
    if not state.get("installed"):
        return {"host": "claude", "ok": True, "skipped": True, "reason": "not registered"}
    code, out, err = run(["claude", "mcp", "remove", "--scope", scope, name], timeout=15)
    return {"host": "claude", "ok": code == 0, "scope": scope, "error": (err or out or "").strip() if code != 0 else None}


def wanted_hosts(host: str) -> list[str]:
    return list(SUPPORTED_HOSTS) if host == "all" else [host]


def status(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file) if args.env_file else None
    api_key, source = api_key_from_env(env_file)
    result: dict[str, Any] = {
        "ok": bool(api_key),
        "name": args.name,
        "package": args.package,
        "api_key_present": bool(api_key),
        "api_key_source": source,
        "npx_present": bool(shutil.which("npx")),
        "node_present": bool(shutil.which("node")),
        "hosts": [],
    }
    for host in wanted_hosts(args.host):
        result["hosts"].append(codex_status(args.name) if host == "codex" else claude_status(args.name))
    if not api_key:
        result["next_action"] = "Set APOLLO_API_KEY in your shell or .env, then run install. Apollo sequences generally require a Master API key."
    emit(result)


def install(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file) if args.env_file else None
    api_key, source = api_key_from_env(env_file)
    if not api_key:
        emit({
            "ok": False,
            "error": "missing APOLLO_API_KEY",
            "next_action": "Create an Apollo API key at app.apollo.io → Settings → Integrations → Apollo API, add APOLLO_API_KEY to .env or your shell, then rerun install. Use a Master API key for sequences/email accounts.",
        })
        raise SystemExit(2)
    results = []
    for host in wanted_hosts(args.host):
        if host == "codex":
            results.append(codex_install(args.name, args.package, api_key))
        elif host == "claude":
            results.append(claude_install(args.name, args.package, args.scope, api_key, replace=args.replace))
    emit({
        "ok": all(r.get("ok") for r in results),
        "name": args.name,
        "package": args.package,
        "api_key_source": source,
        "hosts": results,
        "next_action": "Restart the MCP host so it loads the Apollo MCP server.",
    })


def remove(args: argparse.Namespace) -> None:
    results = []
    for host in wanted_hosts(args.host):
        results.append(codex_remove(args.name) if host == "codex" else claude_remove(args.name, args.scope))
    emit({"ok": all(r.get("ok") for r in results), "name": args.name, "hosts": results})


def normalized_header_map(fieldnames: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name in fieldnames or []:
        key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        if key:
            mapping[key] = name
    return mapping


def pick(row: dict[str, str], mapping: dict[str, str], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        original = mapping.get(candidate)
        if original is not None:
            value = (row.get(original) or "").strip()
            if value:
                return value
    return ""


def split_name(name: str) -> tuple[str, str]:
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def normalize_linkedin_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("linkedin.com/") or value.startswith("www.linkedin.com/"):
        value = "https://" + value
    lowered = value.lower()
    if "linkedin.com/in/" not in lowered and "linkedin.com/pub/" not in lowered:
        return ""
    return value


def dedupe_key(contact: dict[str, Any]) -> str:
    email = str(contact.get("email") or "").strip().lower()
    linkedin = str(contact.get("linkedin_url") or "").strip().rstrip("/").lower()
    if email:
        return f"email:{email}"
    if linkedin:
        return f"linkedin:{linkedin}"
    raw = "|".join(str(contact.get(k, "")).lower() for k in ("first_name", "last_name", "organization_name"))
    return "row:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def chunked(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def prepare_leads(args: argparse.Namespace) -> None:
    if args.batch_size <= 0:
        emit({"ok": False, "error": "--batch-size must be a positive integer"})
        raise SystemExit(2)
    if args.enrich_batch_size <= 0:
        emit({"ok": False, "error": "--enrich-batch-size must be a positive integer"})
        raise SystemExit(2)
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        emit({"ok": False, "error": f"input not found: {input_path}"})
        raise SystemExit(2)
    run_id = args.run_id or f"apollo-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    out_dir = Path(args.out_dir or Path(".powerpacks") / "apollo" / run_id).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    contacts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    with input_path.open(newline="") as fh:
        reader = CsvIO.dict_reader(fh)
        mapping = normalized_header_map(reader.fieldnames)
        for idx, row in enumerate(reader, start=2):
            name = pick(row, mapping, NAME_COLUMNS)
            first = pick(row, mapping, FIRST_NAME_COLUMNS)
            last = pick(row, mapping, LAST_NAME_COLUMNS)
            if not first and not last and name:
                first, last = split_name(name)
            email = pick(row, mapping, EMAIL_COLUMNS)
            linkedin_url = normalize_linkedin_url(pick(row, mapping, LINKEDIN_COLUMNS))
            contact = {
                "first_name": first,
                "last_name": last,
                "email": email,
                "title": pick(row, mapping, TITLE_COLUMNS),
                "organization_name": pick(row, mapping, COMPANY_COLUMNS),
                "linkedin_url": linkedin_url,
                "present_raw_address": pick(row, mapping, LOCATION_COLUMNS),
            }
            contact = {k: v for k, v in contact.items() if v}
            if not (contact.get("email") or contact.get("linkedin_url") or (contact.get("first_name") and contact.get("last_name"))):
                skipped.append({"row": idx, "reason": "missing email/linkedin/name"})
                continue
            key = dedupe_key(contact)
            if key in seen:
                skipped.append({"row": idx, "reason": "duplicate"})
                continue
            seen.add(key)
            contacts.append(contact)

    enrich_requests = [
        {k: v for k, v in contact.items() if k in {"first_name", "last_name", "email", "linkedin_url", "organization_name"}}
        for contact in contacts
        if contact.get("linkedin_url") and not contact.get("email")
    ]
    create_ready_contacts = [
        contact for contact in contacts
        if contact.get("first_name") and contact.get("last_name") and contact.get("email")
    ]
    manual_review_contacts = [
        contact for contact in contacts
        if contact not in create_ready_contacts
    ]
    contact_batches = chunked(create_ready_contacts, args.batch_size)
    enrich_batches = chunked(enrich_requests, min(args.enrich_batch_size, 10))

    contacts_path = out_dir / "contacts.json"
    create_ready_path = out_dir / "create_ready_contacts.json"
    manual_review_path = out_dir / "manual_review_contacts.json"
    enrich_path = out_dir / "enrich_requests.json"
    batches_path = out_dir / "contact_batches.json"
    enrich_batches_path = out_dir / "enrich_batches.json"
    manifest_path = out_dir / "manifest.json"
    contacts_path.write_text(json.dumps(contacts, indent=2, sort_keys=True) + "\n")
    create_ready_path.write_text(json.dumps(create_ready_contacts, indent=2, sort_keys=True) + "\n")
    manual_review_path.write_text(json.dumps(manual_review_contacts, indent=2, sort_keys=True) + "\n")
    enrich_path.write_text(json.dumps(enrich_requests, indent=2, sort_keys=True) + "\n")
    batches_path.write_text(json.dumps(contact_batches, indent=2, sort_keys=True) + "\n")
    enrich_batches_path.write_text(json.dumps(enrich_batches, indent=2, sort_keys=True) + "\n")
    manifest = {
        "ok": True,
        "created_at": now_iso(),
        "input": str(input_path),
        "out_dir": str(out_dir),
        "contacts_path": str(contacts_path),
        "create_ready_contacts_path": str(create_ready_path),
        "manual_review_contacts_path": str(manual_review_path),
        "enrich_requests_path": str(enrich_path),
        "contact_batches_path": str(batches_path),
        "enrich_batches_path": str(enrich_batches_path),
        "contacts": len(contacts),
        "create_ready_contacts": len(create_ready_contacts),
        "needs_enrichment_or_review": len(manual_review_contacts),
        "with_email": sum(1 for c in contacts if c.get("email")),
        "linkedin_only": sum(1 for c in contacts if c.get("linkedin_url") and not c.get("email")),
        "skipped": len(skipped),
        "skipped_rows": skipped[:50],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    emit(manifest | {"manifest_path": str(manifest_path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apollo.io MCP setup and lead handoff helper")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--name", default=DEFAULT_NAME, help="MCP server name (default: apollo)")
        p.add_argument("--package", default=DEFAULT_PACKAGE, help="npm package passed to npx")
        p.add_argument("--host", choices=("all", *SUPPORTED_HOSTS), default=DEFAULT_HOST)
        p.add_argument("--env-file", default=".env", help="dotenv file to read APOLLO_API_KEY from")
        p.add_argument("--scope", default=DEFAULT_CLAUDE_SCOPE, help="Claude Code MCP scope")

    p_status = sub.add_parser("status", help="Show Apollo MCP readiness without printing secrets")
    add_common(p_status)
    p_status.set_defaults(func=status)

    p_install = sub.add_parser("install", help="Register Apollo MCP in Codex and/or Claude Code")
    add_common(p_install)
    p_install.add_argument("--replace", action="store_true", help="Replace an existing Claude MCP registration")
    p_install.set_defaults(func=install)

    p_remove = sub.add_parser("remove", help="Remove Apollo MCP registration")
    add_common(p_remove)
    p_remove.set_defaults(func=remove)

    p_prepare = sub.add_parser("prepare-leads", help="Convert a CSV export into Apollo MCP contact/enrichment batches")
    p_prepare.add_argument("--input", required=True, help="CSV export from search-network or sales-nav")
    p_prepare.add_argument("--out-dir", help="Output directory (default: .powerpacks/apollo/<run-id>)")
    p_prepare.add_argument("--run-id", help="Stable run id for default output dir")
    p_prepare.add_argument("--batch-size", type=int, default=25, help="Contact batch size for bulk_create_contacts")
    p_prepare.add_argument("--enrich-batch-size", type=int, default=10, help="Enrichment batch size; Apollo MCP caps bulk enrich at 10")
    p_prepare.set_defaults(func=prepare_leads)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
