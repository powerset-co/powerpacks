#!/usr/bin/env python3
"""Resolve queued email→LinkedIn identities (harness prompts or Parallel.ai).

Reads a `linkedin_resolution_queue.csv` and writes `linkedin_resolutions.csv`
with columns `email, full_name, company, linkedin_url, x_handle, status,
confidence, reasoning, candidates`, using the shared prompt in
`packs/ingestion/prompts/linkedin_resolution.md`.

Providers (`--provider`):
- `harness`: no spend; writes `harness_prompts.jsonl` for Codex/Claude/manual
  resolution.
- `parallel` (default): spend-bearing; `run` without `--approve-spend` returns
  `blocked_approval`, and an approved rerun sends (full_name, company, email)
  to Parallel.ai.

Usage:
    resolve_queue.py run --provider harness --input .../linkedin_resolution_queue.csv [--output-dir DIR]
    resolve_queue.py run --provider parallel [--approve-spend] --input ...
    resolve_queue.py status

Dedup: reads any existing output CSV to skip already-resolved emails, so
repeated runs shrink the new-lookup set to zero. The CLI has no separate
`approve`/`continue` subcommand; an interrupted provider task may be
resubmitted on rerun.

Changelog:
  2026-07-23 (audit): resolve_queue.README.md sidecar folded into this
    docstring; dropped its stale output-column list
    (handle/matched_name/matched_headline/evidence never existed here) and the
    nonexistent `instructions.md` artifact claim.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_LEDGER = Path(".powerpacks/network-import/linkedin-resolution/import-run.json")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/network-import/linkedin-resolution")
DEFAULT_BASE_URL = os.environ.get("POWERPACKS_PARALLEL_BASE_URL", "https://api.parallel.ai")
DEFAULT_BETA = os.environ.get("POWERPACKS_PARALLEL_BETA", "search-extract-2025-10-10")
DEFAULT_PROCESSOR = os.environ.get("POWERPACKS_PARALLEL_PROCESSOR", "core2x")
ALLOWED_PROCESSORS = {"core", "core2x", "pro"}

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "fastmail.com", "zoho.com",
    "ymail.com", "att.net", "verizon.net", "sbcglobal.net", "cox.net",
    "earthlink.net", "comcast.net", "mail.com",
}

GENERIC_PREFIXES = {
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply",
    "info", "contact", "support", "help", "hello",
    "sales", "marketing", "hr", "careers", "jobs",
    "admin", "administrator", "webmaster", "postmaster",
    "office", "team", "staff", "general",
    "billing", "invoices", "payments", "accounts", "accounting",
    "newsletter", "news", "updates", "notifications",
    "feedback", "enquiries", "inquiries",
    "service", "customerservice", "care", "dispatch",
    "concierge", "reservation", "reservations", "booking", "bookings",
    "rsvp", "registration", "club", "member", "members", "membership", "memberservices",
    "optical", "photos", "equity", "futures",
    "launch", "eat", "pbx", "alumni", "masters", "csi",
    "studentinfo", "fintechsupport", "casasupport",
}

GENERIC_KEYWORDS = {
    "support", "service", "noreply", "reply", "taskforce",
    "insurance", "verification", "recognition",
}


BUSINESS_NAME_KEYWORDS = {
    "llc", "inc", "corp", "ltd", "team", "services", "service",
    "spa", "optometry", "electronics", "insurance", "association",
    "department", "office", "institute", "run", "discount", "massages",
    "management", "concierge", "dispatch", "accounting", "task force",
    "hawaii", "waikiki", "aruba", "support", "delivery",
    "wines", "coffee", "mason", "security", "motors",
}


def is_likely_person_name(name: str) -> bool:
    """Return True if the name looks like a real person (first + last)."""
    if not name:
        return False
    clean = re.sub(r'\s*\([^)]*\)', '', name).strip()  # strip parentheticals like (LinkedIn Supplier)
    clean = re.sub(r'^["\']|["\']$', '', clean).strip()
    words = clean.split()
    if len(words) < 2:
        return False
    if any(kw in clean.lower() for kw in BUSINESS_NAME_KEYWORDS):
        return False
    if '&' in clean:
        return False
    # All-caps or all-lower single tokens that aren't name-like
    if clean == clean.upper() and len(words) <= 2:
        return False
    return True


def is_generic_or_non_person(email: str) -> bool:
    """Return True if the email looks like a role/service address, not a person."""
    if not email or "@" not in email:
        return True
    local = email.split("@")[0].lower().strip()
    # Strip plus-addressing
    local = local.split("+")[0]
    # Exact prefix match
    if local in GENERIC_PREFIXES:
        return True
    # First segment match (e.g. customer.service@, no-reply@, info-mhi@)
    base = re.split(r'[.\-_]', local)[0]
    if base in GENERIC_PREFIXES:
        return True
    # Contains generic keyword anywhere
    for kw in GENERIC_KEYWORDS:
        if kw in local:
            return True
    # Phone-number-like local parts
    if re.match(r'^\d{7,}$', local):
        return True
    # Single character local parts
    if len(local) <= 1:
        return True
    # Local part is just digits (e.g. 2relaxinparadise is fine but pure digits aren't)
    if re.match(r'^\d+$', local):
        return True
    return False

PARALLEL_ENRICH_INSTRUCTIONS = """You are matching a real-world person identity using:
- full name
- current company/employer
- work email

Primary goal:
- Find the correct LinkedIn profile for the person.

Secondary goal:
- Find the correct X/Twitter handle ONLY if there is strong evidence it is the same person.

LinkedIn matching rules:
- Prefer exact identity matches supported by company, work history, location, or direct profile evidence.
- If multiple people share the same name, choose the one whose company/work history best matches the input.
- If no reliable LinkedIn match exists, return linkedin_url = "" and status = "not_found".

X/Twitter matching rules:
- Be conservative. Same name alone is NOT enough.
- Only return x_handle if the X profile clearly matches the LinkedIn person by at least one strong identity signal:
  - same employer/company/org referenced in the X bio or linked website
  - same personal website/domain
  - highly specific role/location alignment with no contradictions
- If the X bio/content/location contradicts the LinkedIn identity, return x_handle = "".
- If evidence is weak, ambiguous, or mostly based on same-name matching, return x_handle = "".
- It is better to miss a true handle than to attach the wrong handle.

Candidate rules:
- In `candidates`, list up to 5 plausible LinkedIn profiles, best match first, each with
  name, headline, location, a match_confidence in [0,1], and a short evidence note.
- Set `linkedin_url` to candidates[0].linkedin_url (your single best pick). If you are
  confident in exactly one, candidates may contain just that one.
- If no reliable match exists, return linkedin_url = "", candidates = [], status = "not_found".

Using the context field:
- The input may include a `context` string with extra facts mined from the user's own
  emails (past employers, education, location, phone/handles, a search hint). TREAT IT AS
  STRONG EVIDENCE about the SAME person, even when their current employer differs from it
  (people change jobs — a past role like "private equity" still confirms identity).
- Use it to disambiguate same-name people and to RAISE confidence when a candidate's
  history matches the context, even if their current title is different from the company field.

Output rules:
- Return valid JSON matching the schema.
- Use status = "completed" when you found the LinkedIn person, even if x_handle is empty.
- Use status = "not_found" only if no reliable LinkedIn profile is found.
"""

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string", "description": "Full name of the person"},
        "company": {"type": "string", "description": "Company name where the person works"},
        "email": {"type": "string", "description": "Work email address (for context)"},
        "context": {"type": "string", "description": "Extra known facts mined from our own emails (past roles, education, location, phone/handles, search hint) to disambiguate and confirm the match. May be empty."},
    },
    "required": ["full_name", "company", "email"],
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "linkedin_url": {"type": "string", "description": "Best/top LinkedIn profile URL (empty string if none). Must equal candidates[0].linkedin_url when candidates is non-empty."},
        "x_handle": {"type": "string", "description": "X/Twitter handle (e.g. @username). Empty string unless strong evidence."},
        "status": {"type": "string", "description": "Status: completed, not_found, or error"},
        "candidates": {
            "type": "array",
            "description": "Up to 5 plausible profiles, best first. Empty if none found.",
            "items": {
                "type": "object",
                "properties": {
                    "linkedin_url": {"type": "string"},
                    "name": {"type": "string"},
                    "headline": {"type": "string", "description": "Current title/company headline"},
                    "location": {"type": "string"},
                    "match_confidence": {"type": "number", "description": "0..1 confidence this candidate is the person"},
                    "evidence": {"type": "string", "description": "Why this candidate matches the input"},
                },
                "required": ["linkedin_url"],
            },
        },
    },
    "required": ["linkedin_url", "status"],
}

OUTPUT_COLUMNS = [
    "email", "full_name", "company", "linkedin_url", "x_handle", "status",
    "confidence", "reasoning", "candidates",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(CsvIO.dict_reader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def clean_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip("'\"")
    name = re.sub(r'\s*\(via [^)]+\)', '', name)
    name = re.sub(r'^(Mr\.|Mrs\.|Ms\.|Dr\.)\s*', '', name)
    if ',' in name and name.count(',') == 1:
        parts = name.split(',')
        if len(parts[0].split()) == 1 and len(parts[1].strip().split()) >= 1:
            name = f"{parts[1].strip()} {parts[0].strip()}"
    if name == name.lower():
        name = name.title()
    return name.strip()


def extract_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@")[1].lower()


def load_contacts(input_csv: Path) -> list[dict[str, str]]:
    """Load contacts from a resolution queue CSV, derive company from email domain."""
    contacts: list[dict[str, str]] = []
    skipped: list[str] = []
    for row in read_csv_rows(input_csv):
        name = row.get("display_name") or row.get("full_name") or ""
        email = row.get("primary_email") or row.get("email") or row.get("handle") or ""
        email_type = row.get("primary_email_type", "")
        if not name or not email:
            continue
        if is_generic_or_non_person(email) or not is_likely_person_name(name):
            skipped.append(email)
            continue
        domain = extract_domain(email)
        is_personal = domain in PERSONAL_DOMAINS or email_type == "personal"
        if not is_personal:
            company = domain.replace('.com', '').replace('.co', '').replace('.io', '').replace('.org', '').title()
        else:
            company = ""
        contacts.append({
            "full_name": clean_name(name),
            "company": company,
            "email": email.lower().strip(),
            "is_personal": str(is_personal).lower(),
            "context": (row.get("context") or "").strip(),
        })
    if skipped:
        print(f"[resolve] Filtered {len(skipped)} generic/non-person emails", file=sys.stderr)
    return contacts


def load_existing_results(output_csv: Path) -> set[str]:
    """Load emails already resolved from a prior output CSV."""
    seen: set[str] = set()
    for row in read_csv_rows(output_csv):
        email = (row.get("email") or "").strip().lower()
        if email:
            seen.add(email)
    return seen


def check_supabase_cache(emails: list[str]) -> tuple[list[dict[str, Any]], set[str]]:
    """Check email_lookup_cache_v2 in Supabase. Returns (cached_rows, cached_emails)."""
    try:
        import psycopg2
    except ImportError:
        return [], set()
    pg_host = os.environ.get("POSTGRES_HOST", "").strip()
    pg_password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    if not pg_host or not pg_password:
        return [], set()
    try:
        conn = psycopg2.connect(
            host=pg_host,
            port=os.environ.get("POSTGRES_PORT", "6543"),
            database=os.environ.get("POSTGRES_DB", "postgres"),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=pg_password,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT email, linkedin_url, public_identifier, status, source FROM email_lookup_cache_v2 WHERE email = ANY(%s)",
            ([e.lower().strip() for e in emails],),
        )
        rows = cur.fetchall()
        conn.close()
        cached_rows: list[dict[str, Any]] = []
        cached_emails: set[str] = set()
        for email, linkedin_url, public_id, status, source in rows:
            cached_emails.add(email.lower())
            if status == "found" and linkedin_url:
                cached_rows.append({
                    "email": email,
                    "linkedin_url": linkedin_url,
                    "status": "completed",
                    "confidence": "high",
                    "reasoning": f"From Supabase cache ({source})",
                })
            else:
                cached_rows.append({
                    "email": email,
                    "linkedin_url": "",
                    "status": "not_found",
                    "confidence": "",
                    "reasoning": f"Previously not_found ({source})",
                })
        print(f"[resolve] Supabase cache: {len(cached_rows)} hits ({sum(1 for r in cached_rows if r['status']=='completed')} found, {sum(1 for r in cached_rows if r['status']=='not_found')} not_found)", file=sys.stderr)
        return cached_rows, cached_emails
    except Exception as exc:
        print(f"[resolve] Supabase cache check failed: {exc}", file=sys.stderr)
        return [], set()


# ── Parallel.ai client (sync, no SDK) ────────────────────────

class ParallelClient:
    def __init__(self, api_key: str, base_url: str, beta: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.beta = beta

    def request(self, method: str, path: str, body: Any = None, timeout: int = 60) -> tuple[int, Any, str]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"x-api-key": self.api_key, "parallel-beta": self.beta, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(raw) if raw else None, ""
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw) if raw else None, raw
            except json.JSONDecodeError:
                return exc.code, None, raw

    def create_group(self) -> dict[str, Any]:
        status, body, raw = self.request("POST", "/v1beta/tasks/groups", {"metadata": {"source": "powerpacks-email-resolution", "submitted_at": now_iso()}})
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"create_group failed HTTP {status}: {raw[:200]}")
        return body

    def add_runs(self, group_id: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        status, body, raw = self.request("POST", f"/v1beta/tasks/groups/{group_id}/runs", {"inputs": inputs})
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"add_runs failed HTTP {status}: {raw[:200]}")
        return body

    def get_group(self, group_id: str) -> dict[str, Any]:
        status, body, raw = self.request("GET", f"/v1beta/tasks/groups/{group_id}")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_group failed HTTP {status}: {raw[:200]}")
        return body

    def get_result(self, run_id: str) -> dict[str, Any] | None:
        path = f"/v1/tasks/runs/{run_id}/result?" + urllib.parse.urlencode({"beta": "true", "api_timeout": 60})
        status, body, raw = self.request("GET", path, timeout=75)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_result failed HTTP {status}: {raw[:200]}")
        return body


# ── Main enrichment logic ─────────────────────────────────────

def task_spec() -> dict[str, Any]:
    return {
        "instructions": PARALLEL_ENRICH_INSTRUCTIONS,
        "input_schema": {"json_schema": INPUT_SCHEMA},
        "output_schema": {"json_schema": OUTPUT_SCHEMA},
    }


def submit_and_poll(
    client: ParallelClient,
    contacts: list[dict[str, str]],
    processor: str,
    *,
    ledger_path: Path,
    ledger: dict[str, Any],
    batch_size: int = 1000,
    max_wait: int = 900,
) -> list[dict[str, Any]]:
    """Submit contacts to Parallel, poll until done, persist progress, return results."""
    group = client.create_group()
    group_id = group.get("taskgroup_id") or group.get("id")
    ledger["parallel"] = {"taskgroup_id": group_id, "run_ids": [], "submitted_at": now_iso(), "contacts": len(contacts)}
    ledger["status"] = "submitted"
    save_ledger(ledger_path, ledger)
    print(f"[resolve] Created task group {group_id} for {len(contacts)} contacts", file=sys.stderr, flush=True)
    sys.stdout.write(json.dumps({"progress": "submitted", "taskgroup_id": group_id, "contacts": len(contacts)}) + "\n")
    sys.stdout.flush()

    spec = task_spec()
    run_ids: list[str] = []
    run_id_to_contact: dict[str, dict[str, str]] = {}
    for i in range(0, len(contacts), batch_size):
        batch = contacts[i:i + batch_size]
        inputs = [{
            "task_spec": spec,
            "input": {"full_name": c["full_name"], "company": c["company"], "email": c["email"], "context": c.get("context", "")},
            "metadata": {"email": c["email"]},
            "processor": processor,
        } for c in batch]
        resp = client.add_runs(group_id, inputs)
        new_ids = resp.get("run_ids") or []
        for rid, contact in zip(new_ids, batch):
            run_id_to_contact[rid] = contact
        run_ids.extend(new_ids)
        ledger["parallel"]["run_ids"] = run_ids
        ledger["parallel"]["submitted_runs"] = len(run_ids)
        save_ledger(ledger_path, ledger)
        print(f"[resolve] Submitted batch {i // batch_size + 1}: {len(new_ids)} runs (total {len(run_ids)})", file=sys.stderr, flush=True)
        sys.stdout.flush()

    # Poll
    start = time.time()
    while True:
        group_status = client.get_group(group_id)
        status = group_status.get("status", {})
        counts = status.get("task_run_status_counts", {})
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        elapsed = time.time() - start
        ledger["status"] = "polling"
        ledger["parallel"]["last_poll_at"] = now_iso()
        ledger["parallel"]["status_counts"] = counts
        ledger["parallel"]["is_active"] = status.get("is_active")
        save_ledger(ledger_path, ledger)
        print(f"[resolve] {elapsed:.0f}s: completed={completed} failed={failed} active={status.get('is_active')}", file=sys.stderr, flush=True)
        sys.stdout.write(json.dumps({"progress": "polling", "elapsed_s": int(elapsed), "completed": completed, "failed": failed, "total": len(run_ids)}) + "\n")
        sys.stdout.flush()
        if not status.get("is_active", True):
            break
        if elapsed > max_wait:
            print(f"[resolve] Timeout after {elapsed:.0f}s", file=sys.stderr)
            break
        time.sleep(10)

    # Fetch results
    results: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            result = client.get_result(run_id) or {}
            output = result.get("output", {})
            content = output.get("content") if isinstance(output, dict) else {}
            if not isinstance(content, dict):
                continue
            contact = run_id_to_contact.get(run_id, {})
            basis_arr = output.get("basis", []) or []
            basis_by_field: dict[str, dict] = {}
            for b in basis_arr:
                if isinstance(b, dict) and b.get("field"):
                    basis_by_field[b["field"]] = b
            li_basis = basis_by_field.get("linkedin_url", {})
            candidates = content.get("candidates") or []
            if not isinstance(candidates, list):
                candidates = []
            top_url = content.get("linkedin_url", "")
            # Backward-compatible: linkedin_url stays the single top choice. If the
            # model only populated candidates, fall back to the best one.
            if not top_url and candidates and isinstance(candidates[0], dict):
                top_url = candidates[0].get("linkedin_url", "")
            results.append({
                "email": contact.get("email", ""),
                "full_name": contact.get("full_name", ""),
                "company": contact.get("company", ""),
                "linkedin_url": top_url,
                "x_handle": content.get("x_handle", ""),
                "status": content.get("status", ""),
                "confidence": li_basis.get("confidence", ""),
                "reasoning": li_basis.get("reasoning", ""),
                "candidates": json.dumps(candidates, ensure_ascii=False),
            })
        except Exception as exc:
            print(f"[resolve] Failed to fetch result for {run_id}: {exc}", file=sys.stderr)

    ledger["status"] = "fetched"
    ledger["parallel"]["results_fetched"] = len(results)
    save_ledger(ledger_path, ledger)
    print(f"[resolve] Fetched {len(results)} results", file=sys.stderr, flush=True)
    sys.stdout.write(json.dumps({"progress": "fetched", "results": len(results)}) + "\n")
    sys.stdout.flush()
    return results


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def run_enrichment(input_csv: Path, output_dir: Path, ledger_path: Path, *, provider: str, processor: str, limit: int | None, approve_spend: bool, base_url: str, beta: str) -> dict[str, Any]:
    """Main entry point: load contacts, dedup, submit, write results."""
    output_csv = output_dir / "linkedin_resolutions.csv"
    all_contacts = load_contacts(input_csv)
    if not all_contacts:
        return {"status": "completed", "contacts_loaded": 0, "processed": 0, "output": str(output_csv)}

    # Dedup against existing output
    existing = load_existing_results(output_csv)
    to_process = [c for c in all_contacts if c["email"] not in existing]

    if limit is not None:
        to_process = to_process[:limit]

    ledger: dict[str, Any] = {
        "primitive": "gmail/resolve_queue",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "output_dir": str(output_dir),
        "input": {
            "input_csv": str(input_csv),
            "provider": provider,
            "processor": processor,
            "limit": limit,
        },
        "contacts_loaded": len(all_contacts),
        "already_resolved": len(existing),
        "to_process": len(to_process),
    }
    save_ledger(ledger_path, ledger)

    if not to_process:
        ledger["status"] = "completed"
        ledger["message"] = "All contacts already resolved"
        save_ledger(ledger_path, ledger)
        return {"status": "completed", "output": str(output_csv), "contacts_loaded": len(all_contacts), "already_resolved": len(existing), "processed": 0}

    if provider == "harness":
        output_dir.mkdir(parents=True, exist_ok=True)
        prompts = output_dir / "harness_prompts.jsonl"
        with prompts.open("w", encoding="utf-8") as f:
            for c in to_process:
                f.write(json.dumps({"instructions": PARALLEL_ENRICH_INSTRUCTIONS, "input": {"full_name": c["full_name"], "company": c["company"], "email": c["email"], "context": c.get("context", "")}}) + "\n")
        ledger["status"] = "prepared_harness"
        ledger["artifacts"] = {"prompts_jsonl": str(prompts)}
        save_ledger(ledger_path, ledger)
        return {"status": "prepared_harness", "rows": len(to_process), "prompts_jsonl": str(prompts), "output": str(output_csv)}

    # Parallel provider
    if not approve_spend:
        ledger["status"] = "blocked_approval"
        ledger["blocked"] = {"step": "parallel_submit", "approval_type": "external_api_spend", "contacts": len(to_process)}
        save_ledger(ledger_path, ledger)
        emit({
            "status": "blocked_approval",
            "approval_type": "external_api_spend",
            "message": f"Approve Parallel.ai spend for {len(to_process)} email→LinkedIn lookups?",
            "contacts": len(to_process),
            "ledger": str(ledger_path),
            "output": str(output_csv),
        })
        return {"status": "blocked_approval", "contacts": len(to_process), "output": str(output_csv)}

    api_key = os.getenv("PARALLEL_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("PARALLEL_API_KEY is not set")

    results = submit_and_poll(
        ParallelClient(api_key, base_url, beta),
        to_process, processor,
        ledger_path=ledger_path,
        ledger=ledger,
    )

    # Merge with existing results + new results
    existing_rows = read_csv_rows(output_csv)
    all_rows = existing_rows + results
    write_csv(output_csv, OUTPUT_COLUMNS, all_rows)

    found = sum(1 for r in results if r.get("linkedin_url") and r.get("status") == "completed")
    not_found = sum(1 for r in results if r.get("status") == "not_found")

    ledger["status"] = "completed"
    ledger["results"] = {"processed": len(results), "found": found, "not_found": not_found}
    save_ledger(ledger_path, ledger)

    return {
        "status": "completed",
        "output": str(output_csv),
        "contacts_loaded": len(all_contacts),
        "already_resolved": len(existing),
        "processed": len(results),
        "found": found,
        "not_found": not_found,
    }


# ── CLI ───────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    result = run_enrichment(
        input_csv=Path(args.input),
        output_dir=Path(args.output_dir),
        ledger_path=Path(args.ledger),
        provider=args.provider,
        processor=args.processor,
        limit=args.limit,
        approve_spend=getattr(args, "approve_spend", False),
        base_url=args.base_url,
        beta=args.beta,
    )
    emit(result)
    if result.get("status") == "blocked_approval":
        return 20
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    emit(read_json(Path(args.ledger), {"status": "missing", "ledger": args.ledger}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve email→LinkedIn via Parallel.ai")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--provider", choices=["harness", "parallel"], default="parallel")
    run.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--processor", default=DEFAULT_PROCESSOR, choices=sorted(ALLOWED_PROCESSORS))
    run.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run.add_argument("--beta", default=DEFAULT_BETA)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--approve-spend", action="store_true", help="Auto-approve Parallel spend")
    run.set_defaults(func=cmd_run)
    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
