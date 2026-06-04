#!/usr/bin/env python3
"""Local Gmail network import from msgvault metadata.

The supported sync path is msgvault's local SQLite archive. This primitive reads
only msgvault metadata tables and writes Powerpacks-local artifacts. It never
reads Gmail message bodies, subjects, snippets, raw MIME, or attachments, and it
does not use Powerset-hosted Gmail OAuth/sync endpoints.

The legacy one-person local seed remains for deterministic tests/manual seeds;
real Gmail imports should use the `msgvault` subcommand.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from packs.ingestion.schemas.people_schema import generate_person_id as generate_linkedin_person_id
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import generate_person_id as generate_linkedin_person_id

DEFAULT_LEDGER = Path(".powerpacks/network-import/discover/gmail-one/ledger.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_MSGVAULT_DB = Path(os.environ.get("MSGVAULT_HOME", str(Path.home() / ".msgvault"))) / "msgvault.db"
DEFAULT_EXCLUDED_MSGVAULT_LABELS = ("CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES")

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
LINKEDIN_RESOLUTION_QUEUE_COLUMNS = [
    "handle",
    "id",
    "account_emails",
    "source_ids",
    "display_name",
    "full_name",
    "primary_email",
    "company_guess",
    "primary_email_type",
    "total_messages",
    "thread_count",
    "last_interaction",
    "source",
    "source_channels",
]
LINKEDIN_RESOLUTION_COLUMNS = ["handle", "status", "linkedin_url", "confidence", "matched_name", "matched_headline", "evidence", "reasoning"]
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


def extract_public_identifier(linkedin_url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url or "", re.IGNORECASE)
    return match.group(1).strip().rstrip("/").lower() if match else ""


def normalize_linkedin_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith("linkedin.com/"):
        url = "https://www." + url
    elif url.startswith("www.linkedin.com/"):
        url = "https://" + url
    url = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    public_id = extract_public_identifier(url)
    return f"https://www.linkedin.com/in/{public_id}" if public_id else url


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def one_person_dir(base_dir: Path, contact: OnePersonInput) -> Path:
    account = source_slug(contact.account_email or "local")
    contact_slug = source_slug(contact.email)
    return base_dir / "discover" / "gmail-one" / account / contact_slug


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def source_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or "source"


def gmail_discover_dir(base_dir: Path, account_email: str = "") -> Path:
    return base_dir / "discover" / "gmail" / source_slug(account_email or "all")


def csv_key(row: dict[str, Any], fields: list[str]) -> tuple[str, ...] | None:
    key = tuple(str(row.get(field) or "").strip().lower() for field in fields)
    return key if any(key) else None


def normalize_csv_row(fieldnames: list[str], row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field, "") for field in fieldnames}


def merge_csv_row(fieldnames: list[str], existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = normalize_csv_row(fieldnames, existing)
    for field in fieldnames:
        value = incoming.get(field, "")
        if value in ("", None):
            continue
        if field == "added_at" and merged.get(field):
            continue
        merged[field] = value
    return merged


def upsert_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]], key_fields: list[str]) -> dict[str, int]:
    existing_rows = read_csv(path) if path.exists() else []
    keyed: dict[tuple[str, ...], dict[str, Any]] = {}
    keyless_existing: list[dict[str, Any]] = []
    for row in existing_rows:
        normalized = normalize_csv_row(fieldnames, row)
        key = csv_key(normalized, key_fields)
        if key is None:
            keyless_existing.append(normalized)
            continue
        keyed[key] = merge_csv_row(fieldnames, keyed[key], normalized) if key in keyed else normalized

    incoming_keys: set[tuple[str, ...]] = set()
    for row in rows:
        normalized = normalize_csv_row(fieldnames, row)
        key = csv_key(normalized, key_fields)
        if key is None:
            keyless_existing.append(normalized)
            continue
        incoming_keys.add(key)
        keyed[key] = merge_csv_row(fieldnames, keyed[key], normalized) if key in keyed else normalized

    output_rows = [keyed[key] for key in sorted(keyed)]
    output_rows.extend(keyless_existing)
    write_csv(path, fieldnames, output_rows)
    return {
        "incoming": len(rows),
        "existing": len(existing_rows),
        "written": len(output_rows),
        "preserved_existing": len([key for key in keyed if key not in incoming_keys]),
        "upserted": len(incoming_keys),
    }


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


def normalize_label_names(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        label = str(value or "").strip()
        if label and label.upper() not in out:
            out.append(label.upper())
    return out


def msgvault_has_label_tables(con: sqlite3.Connection) -> bool:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name IN ('labels', 'message_labels')"
    ).fetchall()
    return {str(row[0]) for row in rows} == {"labels", "message_labels"}


def default_excluded_labels(include_category_mail: bool, extra_labels: Iterable[str] | None = None) -> list[str]:
    labels: list[str] = []
    if not include_category_mail:
        labels.extend(DEFAULT_EXCLUDED_MSGVAULT_LABELS)
    labels.extend(extra_labels or [])
    return normalize_label_names(labels)


def iter_msgvault_metadata(con: sqlite3.Connection, account_email: str = "", exclude_labels: Iterable[str] | None = None) -> Iterable[sqlite3.Row]:
    labels = normalize_label_names(exclude_labels)
    label_filter = ""
    params: list[Any] = [account_email, account_email]
    if labels and msgvault_has_label_tables(con):
        placeholders = ",".join("?" for _ in labels)
        label_filter = f"""
          AND NOT EXISTS (
              SELECT 1
              FROM message_labels ml
              JOIN labels l ON l.id = ml.label_id
              WHERE ml.message_id = m.id
                AND UPPER(l.name) IN ({placeholders})
          )
        """
        params.extend(labels)
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
          {label_filter}
        ORDER BY LOWER(p.email_address), m.id
    """.format(label_filter=label_filter)
    yield from con.execute(query, params)


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


def aggregate_msgvault_contacts(con: sqlite3.Connection, account_email: str = "", exclude_labels: Iterable[str] | None = None) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    account_filter = account_email.strip().lower()
    for row in iter_msgvault_metadata(con, account_filter, exclude_labels):
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


def linkedin_resolution_queue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        if not email:
            continue
        guess = domain_guess(email)
        queue.append({
            "handle": email,
            "id": f"gmail:{short_hash(email, 16)}",
            "account_emails": json.dumps(row.get("account_emails") or [], ensure_ascii=False),
            "source_ids": json.dumps(row.get("source_ids") or [], ensure_ascii=False),
            "display_name": row.get("display_name") or "",
            "full_name": row.get("display_name") or "",
            "primary_email": email,
            "company_guess": guess.get("company_guess", ""),
            "primary_email_type": row.get("primary_email_type") or classify_email(email),
            "total_messages": row.get("total_messages", ""),
            "thread_count": row.get("thread_count", ""),
            "last_interaction": row.get("last_interaction", ""),
            "source": "gmail_msgvault",
            "source_channels": "gmail_msgvault",
        })
    return queue


def load_resolution_map(path: Path, min_confidence: float) -> dict[str, dict[str, str]]:
    resolutions: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        status = (row.get("status") or "").strip().lower()
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
        try:
            confidence = float(row.get("confidence") or 0)
        except ValueError:
            confidence = 0.0
        handle = (row.get("handle") or "").strip().lower()
        if status == "found" and linkedin_url and handle and confidence >= min_confidence:
            row = dict(row)
            row["linkedin_url"] = linkedin_url
            row["confidence"] = str(confidence)
            resolutions[handle] = row
    return resolutions


def apply_linkedin_resolutions_to_people(people_csv: Path, resolutions_csv: Path, output_dir: Path, *, min_confidence: float = 0.75) -> dict[str, Any]:
    people_rows = read_csv(people_csv)
    resolutions = load_resolution_map(resolutions_csv, min_confidence)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "people.csv"
    applied_path = output_dir / "linkedin_resolutions_applied.csv"
    applied: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    for row in people_rows:
        normalized = {col: row.get(col, "") for col in PEOPLE_COLUMNS}
        email = (normalized.get("primary_email") or "").strip().lower()
        resolution = resolutions.get(email) or resolutions.get((normalized.get("id") or "").strip().lower())
        if resolution:
            linkedin_url = normalize_linkedin_url(resolution.get("linkedin_url") or "")
            public_id = extract_public_identifier(linkedin_url)
            if public_id:
                normalized["id"] = generate_linkedin_person_id(public_id)
                normalized["public_identifier"] = public_id
                normalized["linkedin_url"] = linkedin_url
                if resolution.get("matched_name") and not normalized.get("full_name"):
                    normalized["full_name"] = resolution["matched_name"]
                if resolution.get("matched_headline"):
                    normalized["headline"] = resolution["matched_headline"]
                normalized["enrichment_provider"] = "parallel_linkedin_resolution"
                normalized["enriched_at"] = now_iso()
                artifacts = [str(people_csv), str(resolutions_csv)]
                try:
                    existing = json.loads(normalized.get("source_artifacts") or "[]")
                    if isinstance(existing, list):
                        artifacts = [str(x) for x in existing] + [str(resolutions_csv)]
                except json.JSONDecodeError:
                    pass
                normalized["source_artifacts"] = json.dumps(sorted(set(artifacts)), ensure_ascii=False)
                applied.append({
                    "primary_email": email,
                    "linkedin_url": linkedin_url,
                    "public_identifier": public_id,
                    "confidence": resolution.get("confidence", ""),
                    "matched_name": resolution.get("matched_name", ""),
                })
        output_rows.append(normalized)
    write_csv(out_path, PEOPLE_COLUMNS, output_rows)
    write_csv(applied_path, ["primary_email", "linkedin_url", "public_identifier", "confidence", "matched_name"], applied)
    return {
        "status": "completed",
        "input_people_csv": str(people_csv),
        "resolutions_csv": str(resolutions_csv),
        "people_csv": str(out_path),
        "applied_csv": str(applied_path),
        "rows": len(output_rows),
        "resolved": len(applied),
        "min_confidence": min_confidence,
    }


def has_round_trip_interaction(row: dict[str, Any]) -> bool:
    return int(row.get("total_sent") or 0) > 0 and int(row.get("total_received") or 0) > 0


def write_msgvault_artifacts(rows: list[dict[str, Any]], out_dir: Path, account_email: str = "", operator_id: str = "local", *, include_automated: bool = False, limit: int | None = None, excluded_labels: Iterable[str] | None = None) -> dict[str, Any]:
    automated_filtered = [row for row in rows if row.get("automated_filtered") and not include_automated]
    non_automated = [row for row in rows if include_automated or not row.get("automated_filtered")]
    one_way_filtered = [row for row in non_automated if not has_round_trip_interaction(row)]
    filtered = [row for row in non_automated if has_round_trip_interaction(row)]
    if limit is not None:
        filtered = filtered[: max(0, int(limit))]
    out_dir.mkdir(parents=True, exist_ok=True)
    threads_path = out_dir / "gmail_threads.csv"
    aggregated_path = out_dir / "gmail_contacts_aggregated.csv"
    targeted_path = out_dir / "targeted_emails.csv"
    resolution_queue_path = out_dir / "linkedin_resolution_queue.csv"
    people_path = out_dir / "people.csv"
    accounts_path = out_dir / "accounts.csv"
    manifest_path = out_dir / "manifest.json"
    discovered_at = now_iso()

    account_rows = []
    seen_accounts: set[str] = set()
    for row in filtered:
        for account in row.get("account_emails") or []:
            if account in seen_accounts:
                continue
            seen_accounts.add(account)
            account_rows.append({"account_id": f"msgvault:{short_hash(account, 12)}", "account_email": account, "provider": "gmail", "source": "msgvault", "added_at": discovered_at})
    if account_email and account_email not in seen_accounts:
        account_rows.append({"account_id": f"msgvault:{short_hash(account_email, 12)}", "account_email": account_email, "provider": "gmail", "source": "msgvault", "added_at": discovered_at})
    upserts: dict[str, dict[str, int]] = {}
    upserts["accounts_csv"] = upsert_csv(accounts_path, ACCOUNT_COLUMNS, account_rows, ["account_email"])

    threads_rows = [{
        "email": row["email"],
        "display_name": row["display_name"],
        "thread_id": "",
        "received_count": row["total_received"],
        "sent_count": row["total_sent"],
        "message_count": row["total_messages"],
        "first_message_at": row["first_interaction"],
        "last_message_at": row["last_interaction"],
        "subject": "",
        "discovered_at": discovered_at,
    } for row in filtered]
    aggregated_rows = [{
        "email": row["email"],
        "display_name": row["display_name"],
        "total_sent": row["total_sent"],
        "total_received": row["total_received"],
        "total_messages": row["total_messages"],
        "thread_count": row["thread_count"],
        "first_interaction": row["first_interaction"],
        "last_interaction": row["last_interaction"],
        "sample_subjects": "[]",
    } for row in filtered]
    targeted_rows = [{
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
    } for row in filtered]
    resolution_queue_rows = linkedin_resolution_queue_rows(filtered)
    people_rows = people_rows_from_msgvault(filtered, [str(targeted_path), str(aggregated_path), str(resolution_queue_path)])

    upserts["gmail_threads_csv"] = upsert_csv(threads_path, THREAD_COLUMNS, threads_rows, ["email"])
    upserts["gmail_contacts_aggregated_csv"] = upsert_csv(aggregated_path, AGGREGATED_COLUMNS, aggregated_rows, ["email"])
    upserts["targeted_emails_csv"] = upsert_csv(targeted_path, TARGETED_COLUMNS, targeted_rows, ["primary_email"])
    upserts["linkedin_resolution_queue_csv"] = upsert_csv(resolution_queue_path, LINKEDIN_RESOLUTION_QUEUE_COLUMNS, resolution_queue_rows, ["handle"])
    upserts["people_csv"] = upsert_csv(people_path, PEOPLE_COLUMNS, people_rows, ["primary_email"])

    existing_manifest = read_json(manifest_path, {}) or {}

    manifest = {
        "task": "import_gmail_network_msgvault",
        "version": 2,
        "created_at": existing_manifest.get("created_at") or discovered_at,
        "updated_at": discovered_at,
        "status": "completed",
        "source": "msgvault",
        "artifact_dir": str(out_dir),
        "account_slug": out_dir.name,
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
            "contacts_final": upserts["people_csv"]["written"],
            "contacts_preserved_existing": upserts["people_csv"]["preserved_existing"],
            "automated_filtered": len(automated_filtered),
            "one_way_filtered": len(one_way_filtered),
            "round_trip_required": True,
            "accounts": upserts["accounts_csv"]["written"],
            "excluded_labels": normalize_label_names(excluded_labels),
        },
        "upserts": upserts,
        "artifacts": {
            "accounts_csv": str(accounts_path),
            "gmail_threads_csv": str(threads_path),
            "gmail_contacts_aggregated_csv": str(aggregated_path),
            "targeted_emails_csv": str(targeted_path),
            "linkedin_resolution_queue_csv": str(resolution_queue_path),
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


def artifact_dir_from_ledger(ledger: dict[str, Any]) -> Path:
    return Path(str(ledger.get("artifact_dir") or ledger.get("run_dir") or DEFAULT_BASE_DIR / "discover" / "gmail-one"))


def step_seed_one(ledger: dict[str, Any]) -> dict[str, Any]:
    contact = OnePersonInput(**ledger["input"])
    manifest = make_artifacts(contact, artifact_dir_from_ledger(ledger))
    ledger["account"] = manifest["account"]
    ledger["ids"] = manifest["ids"]
    ledger["artifacts"].update(manifest["artifacts"])
    return {"manifest": manifest}


def step_prepare_local_workspace(ledger: dict[str, Any]) -> dict[str, Any]:
    artifact_dir = artifact_dir_from_ledger(ledger)
    workspace = {
        "workspace_root": str(artifact_dir),
        "contract": "powerpacks.gmail_network_import.v1.local_one_person",
        "multiple_accounts_supported_by": "one run per account_email/account_id, then merge later by email/linkedin/person_id",
        "sync_model": "msgvault local SQLite metadata import is the supported Gmail sync path; this legacy seed does not use OAuth",
    }
    path = artifact_dir / "workspace.json"
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
            "Gmail sync",
            "Parallel.ai enrichment",
            "EnrichLayer fallback",
            "RapidAPI candidate collection",
            "OpenAI candidate review",
            "Harmonic enrichment",
            "uploads or production source seeding",
        ],
        "future_orchestrator_shape": "run/continue/approve, with sub-agents allowed for long approved steps; all artifacts remain under .powerpacks/.",
    }
    path = artifact_dir_from_ledger(ledger) / "next-steps.json"
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
                "artifact_dir": ledger.get("artifact_dir") or ledger.get("run_dir"),
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
    out_dir = one_person_dir(Path(args.output_dir), contact)
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
        "artifact_dir": str(out_dir),
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


def list_msgvault_accounts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute("""
        SELECT
            s.id AS source_id,
            s.identifier AS account_email,
            s.display_name AS display_name,
            COUNT(DISTINCT m.id) AS message_count
        FROM sources s
        LEFT JOIN messages m ON m.source_id = s.id
        WHERE (s.source_type IS NULL OR LOWER(s.source_type) = 'gmail')
          AND s.identifier IS NOT NULL
          AND TRIM(s.identifier) != ''
        GROUP BY s.id, s.identifier, s.display_name
        ORDER BY LOWER(s.identifier)
    """).fetchall()
    accounts: list[dict[str, Any]] = []
    for row in rows:
        email = str(row["account_email"] or "").strip().lower()
        if not email:
            continue
        accounts.append({
            "source_id": str(row["source_id"]),
            "account_email": email,
            "display_name": str(row["display_name"] or ""),
            "message_count": int(row["message_count"] or 0),
        })
    return accounts


def command_msgvault_accounts(args: argparse.Namespace) -> int:
    con = connect_msgvault(Path(args.db))
    try:
        require_msgvault_schema(con)
        accounts = list_msgvault_accounts(con)
    finally:
        con.close()
    emit({
        "status": "ok",
        "source": "msgvault",
        "db": str(Path(args.db).expanduser()),
        "accounts": accounts,
        "count": len(accounts),
        "privacy": {
            "message_bodies_read": False,
            "message_subjects_included": False,
            "raw_mime_read": False,
            "local_artifacts_only": True,
        },
    })
    return 0


def command_msgvault(args: argparse.Namespace) -> int:
    excluded_labels = default_excluded_labels(bool(args.include_category_mail), args.exclude_label)
    con = connect_msgvault(Path(args.db))
    try:
        require_msgvault_schema(con)
        rows = aggregate_msgvault_contacts(con, args.account_email, excluded_labels)
    finally:
        con.close()
    out_dir = gmail_discover_dir(Path(args.output_dir), args.account_email)
    manifest = write_msgvault_artifacts(
        rows,
        out_dir,
        account_email=args.account_email,
        operator_id=args.operator_id,
        include_automated=bool(args.include_automated),
        limit=args.limit,
        excluded_labels=excluded_labels,
    )
    emit({
        "status": "completed",
        "artifact_dir": str(out_dir),
        "artifacts": manifest["artifacts"],
        "counts": manifest["counts"],
        "privacy": manifest["privacy"],
        "summary": "Imported Gmail contact metadata from msgvault and wrote a LinkedIn resolution queue. No message bodies, subjects, raw MIME, external APIs, uploads, or prod writes were used.",
    })
    return 0


def command_apply_resolutions(args: argparse.Namespace) -> int:
    payload = apply_linkedin_resolutions_to_people(
        Path(args.people_csv),
        Path(args.resolutions_csv),
        Path(args.output_dir),
        min_confidence=args.min_confidence,
    )
    emit(payload)
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

    approve = sub.add_parser("approve", help="Approve the currently blocked future confirmation")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.add_argument("--approval-id")
    approve.set_defaults(func=command_approve)

    status = sub.add_parser("status", help="Show ledger status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=command_status)

    sources = sub.add_parser("msgvault-accounts", aliases=["msgvault-sources"], help="List Gmail source accounts in a local msgvault SQLite archive")
    sources.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")
    sources.set_defaults(func=command_msgvault_accounts)

    msgvault = sub.add_parser("msgvault", aliases=["import-msgvault"], help="Import Gmail contact metadata from a local msgvault SQLite archive")
    msgvault.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")
    msgvault.add_argument("--account-email", default="", help="Optional Gmail source account filter")
    msgvault.add_argument("--operator-id", default="local")
    msgvault.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    msgvault.add_argument("--limit", type=int)
    msgvault.add_argument("--include-automated", action="store_true", help="Include noreply/automated service addresses")
    msgvault.add_argument("--exclude-label", action="append", default=[], help="Exclude messages with this msgvault/Gmail label name; may be repeated")
    msgvault.add_argument("--include-category-mail", action="store_true", help="Do not exclude default Gmail category labels: Social, Promotions, Forums, Updates")
    msgvault.set_defaults(func=command_msgvault)

    apply = sub.add_parser("apply-resolutions", help="Apply LinkedIn resolution results to a Gmail/msgvault people.csv")
    apply.add_argument("--people-csv", required=True)
    apply.add_argument("--resolutions-csv", required=True)
    apply.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    apply.add_argument("--min-confidence", type=float, default=0.75)
    apply.set_defaults(func=command_apply_resolutions)

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
