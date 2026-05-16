#!/usr/bin/env python3
"""Resumable local Gmail network-import orchestrator.

V1 is intentionally small and safe: one person, local `.powerpacks/` artifacts,
no Gmail API, no paid APIs, no DVC, no uploads, no production writes.

The agent-facing contract mirrors messages/import_contacts_pipeline:

    run       start a fresh run and proceed until done or a future gate
    continue  resume from the ledger
    approve   approve the currently blocked future gate

Stdlib-only. No Gmail message bodies or subjects are read or written.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LEDGER = Path(".powerpacks/network-import/gmail/import-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_APP_GMAIL_URL = "https://search.powerset.dev/gmail"
DEFAULT_API_URL = "https://search-api-7wk4uhe77q-uw.a.run.app"
DEFAULT_MSGVAULT_DB = Path(os.environ.get("MSGVAULT_HOME", str(Path.home() / ".msgvault"))) / "msgvault.db"

PERSONAL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
    "msn.com",
    "live.com",
    "me.com",
    "mac.com",
    "protonmail.com",
    "mail.com",
    "ymail.com",
    "googlemail.com",
    "comcast.net",
    "att.net",
    "verizon.net",
    "sbcglobal.net",
    "cox.net",
    "earthlink.net",
    "126.com",
    "163.com",
    "qq.com",
}
NON_WORK_DOMAINS = PERSONAL_DOMAINS | {"noreply.github.com", "users.noreply.github.com"}
AUTOMATED_EMAIL_KEYWORDS = {
    "unsub",
    "unsubscribe",
    "bounce",
    "spam",
    "spamproc",
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "leave-",
    "void-",
    "reply.",
    "notification",
    "notifications",
    "alert",
    "alerts",
    "reservation",
    "reservations",
    "booking",
    "bookings",
}
SUPPORT_TICKET_DOMAINS = {"zendesk", "freshdesk", "intercom", "helpscout", "helpdesk"}
TRAVEL_SERVICE_DOMAINS = {
    "airbnb",
    "vrbo",
    "booking.com",
    "hotels.com",
    "expedia",
    "uber.com",
    "lyft.com",
    "united",
    "delta",
    "aa.com",
    "americanairlines",
    "southwest",
    "jetblue",
    "alaska",
    "marriott",
    "hilton",
    "hyatt",
    "hertz",
    "avis",
    "enterprise",
}

THREAD_COLUMNS = [
    "email",
    "display_name",
    "thread_id",
    "received_count",
    "sent_count",
    "message_count",
    "first_message_at",
    "last_message_at",
    "subject",
    "discovered_at",
]
AGGREGATED_COLUMNS = [
    "email",
    "display_name",
    "total_sent",
    "total_received",
    "total_messages",
    "thread_count",
    "first_interaction",
    "last_interaction",
    "sample_subjects",
]
TARGETED_COLUMNS = [
    "display_name",
    "primary_email",
    "primary_email_type",
    "all_emails",
    "email_count",
    "total_sent",
    "total_received",
    "total_messages",
    "thread_count",
    "first_interaction",
    "last_interaction",
    "is_duplicate",
    "potential_same_person_emails",
    "sample_subjects",
    "sample_calendar_titles",
]
ACCOUNT_COLUMNS = ["account_id", "account_email", "provider", "source", "added_at"]
PEOPLE_COLUMNS = [
    "id",
    "public_identifier",
    "linkedin_url",
    "first_name",
    "last_name",
    "full_name",
    "headline",
    "summary",
    "city",
    "state",
    "country",
    "location_raw",
    "profile_picture_url",
    "work_experiences",
    "education",
    "current_title",
    "current_company",
    "current_company_urn",
    "entity_urn",
    "enrichment_provider",
    "enriched_at",
    "harmonic_response",
    "harmonic_location",
    "rapidapi_response",
    "twitter_handle",
    "twitter_response",
    "primary_email",
    "all_emails",
    "primary_phone",
    "all_phones",
    "source_channels",
    "source_artifacts",
]
PIPELINE_STEPS = ["seed_one", "prepare_local_workspace", "write_next_steps"]


class PipelineBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or "blocked")
        self.payload = payload
        self.code = code


class PipelineFailed(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def powerset_token() -> str:
    cmd = [
        "uv",
        "run",
        "--project",
        str(repo_root()),
        "python",
        str(repo_root() / "packs/powerset/primitives/auth/auth.py"),
        "token",
        "--bearer-only",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise PipelineFailed((completed.stderr or completed.stdout or "Powerset login required").strip())
    token = completed.stdout.strip()
    if not token:
        raise PipelineFailed("Powerset login required: no access token returned")
    return token


def api_get_json(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise PipelineFailed(f"API request failed HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PipelineFailed(f"API request failed: {exc}") from exc


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not value or "@" not in value:
        raise ValueError("--email must contain a valid email address")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("--email must contain a valid email address")
    return value


def normalize_name(name: str, email: str = "") -> str:
    value = " ".join((name or "").strip().split())
    if value:
        return value
    local = email.split("@", 1)[0] if "@" in email else ""
    local = re.sub(r"[._+-]+", " ", local)
    return " ".join(part.capitalize() for part in local.split() if part)


def classify_email(email: str) -> str:
    if not email or "@" not in email:
        return "unknown"
    domain = email.rsplit("@", 1)[1].lower()
    if domain in PERSONAL_DOMAINS:
        return "personal"
    if domain in NON_WORK_DOMAINS:
        return "other"
    return "work"


def domain_guess(email: str) -> dict[str, str]:
    domain = email.rsplit("@", 1)[1].lower() if "@" in email else ""
    root = domain.split(".")[0] if domain else ""
    company_guess = " ".join(part.capitalize() for part in re.split(r"[-_]", root) if part)
    return {"domain": domain, "company_guess": company_guess, "method": "local_domain_heuristic"}


def parse_email_header(header_value: str) -> list[tuple[str, str]]:
    """Parse a Gmail From/To/Cc header into (display_name, email) pairs.

    Ported from the legacy Gmail ingestion path, but kept stdlib-only and local.
    """
    if not header_value:
        return []

    parts: list[str] = []
    current = ""
    in_quotes = False
    in_angle = False
    for char in header_value:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "<":
            in_angle = True
        elif char == ">":
            in_angle = False
        elif char == "," and not in_quotes and not in_angle:
            if current.strip():
                parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())

    results: list[tuple[str, str]] = []
    angle_pattern = r"^(.*?)\s*<([^>]+)>$"
    email_pattern = r"^[a-zA-Z0-9._%+-]{2,}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    for part in parts:
        value = part.strip()
        if not value:
            continue
        angle_match = re.match(angle_pattern, value)
        if angle_match:
            name = normalize_name(angle_match.group(1).strip().strip('"'))
            email = angle_match.group(2).strip().lower()
            if re.match(email_pattern, email):
                results.append((name, email))
        else:
            email = value.lower()
            if re.match(email_pattern, email):
                results.append(("", email))
    return results


def is_automated_email(email: str) -> tuple[bool, str]:
    if not email or "@" not in email:
        return True, "invalid email"
    local_part, domain = email.lower().rsplit("@", 1)
    for keyword in AUTOMATED_EMAIL_KEYWORDS:
        if keyword in local_part or keyword in domain:
            return True, f"contains '{keyword}'"
    for system in SUPPORT_TICKET_DOMAINS:
        if system in domain:
            return True, f"support ticket system ({system})"
    for service in TRAVEL_SERVICE_DOMAINS:
        if service in domain:
            return True, f"travel/hospitality service ({service})"
    if re.search(r"[a-f0-9]{16,}", local_part):
        return True, "hash-like pattern in email"
    if len(local_part) >= 20:
        vowel_count = sum(1 for c in local_part if c in "aeiou")
        vowel_ratio = vowel_count / len(local_part) if local_part else 0
        if vowel_ratio < 0.15 and re.match(r"^[a-z0-9_-]+$", local_part):
            return True, "random alphanumeric pattern"
    if len(local_part) > 40 and re.match(r"^[a-z0-9_-]+$", local_part):
        return True, "very long alphanumeric local part"
    return False, ""


@dataclass
class OnePersonInput:
    email: str
    display_name: str
    account_email: str = ""
    account_id: str = "local"
    operator_id: str = "local"
    total_sent: int = 1
    total_received: int = 1
    total_messages: int = 2
    thread_count: int = 1
    first_interaction: str = ""
    last_interaction: str = ""
    source: str = "gmail"

    @property
    def account_short(self) -> str:
        return short_hash(self.account_id or self.account_email or "local", 8)

    @property
    def operator_short(self) -> str:
        return (self.operator_id or "local")[:8]


def build_one_person(args: argparse.Namespace) -> OnePersonInput:
    email = normalize_email(args.email)
    account_email = normalize_email(args.account_email) if args.account_email else ""
    total_sent = max(0, int(args.total_sent))
    total_received = max(0, int(args.total_received))
    total_messages = int(args.total_messages) if args.total_messages is not None else total_sent + total_received
    total_messages = max(total_messages, total_sent + total_received)
    return OnePersonInput(
        email=email,
        display_name=normalize_name(args.name, email),
        account_email=account_email,
        account_id=args.account_id or account_email or "local",
        operator_id=args.operator_id or "local",
        total_sent=total_sent,
        total_received=total_received,
        total_messages=total_messages,
        thread_count=max(1, int(args.thread_count)),
        first_interaction=args.first_interaction or "",
        last_interaction=args.last_interaction or "",
    )


def run_dir(base_dir: Path, contact: OnePersonInput, run_id: str | None) -> Path:
    rid = run_id or f"gmail-one-{short_hash(contact.email + ':' + now_iso(), 12)}"
    return base_dir / "gmail" / rid


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def append_account(contact: OnePersonInput, out_dir: Path) -> Path:
    path = out_dir / "accounts.csv"
    rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    row = {
        "account_id": contact.account_id,
        "account_email": contact.account_email,
        "provider": "gmail",
        "source": "local_one_person_seed",
        "added_at": now_iso(),
    }
    if not any(r.get("account_id") == row["account_id"] for r in rows):
        rows.append(row)
    write_csv(path, ACCOUNT_COLUMNS, rows)
    return path


def split_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in re.split(r"\s+", (full_name or "").strip()) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def default_name_for_email(email: str) -> str:
    local = email.split("@", 1)[0] if "@" in email else email
    local = re.sub(r"[._+-]+", " ", local)
    return " ".join(part.capitalize() for part in local.split() if part)


def msgvault_db_uri(path: Path) -> str:
    return f"file:{path.expanduser().resolve()}?mode=ro"


def connect_msgvault(path: Path) -> sqlite3.Connection:
    db_path = path.expanduser()
    if not db_path.exists():
        raise SystemExit(f"msgvault database not found: {db_path}. Run msgvault sync-full first or pass --db.")
    try:
        con = sqlite3.connect(msgvault_db_uri(db_path), uri=True)
    except sqlite3.Error as exc:
        raise SystemExit(f"failed to open msgvault database read-only: {exc}") from exc
    con.row_factory = sqlite3.Row
    return con


def require_msgvault_schema(con: sqlite3.Connection) -> None:
    required = {"sources", "participants", "messages", "message_recipients"}
    rows = con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    present = {str(row[0]) for row in rows}
    missing = sorted(required - present)
    if missing:
        raise SystemExit(f"msgvault schema missing required tables: {', '.join(missing)}")


def iter_msgvault_metadata(con: sqlite3.Connection, account_email: str = "") -> Iterable[sqlite3.Row]:
    query = """
        SELECT
            s.id AS source_id,
            s.identifier AS account_email,
            s.display_name AS account_display_name,
            p.email_address AS email,
            p.display_name AS participant_display_name,
            mr.display_name AS recipient_display_name,
            LOWER(mr.recipient_type) AS recipient_type,
            m.id AS message_id,
            m.conversation_id AS conversation_id,
            COALESCE(m.sent_at, m.received_at, m.internal_date) AS message_at
        FROM message_recipients mr
        JOIN participants p ON p.id = mr.participant_id
        JOIN messages m ON m.id = mr.message_id
        JOIN sources s ON s.id = m.source_id
        WHERE p.email_address IS NOT NULL
          AND TRIM(p.email_address) != ''
          AND (m.message_type IS NULL OR m.message_type = '' OR m.message_type = 'email')
          AND (m.deleted_at IS NULL OR m.deleted_at = '')
          AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
          AND (? = '' OR LOWER(s.identifier) = LOWER(?))
        ORDER BY LOWER(p.email_address), m.id
    """
    yield from con.execute(query, (account_email, account_email))


def best_display_name(email: str, names: dict[str, int]) -> str:
    cleaned: dict[str, int] = {}
    email_l = email.lower()
    for name, count in names.items():
        value = normalize_name(name, email)
        if not value or value.lower() == email_l:
            continue
        cleaned[value] = cleaned.get(value, 0) + count
    if cleaned:
        return sorted(cleaned.items(), key=lambda item: (-item[1], item[0].casefold()))[0][0]
    return default_name_for_email(email)


def aggregate_msgvault_contacts(con: sqlite3.Connection, account_email: str = "") -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    account_filter = account_email.strip().lower()
    for row in iter_msgvault_metadata(con, account_filter):
        try:
            email = normalize_email(str(row["email"] or ""))
        except ValueError:
            continue
        source_account = str(row["account_email"] or "").strip().lower()
        if email == source_account or (account_filter and email == account_filter):
            continue
        record = records.setdefault(email, {
            "email": email,
            "names": {},
            "sent_messages": set(),
            "received_messages": set(),
            "all_messages": set(),
            "threads": set(),
            "accounts": set(),
            "source_ids": set(),
            "first_interaction": "",
            "last_interaction": "",
        })
        for name_key in ("recipient_display_name", "participant_display_name"):
            name = str(row[name_key] or "").strip()
            if name:
                record["names"][name] = int(record["names"].get(name, 0)) + 1
        msg_id = str(row["message_id"])
        record["all_messages"].add(msg_id)
        if row["conversation_id"] is not None:
            record["threads"].add(str(row["conversation_id"]))
        if row["source_id"] is not None:
            record["source_ids"].add(str(row["source_id"]))
        if source_account:
            record["accounts"].add(source_account)
        recipient_type = str(row["recipient_type"] or "")
        if recipient_type == "from":
            record["received_messages"].add(msg_id)
        elif recipient_type in {"to", "cc", "bcc"}:
            record["sent_messages"].add(msg_id)
        message_at = str(row["message_at"] or "").strip()
        if message_at:
            if not record["first_interaction"] or message_at < record["first_interaction"]:
                record["first_interaction"] = message_at
            if not record["last_interaction"] or message_at > record["last_interaction"]:
                record["last_interaction"] = message_at
    out: list[dict[str, Any]] = []
    for email, record in records.items():
        display_name = best_display_name(email, record["names"])
        automated, automated_reason = is_automated_email(email)
        out.append({
            "email": email,
            "display_name": display_name,
            "total_sent": len(record["sent_messages"]),
            "total_received": len(record["received_messages"]),
            "total_messages": len(record["all_messages"]),
            "thread_count": len(record["threads"]),
            "first_interaction": record["first_interaction"],
            "last_interaction": record["last_interaction"],
            "account_emails": sorted(record["accounts"]),
            "source_ids": sorted(record["source_ids"]),
            "primary_email_type": classify_email(email),
            "automated_filtered": automated,
            "automated_reason": automated_reason,
        })
    out.sort(key=lambda row: (-int(row["total_messages"]), str(row["email"])))
    return out


def people_rows_from_msgvault(rows: list[dict[str, Any]], source_artifacts: list[str]) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for row in rows:
        first_name, last_name = split_name(row.get("display_name") or "")
        person = {col: "" for col in PEOPLE_COLUMNS}
        person.update({
            "id": f"gmail:{short_hash(row['email'], 16)}",
            "first_name": first_name,
            "last_name": last_name,
            "full_name": row.get("display_name") or "",
            "enrichment_provider": "msgvault_metadata",
            "enriched_at": now_iso(),
            "primary_email": row["email"],
            "all_emails": json.dumps([row["email"]]),
            "source_channels": "gmail_msgvault",
            "source_artifacts": json.dumps(source_artifacts, ensure_ascii=False),
        })
        people.append(person)
    return people


def write_msgvault_artifacts(rows: list[dict[str, Any]], out_dir: Path, account_email: str = "", operator_id: str = "local", *, include_automated: bool = False, limit: int | None = None) -> dict[str, Any]:
    filtered = [row for row in rows if include_automated or not row.get("automated_filtered")]
    if limit is not None:
        filtered = filtered[: max(0, int(limit))]
    account_short = short_hash(account_email or "all", 8)
    op_short = (operator_id or "local")[:8]
    out_dir.mkdir(parents=True, exist_ok=True)
    threads_path = out_dir / f"gmail_threads_{account_short}_{op_short}.csv"
    aggregated_path = out_dir / f"gmail_contacts_aggregated_{account_short}_{op_short}.csv"
    targeted_path = out_dir / f"targeted_emails_{account_short}_{op_short}.csv"
    people_path = out_dir / "people.csv"
    accounts_path = out_dir / "accounts.csv"
    manifest_path = out_dir / "manifest.json"

    account_rows = []
    seen_accounts: set[str] = set()
    for row in filtered:
        for account in row.get("account_emails") or []:
            if account in seen_accounts:
                continue
            seen_accounts.add(account)
            account_rows.append({"account_id": f"msgvault:{short_hash(account, 12)}", "account_email": account, "provider": "gmail", "source": "msgvault", "added_at": now_iso()})
    if account_email and account_email not in seen_accounts:
        account_rows.append({"account_id": f"msgvault:{short_hash(account_email, 12)}", "account_email": account_email, "provider": "gmail", "source": "msgvault", "added_at": now_iso()})
    write_csv(accounts_path, ACCOUNT_COLUMNS, account_rows)

    write_csv(threads_path, THREAD_COLUMNS, [{
        "email": row["email"],
        "display_name": row["display_name"],
        "thread_id": "",
        "received_count": row["total_received"],
        "sent_count": row["total_sent"],
        "message_count": row["total_messages"],
        "first_message_at": row["first_interaction"],
        "last_message_at": row["last_interaction"],
        "subject": "",
        "discovered_at": now_iso(),
    } for row in filtered])
    write_csv(aggregated_path, AGGREGATED_COLUMNS, [{
        "email": row["email"],
        "display_name": row["display_name"],
        "total_sent": row["total_sent"],
        "total_received": row["total_received"],
        "total_messages": row["total_messages"],
        "thread_count": row["thread_count"],
        "first_interaction": row["first_interaction"],
        "last_interaction": row["last_interaction"],
        "sample_subjects": "[]",
    } for row in filtered])
    write_csv(targeted_path, TARGETED_COLUMNS, [{
        "display_name": row["display_name"],
        "primary_email": row["email"],
        "primary_email_type": row["primary_email_type"],
        "all_emails": json.dumps([row["email"]]),
        "email_count": 1,
        "total_sent": row["total_sent"],
        "total_received": row["total_received"],
        "total_messages": row["total_messages"],
        "thread_count": row["thread_count"],
        "first_interaction": row["first_interaction"],
        "last_interaction": row["last_interaction"],
        "is_duplicate": False,
        "potential_same_person_emails": "[]",
        "sample_subjects": "[]",
        "sample_calendar_titles": "[]",
    } for row in filtered])
    write_csv(people_path, PEOPLE_COLUMNS, people_rows_from_msgvault(filtered, [str(targeted_path), str(aggregated_path)]))

    manifest = {
        "task": "import_gmail_network_msgvault",
        "version": 1,
        "created_at": now_iso(),
        "status": "completed",
        "source": "msgvault",
        "privacy": {
            "message_bodies_read": False,
            "message_subjects_included": False,
            "raw_mime_read": False,
            "local_artifacts_only": True,
        },
        "account_email": account_email,
        "counts": {
            "contacts_seen": len(rows),
            "contacts_written": len(filtered),
            "automated_filtered": len([row for row in rows if row.get("automated_filtered") and not include_automated]),
            "accounts": len(account_rows),
        },
        "artifacts": {
            "accounts_csv": str(accounts_path),
            "gmail_threads_csv": str(threads_path),
            "gmail_contacts_aggregated_csv": str(aggregated_path),
            "targeted_emails_csv": str(targeted_path),
            "people_csv": str(people_path),
            "manifest_json": str(manifest_path),
        },
        "schema_reference": {
            "msgvault_tables": ["sources", "participants", "messages", "message_recipients"],
            "key_fields": ["participants.email_address", "participants.display_name", "message_recipients.display_name", "messages.sent_at", "sources.identifier"],
        },
    }
    write_json(manifest_path, manifest)
    return manifest


def make_artifacts(contact: OnePersonInput, out_dir: Path) -> dict[str, Any]:
    email_type = classify_email(contact.email)
    automated, reason = is_automated_email(contact.email)
    account = contact.account_short
    op = contact.operator_short
    threads_path = out_dir / f"gmail_threads_{account}_{op}.csv"
    aggregated_path = out_dir / f"gmail_contacts_aggregated_{account}_{op}.csv"
    targeted_path = out_dir / f"targeted_emails_{account}_{op}.csv"
    source_jsonl = out_dir / "source_contact.jsonl"
    domain_path = out_dir / "domain_context.json"
    manifest_path = out_dir / "manifest.json"
    accounts_path = append_account(contact, out_dir)

    write_csv(
        threads_path,
        THREAD_COLUMNS,
        [
            {
                "email": contact.email,
                "display_name": contact.display_name,
                "thread_id": "manual-one-person",
                "received_count": contact.total_received,
                "sent_count": contact.total_sent,
                "message_count": contact.total_messages,
                "first_message_at": contact.first_interaction,
                "last_message_at": contact.last_interaction,
                "subject": "",
                "discovered_at": now_iso(),
            }
        ],
    )
    write_csv(
        aggregated_path,
        AGGREGATED_COLUMNS,
        [
            {
                "email": contact.email,
                "display_name": contact.display_name,
                "total_sent": contact.total_sent,
                "total_received": contact.total_received,
                "total_messages": contact.total_messages,
                "thread_count": contact.thread_count,
                "first_interaction": contact.first_interaction,
                "last_interaction": contact.last_interaction,
                "sample_subjects": "[]",
            }
        ],
    )
    write_csv(
        targeted_path,
        TARGETED_COLUMNS,
        [
            {
                "display_name": contact.display_name,
                "primary_email": contact.email,
                "primary_email_type": email_type,
                "all_emails": json.dumps([contact.email]),
                "email_count": 1,
                "total_sent": contact.total_sent,
                "total_received": contact.total_received,
                "total_messages": contact.total_messages,
                "thread_count": contact.thread_count,
                "first_interaction": contact.first_interaction,
                "last_interaction": contact.last_interaction,
                "is_duplicate": False,
                "potential_same_person_emails": "[]",
                "sample_subjects": "[]",
                "sample_calendar_titles": "[]",
            }
        ],
    )
    source_jsonl.write_text(json.dumps(asdict(contact), sort_keys=True) + "\n", encoding="utf-8")
    write_json(domain_path, domain_guess(contact.email))

    manifest = {
        "task": "import_gmail_network_one_person",
        "version": 2,
        "created_at": now_iso(),
        "status": "seeded",
        "privacy": {
            "message_bodies_read": False,
            "message_subjects_included": False,
            "local_artifacts_only": True,
        },
        "contact": {
            "email": contact.email,
            "display_name": contact.display_name,
            "primary_email_type": email_type,
            "automated_filtered": automated,
            "automated_reason": reason,
        },
        "account": {
            "account_id": contact.account_id,
            "account_email": contact.account_email,
            "account_short": account,
        },
        "ids": {
            "operator_id": contact.operator_id,
            "operator_short": op,
        },
        "artifacts": {
            "accounts_csv": str(accounts_path),
            "source_contact_jsonl": str(source_jsonl),
            "domain_context_json": str(domain_path),
            "gmail_threads_csv": str(threads_path),
            "gmail_contacts_aggregated_csv": str(aggregated_path),
            "targeted_emails_csv": str(targeted_path),
            "manifest_json": str(manifest_path),
        },
    }
    write_json(manifest_path, manifest)
    return manifest


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "gmail_network_import")
    ledger.setdefault("version", 2)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("approvals", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def mark_step(ledger: dict[str, Any], step_id: str, status: str, **extra: Any) -> None:
    steps = ledger.setdefault("steps", {})
    rec = steps.setdefault(step_id, {"id": step_id})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked_approval", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def next_pending_step(ledger: dict[str, Any]) -> str | None:
    steps = ledger.setdefault("steps", {})
    for step_id in PIPELINE_STEPS:
        if steps.get(step_id, {}).get("status") != "completed":
            return step_id
    return None


def step_seed_one(ledger: dict[str, Any]) -> dict[str, Any]:
    contact = OnePersonInput(**ledger["input"])
    manifest = make_artifacts(contact, Path(ledger["run_dir"]))
    ledger["account"] = manifest["account"]
    ledger["ids"] = manifest["ids"]
    ledger["artifacts"].update(manifest["artifacts"])
    return {"manifest": manifest}


def step_prepare_local_workspace(ledger: dict[str, Any]) -> dict[str, Any]:
    workspace = {
        "workspace_root": ledger["run_dir"],
        "contract": "powerpacks.gmail_network_import.v1.local_one_person",
        "multiple_accounts_supported_by": "one run per account_email/account_id, then merge later by email/linkedin/person_id",
        "oauth_model": "future: user authorizes Gmail through Powerset; local agent receives scoped metadata/export artifacts, not raw refresh tokens",
    }
    path = Path(ledger["run_dir"]) / "workspace.json"
    write_json(path, workspace)
    ledger["artifacts"]["workspace_json"] = str(path)
    return workspace


def step_write_next_steps(ledger: dict[str, Any]) -> dict[str, Any]:
    next_steps = {
        "implemented_now": [
            "one-person local seed",
            "pipeline-compatible aggregated and targeted CSV shapes",
            "local domain heuristic; no OpenAI domain parse in V1",
            "account metadata for multi-account runs",
        ],
        "not_run_by_v1": [
            "Gmail OAuth sync",
            "Parallel.ai enrichment",
            "EnrichLayer fallback",
            "RapidAPI candidate collection",
            "OpenAI candidate review",
            "Harmonic enrichment",
            "uploads or production source seeding",
        ],
        "future_orchestrator_shape": "run/continue/approve, with sub-agents allowed for long approved steps; all artifacts remain under .powerpacks/.",
    }
    path = Path(ledger["run_dir"]) / "next-steps.json"
    write_json(path, next_steps)
    ledger["artifacts"]["next_steps_json"] = str(path)
    return next_steps


def execute_step(ledger: dict[str, Any], step_id: str) -> dict[str, Any]:
    if step_id == "seed_one":
        return step_seed_one(ledger)
    if step_id == "prepare_local_workspace":
        return step_prepare_local_workspace(ledger)
    if step_id == "write_next_steps":
        return step_write_next_steps(ledger)
    raise PipelineFailed(f"unknown step: {step_id}")


def run_until_blocked_or_done(ledger_path: Path) -> int:
    ledger = load_ledger(ledger_path)
    while True:
        step_id = next_pending_step(ledger)
        if step_id is None:
            ledger["status"] = "completed"
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
            emit({
                "status": "completed",
                "ledger": str(ledger_path),
                "run_dir": ledger.get("run_dir"),
                "artifacts": ledger.get("artifacts", {}),
                "summary": "Local one-person Gmail seed completed. No external APIs, DVC, uploads, or prod writes were run.",
            })
            return 0
        try:
            mark_step(ledger, step_id, "running")
            save_ledger(ledger_path, ledger)
            summary = execute_step(ledger, step_id)
            mark_step(ledger, step_id, "completed", summary=summary)
            save_ledger(ledger_path, ledger)
        except PipelineFailed as exc:
            mark_step(ledger, step_id, "failed", error=str(exc))
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": step_id, "error": str(exc), "ledger": str(ledger_path)})
            return 1


def command_run(args: argparse.Namespace) -> int:
    contact = build_one_person(args)
    out_dir = run_dir(Path(args.output_dir), contact, args.run_id)
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force:
        existing = load_ledger(ledger_path)
        if existing.get("status") not in {"completed", "failed"}:
            emit({
                "status": "active_run_exists",
                "ledger": str(ledger_path),
                "message": "Use continue, approve, or pass --force to start a fresh one-person import.",
            })
            return 0
    ledger = {
        "primitive": "gmail_network_import",
        "version": 2,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": out_dir.name,
        "run_dir": str(out_dir),
        "ledger": str(ledger_path),
        "input": asdict(contact),
        "steps": {},
        "approvals": {},
        "artifacts": {},
        "privacy": {
            "message_bodies_read": False,
            "message_subjects_included": False,
            "local_artifacts_only": True,
        },
    }
    save_ledger(ledger_path, ledger)
    try:
        return run_until_blocked_or_done(ledger_path)
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_continue(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    if not ledger_path.exists():
        emit({"status": "missing_ledger", "ledger": str(ledger_path), "message": "Run the import first."})
        return 2
    try:
        return run_until_blocked_or_done(ledger_path)
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_approve(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    blocked = ledger.get("blocked") or {}
    app_id = args.approval_id or blocked.get("approval_id")
    if not app_id:
        emit({"status": "no_pending_approval", "ledger": str(ledger_path)})
        return 1
    ledger.setdefault("approvals", {})[app_id] = {"approved_at": now_iso(), "source": "operator"}
    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "approved", "approval_id": app_id, "ledger": str(ledger_path)})
    return 0


def command_status(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    emit({
        "status": ledger.get("status", "unknown"),
        "ledger": str(ledger_path),
        "blocked": ledger.get("blocked"),
        "steps": ledger.get("steps", {}),
        "artifacts": ledger.get("artifacts", {}),
    })
    return 0


def command_accounts(args: argparse.Namespace) -> int:
    try:
        stats = api_get_json(f"{args.api_url.rstrip('/')}/v2/integrations/gmail-stats", powerset_token())
    except PipelineFailed as exc:
        emit({"status": "failed", "error": str(exc)})
        return 1
    emit({
        "status": "ok",
        "api_url": args.api_url,
        "connected_accounts": stats.get("connected_accounts", []),
        "counts": {
            "total_contacts": stats.get("total_contacts", 0),
            "confirmed_linkedin": stats.get("confirmed_linkedin", 0),
            "needs_review": stats.get("needs_review", 0),
            "unmatched": stats.get("unmatched", 0),
            "google_contacts_count": stats.get("google_contacts_count", 0),
            "calendar_events_count": stats.get("calendar_events_count", 0),
        },
        "last_sync_at": stats.get("last_sync_at"),
        "tokens_stored": "server_side_encrypted_supabase",
    })
    return 0


def command_connect(args: argparse.Namespace) -> int:
    opened = False
    if not args.no_open:
        opened = webbrowser.open(args.app_url)
    emit({
        "status": "ok",
        "app_url": args.app_url,
        "opened_browser": opened,
        "auth_model": "Browser app performs Auth0 login if needed, then Google OAuth. Powerpacks does not put the local bearer token in the URL.",
        "token_storage": "Google tokens are stored server-side in encrypted Supabase gmail_oauth_tokens and mapped via user_gmail_mappings.",
        "after_connect": "Run `... gmail_network_import.py accounts` to verify linked accounts via the local Powerset token.",
    })
    return 0


def command_msgvault(args: argparse.Namespace) -> int:
    con = connect_msgvault(Path(args.db))
    try:
        require_msgvault_schema(con)
        rows = aggregate_msgvault_contacts(con, args.account_email)
    finally:
        con.close()
    run_id = args.run_id or f"msgvault-{short_hash((args.account_email or 'all') + ':' + now_iso(), 12)}"
    out_dir = Path(args.output_dir) / "gmail" / run_id
    manifest = write_msgvault_artifacts(
        rows,
        out_dir,
        account_email=args.account_email,
        operator_id=args.operator_id,
        include_automated=bool(args.include_automated),
        limit=args.limit,
    )
    emit({
        "status": "completed",
        "run_dir": str(out_dir),
        "artifacts": manifest["artifacts"],
        "counts": manifest["counts"],
        "privacy": manifest["privacy"],
        "summary": "Imported Gmail contact metadata from msgvault. No message bodies, subjects, raw MIME, external APIs, uploads, or prod writes were used.",
    })
    return 0


def add_contact_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--email", required=True, help="Contact email to seed")
    parser.add_argument("--name", default="", help="Contact display name")
    parser.add_argument("--account-email", default="", help="The Gmail account this contact came from")
    parser.add_argument("--account-id", default="", help="Stable local/Powerset account id for this Gmail account")
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--total-sent", type=int, default=1)
    parser.add_argument("--total-received", type=int, default=1)
    parser.add_argument("--total-messages", type=int)
    parser.add_argument("--thread-count", type=int, default=1)
    parser.add_argument("--first-interaction", default="")
    parser.add_argument("--last-interaction", default="")
    parser.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    parser.add_argument("--run-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-person Gmail network-import orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Start a fresh one-person local run")
    add_contact_args(run)
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=command_run)

    cont = sub.add_parser("continue", help="Resume from the ledger")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=command_continue)

    approve = sub.add_parser("approve", help="Approve the currently blocked future gate")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.add_argument("--approval-id")
    approve.set_defaults(func=command_approve)

    status = sub.add_parser("status", help="Show ledger status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=command_status)

    accounts = sub.add_parser("accounts", help="List server-linked Gmail accounts via the local Powerset token")
    accounts.add_argument("--api-url", default=DEFAULT_API_URL)
    accounts.set_defaults(func=command_accounts)

    connect = sub.add_parser("connect", help="Open the Powerset Gmail connection page")
    connect.add_argument("--app-url", default=DEFAULT_APP_GMAIL_URL)
    connect.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    connect.set_defaults(func=command_connect)

    msgvault = sub.add_parser("msgvault", aliases=["import-msgvault"], help="Import Gmail contact metadata from a local msgvault SQLite archive")
    msgvault.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")
    msgvault.add_argument("--account-email", default="", help="Optional Gmail source account filter")
    msgvault.add_argument("--operator-id", default="local")
    msgvault.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    msgvault.add_argument("--run-id")
    msgvault.add_argument("--limit", type=int)
    msgvault.add_argument("--include-automated", action="store_true", help="Include noreply/automated service addresses")
    msgvault.set_defaults(func=command_msgvault)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
