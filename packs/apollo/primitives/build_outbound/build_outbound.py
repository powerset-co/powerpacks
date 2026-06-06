#!/usr/bin/env python3
"""Apollo build-outbound primitive.

Stdlib-only CLI for resolving Sales Navigator artifacts, previewing outbound
copy, building inactive Apollo sequences, and activating an already-built
campaign with an exact id confirmation.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RUNS = Path(".powerpacks/sales-nav/runs")
DEFAULT_OUT = Path(".powerpacks/apollo/build-outbound")
SOURCE = "powerpacks_build_outbound"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_slug(value: str, default: str = "build-outbound") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (slug[:60] or default)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(mask_for_console(payload), indent=2, sort_keys=True))
    return code


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def normalize_tokens(value: str | None) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split() if t]


def query_from_state(state_obj: dict[str, Any]) -> str:
    for key in ("query", "search_query", "search", "name"):
        val = state_obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    plan = state_obj.get("search_plan") or state_obj.get("plan")
    if isinstance(plan, dict):
        for key in ("query", "description", "name"):
            val = plan.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def updated_from_state(path: Path, state_obj: dict[str, Any]) -> str:
    val = state_obj.get("updated_at") or state_obj.get("created_at")
    if isinstance(val, str) and val:
        return val
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")


def candidate_summary(state_path: Path, state_obj: dict[str, Any]) -> dict[str, Any]:
    files = state_obj.get("files") if isinstance(state_obj.get("files"), dict) else {}
    return {
        "state_path": str(state_path),
        "run_dir": str(state_path.parent),
        "query": query_from_state(state_obj),
        "updated_at": updated_from_state(state_path, state_obj),
        "files": files,
    }


def discover_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for path in (current, *current.parents):
        if (path / ".git").exists() or (path / "packs").exists():
            return path
    return ROOT


def resolve_existing_path(raw: str | Path, *, state_path: Path | None = None, repo_root: Path | None = None) -> Path:
    value = Path(raw).expanduser()
    candidates: list[Path] = []
    if value.is_absolute():
        candidates.append(value)
    else:
        candidates.append((Path.cwd() / value))
        candidates.append((repo_root or ROOT) / value)
        if state_path:
            candidates.append(state_path.parent / value)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if str(resolved) not in seen:
            unique.append(resolved)
            seen.add(str(resolved))
    for candidate in unique:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(json.dumps({"path": str(raw), "candidates": [str(c) for c in unique]}))


def state_path_from_manifest(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    paths: list[Any] = [manifest.get("state_path"), manifest.get("state")]
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    paths.extend([files.get("state_json"), files.get("state")])
    for value in paths:
        if isinstance(value, str) and value.strip():
            return resolve_existing_path(value, state_path=manifest_path, repo_root=ROOT)
    sibling = manifest_path.parent / "state.json"
    if sibling.exists():
        return sibling.resolve()
    raise FileNotFoundError(json.dumps({
        "error": "manifest did not identify state.json",
        "manifest": str(manifest_path),
        "tried": [str(sibling)],
    }))


def resolve_sales_nav_state(
    query_hint: str | None,
    sales_nav_manifest: Path | None,
    state: Path | None,
    run_dir: Path | None,
) -> dict[str, Any]:
    try:
        if sales_nav_manifest:
            manifest_path = resolve_existing_path(sales_nav_manifest, repo_root=ROOT)
            manifest = read_json(manifest_path)
            state_path = state_path_from_manifest(manifest_path, manifest if isinstance(manifest, dict) else {})
            state_obj = read_json(state_path)
            return {
                "ok": True,
                "selected": candidate_summary(state_path, state_obj),
                "manifest_path": str(manifest_path),
            }
        if state:
            state_path = resolve_existing_path(state, repo_root=ROOT)
            state_obj = read_json(state_path)
            return {"ok": True, "selected": candidate_summary(state_path, state_obj)}
        if run_dir:
            rd = resolve_existing_path(run_dir, repo_root=ROOT)
            state_path = rd / "state.json" if rd.is_dir() else rd
            if not state_path.exists():
                raise FileNotFoundError(str(state_path))
            state_obj = read_json(state_path)
            return {"ok": True, "selected": candidate_summary(state_path, state_obj)}

        base = (ROOT / DEFAULT_RUNS).resolve()
        states = sorted(base.glob("*/state.json")) if base.exists() else []
        hint_tokens = normalize_tokens(query_hint)
        matches: list[dict[str, Any]] = []
        for state_path in states:
            try:
                state_obj = read_json(state_path)
            except Exception:
                continue
            summary = candidate_summary(state_path, state_obj if isinstance(state_obj, dict) else {})
            haystack = " ".join(normalize_tokens(summary.get("query") or ""))
            if hint_tokens and not all(token in haystack for token in hint_tokens):
                continue
            matches.append(summary)
        if not matches:
            return {"ok": False, "error": "no matching Sales Nav state found", "candidates": []}
        matches.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        newest = matches[0].get("updated_at")
        ties = [m for m in matches if m.get("updated_at") == newest]
        if len(ties) != 1:
            return {"ok": False, "error": "multiple equally-new Sales Nav states matched", "candidates": ties}
        return {"ok": True, "selected": ties[0], "candidates": matches[:10]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "candidates": []}


def state_files(state_path: Path) -> dict[str, Path]:
    state_obj = read_json(state_path)
    files = state_obj.get("files") if isinstance(state_obj, dict) and isinstance(state_obj.get("files"), dict) else {}
    out: dict[str, Path] = {}
    diagnostics: dict[str, Any] = {}
    for key, value in files.items():
        if isinstance(value, str) and value.strip():
            try:
                out[key] = resolve_existing_path(value, state_path=state_path, repo_root=ROOT)
            except FileNotFoundError as exc:
                diagnostics[key] = str(exc)
    if "leads_jsonl" not in out:
        for key in ("leads_csv", "final_leads_csv", "leads", "leads_path"):
            if key in out:
                out["leads_jsonl"] = out[key]
                break
    if "leads_jsonl" not in out:
        for name in ("leads.jsonl", "lead_rows.jsonl", "results.jsonl"):
            candidate = state_path.parent / name
            if candidate.exists():
                out["leads_jsonl"] = candidate.resolve()
                break
    if "leads_jsonl" not in out:
        for name in ("leads.csv", "exports/leads.csv"):
            candidate = state_path.parent / name
            if candidate.exists():
                out["leads_jsonl"] = candidate.resolve()
                break
    if "leads_jsonl" not in out:
        raise FileNotFoundError(json.dumps({
            "error": "leads_jsonl not found",
            "state_path": str(state_path),
            "diagnostics": diagnostics,
        }))
    return out


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for i, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{i}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_lead_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    return read_jsonl(path)


def first_value(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_linkedin_url(value: str) -> str | None:
    if not value:
        return None
    raw = value.strip()
    match = re.search(r"https?://(?:[\w-]+\.)?linkedin\.com/in/[^\s,\"'\]\)]+", raw, flags=re.I)
    if match:
        raw = match.group(0)
    match = re.search(r"https?://[^\s\"']+", raw)
    if match:
        raw = match.group(0)
    raw = raw.strip("[]()<>.,")
    if "linkedin.com/in/" not in raw.lower():
        return None
    raw = raw.split("?")[0].split("#")[0].rstrip("/")
    raw = re.sub(r"^http://", "https://", raw, flags=re.I)
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.startswith("linkedin.com"):
        raw = "https://www." + raw
    raw = re.sub(r"https://(\w+\.)?linkedin\.com", "https://www.linkedin.com", raw, flags=re.I)
    return raw


def split_name(name: str) -> tuple[str, str]:
    parts = [p for p in (name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def normalize_lead(row: dict[str, Any]) -> dict[str, Any] | None:
    linkedin = normalize_linkedin_url(first_value(
        row,
        ("linkedin_url", "linkedin", "linkedin_profile_url", "profile_url", "url"),
    ))
    if not linkedin:
        return None
    name = first_value(row, ("name", "full_name", "person_name"))
    first = first_value(row, ("first_name", "firstname", "given_name"))
    last = first_value(row, ("last_name", "lastname", "family_name", "surname"))
    if not name:
        name = " ".join(v for v in (first, last) if v)
    if not first and not last:
        first, last = split_name(name)
    return {
        "name": name,
        "first_name": first,
        "last_name": last,
        "title": first_value(row, ("title", "job_title", "headline", "current_title")),
        "company": first_value(row, ("company", "company_name", "organization_name", "current_company")),
        "linkedin_url": linkedin,
        "location": first_value(row, ("location", "present_raw_address", "person_location")),
        "source": row,
    }


def load_sales_nav_leads(state_path: Path, limit: int | None) -> list[dict[str, Any]]:
    global LAST_LEAD_LOAD_SUMMARY
    files = state_files(state_path)
    seen: set[str] = set()
    leads: list[dict[str, Any]] = []
    skipped = {"no_linkedin_profile_url": 0, "duplicates": 0}
    for row in read_lead_rows(files["leads_jsonl"]):
        lead = normalize_lead(row)
        if not lead:
            skipped["no_linkedin_profile_url"] += 1
            continue
        key = (lead["linkedin_url"] or "").lower()
        if key in seen:
            skipped["duplicates"] += 1
            continue
        seen.add(key)
        leads.append(lead)
        if limit and len(leads) >= limit:
            break
    LAST_LEAD_LOAD_SUMMARY = {"path": str(files["leads_jsonl"]), "count": len(leads), "skipped": skipped}
    return leads


LAST_LEAD_LOAD_SUMMARY: dict[str, Any] = {}


def default_sequence(instructions: str, search_query: str) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    query = search_query or "Sales Nav leads"
    name = f"{query[:54]} outbound {stamp}"
    context = instructions.strip() or "Introduce our work and ask whether it is worth a short conversation."
    return {
        "name": name,
        "steps": [
            {
                "subject": "Quick question, {{first_name}}",
                "body_text": (
                    f"Hi {{{{first_name}}}},\n\nI noticed your work around {query}. "
                    f"{context}\n\nWorth a quick conversation next week?\n\nBest,"
                ),
                "wait_time": 0,
                "wait_mode": "day",
            },
            {
                "subject": "Re: quick question",
                "body_text": (
                    "Hi {{first_name}},\n\nWanted to bump this in case it got buried. "
                    "If improving the team’s outbound motion is relevant, I’d be glad to compare notes."
                    "\n\nOpen to a short chat?"
                ),
                "wait_time": 3,
                "wait_mode": "day",
            },
            {
                "subject": "Should I close the loop?",
                "body_text": (
                    "Hi {{first_name}},\n\nI do not want to crowd your inbox. "
                    "Should I close the loop, or is there someone better on your team to speak with?"
                    "\n\nThanks,"
                ),
                "wait_time": 7,
                "wait_mode": "day",
            },
        ],
    }


def body_html_from_text(text: str) -> str:
    return "<p>" + html.escape(text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def validate_sequence(sequence: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(sequence, dict):
        raise ValueError("sequence JSON must be an object")
    name = str(sequence.get("name") or "").strip()
    steps = sequence.get("steps")
    if not name:
        raise ValueError("sequence JSON requires name")
    if not isinstance(steps, list) or not (1 <= len(steps) <= 5):
        raise ValueError("sequence JSON requires 1-5 steps")
    clean_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        subject = str(step.get("subject") or "").strip()
        body_text = str(step.get("body_text") or "").strip()
        if not subject or not body_text:
            raise ValueError(f"step {index} requires subject and body_text")
        wait_time = step.get("wait_time", 0 if index == 1 else 3)
        try:
            wait_time = int(wait_time)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"step {index} wait_time must be an integer") from exc
        clean_steps.append({
            "subject": subject,
            "body_text": body_text,
            "body_html": str(step.get("body_html") or body_html_from_text(body_text)),
            "wait_time": wait_time,
            "wait_mode": str(step.get("wait_mode") or "day"),
        })
    return {"name": name, "steps": clean_steps}


def load_sequence_json(path: Path) -> dict[str, Any]:
    return validate_sequence(read_json(path))


def markdown_preview(sequence: dict[str, Any], selected_sales_nav: dict[str, Any]) -> str:
    lines = [
        "# Apollo build-outbound sequence preview",
        "",
        f"Sales Nav query: {selected_sales_nav.get('query') or ''}",
        f"Sales Nav state: {selected_sales_nav.get('state_path') or ''}",
        f"Lead count: {selected_sales_nav.get('lead_count') or selected_sales_nav.get('count') or ''}",
        "",
        f"Sequence: {sequence.get('name')}",
        "",
    ]
    for i, step in enumerate(sequence.get("steps") or [], 1):
        lines.extend([
            f"## Step {i} (wait {step.get('wait_time', 0)} {step.get('wait_mode', 'day')})",
            "",
            f"Subject: {step.get('subject')}",
            "",
            str(step.get("body_text") or ""),
            "",
        ])
    return "\n".join(lines)


def write_sequence_preview(
    sequence: dict[str, Any],
    selected_sales_nav: dict[str, Any],
    out_dir: Path,
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    seq_path = out_dir / "sequence_input.json"
    md_path = out_dir / "sequence_preview.md"
    write_json(seq_path, sequence)
    write_text(md_path, markdown_preview(sequence, selected_sales_nav))
    return {"sequence_input_json": str(seq_path), "sequence_preview_md": str(md_path)}


def mask_email(value: str) -> str:
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    masked_local = (local[:1] + "***") if local else "***"
    dparts = domain.split(".")
    masked_domain = (dparts[0][:1] + "***") + ("." + ".".join(dparts[1:]) if len(dparts) > 1 else "")
    return masked_local + "@" + masked_domain


def mask_for_console(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if "api_key" in str(k).lower() or "token" in str(k).lower():
                out[k] = "<REDACTED>"
            else:
                out[k] = mask_for_console(v)
        return out
    if isinstance(value, list):
        return [mask_for_console(v) for v in value]
    if isinstance(value, str):
        text = value
        api_key = os.environ.get("APOLLO_API_KEY")
        if api_key:
            text = text.replace(api_key, "<REDACTED>")
        text = re.sub(r"APOLLO_API_KEY=([^\s\"']+)", "APOLLO_API_KEY=<REDACTED>", text)
        return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", lambda m: mask_email(m.group(0)), text)
    return value


class ApolloClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://api.apollo.io/api/v1") -> None:
        self.api_key = api_key or os.environ.get("APOLLO_API_KEY")
        if not self.api_key:
            raise RuntimeError("APOLLO_API_KEY is required for Apollo API calls")
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "x-api-key": self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Apollo {method} {path} failed: HTTP {exc.code}: {body[:1000]}") from exc
        return json.loads(text) if text.strip() else {}

    def get_email_accounts(self) -> dict[str, Any]:
        return self.request("GET", "/email_accounts")

    def get_emailer_schedules(self) -> dict[str, Any]:
        return self.request("GET", "/emailer_schedules")

    def bulk_match(self, linkedin_urls: list[str]) -> list[dict[str, Any]]:
        responses = []
        for start in range(0, len(linkedin_urls), 10):
            details = [{"linkedin_url": url} for url in linkedin_urls[start:start + 10]]
            responses.append(self.request(
                "POST",
                "/people/bulk_match",
                {"details": details, "reveal_personal_emails": False},
            ))
        return responses

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/emailer_campaigns", payload)

    def create_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/emailer_steps", payload)

    def search_campaigns(self, q_keywords: str, per_page: int = 25) -> dict[str, Any]:
        return self.request("POST", "/emailer_campaigns/search", {
            "q_keywords": q_keywords,
            "per_page": per_page,
        })

    def patch_template(self, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/emailer_templates/{template_id}", payload)

    def approve_touch(self, touch_id: str) -> dict[str, Any]:
        return self.request("POST", f"/emailer_touches/{touch_id}/approve", {})

    def search_contacts(self, email: str) -> dict[str, Any]:
        return self.request("POST", "/contacts/search", {"contact_emails": [email], "per_page": 10})

    def create_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/contacts", payload)

    def add_contact_ids(self, campaign_id: str, contact_ids: list[str], sender_id: str) -> dict[str, Any]:
        return self.request("POST", f"/emailer_campaigns/{campaign_id}/add_contact_ids", {
            "emailer_campaign_id": campaign_id,
            "contact_ids": contact_ids,
            "send_email_from_email_account_id": sender_id,
            "sequence_active_in_other_campaigns": False,
        })

    def approve_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self.request("POST", f"/emailer_campaigns/{campaign_id}/approve", {})

    def search_messages(self, campaign_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/emailer_messages/search",
            {"emailer_campaign_ids": [campaign_id], "page": 1, "per_page": 100},
        )


def list_from_response(resp: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = resp.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def choose_sender(resp: dict[str, Any], override: str | None) -> dict[str, Any]:
    accounts = list_from_response(resp, ("email_accounts", "accounts", "data"))
    if override:
        for account in accounts:
            if str(account.get("id")) == str(override):
                return account
        return {"id": override}
    candidates = [
        a for a in accounts
        if a.get("active") is not False and a.get("status") not in {"inactive", "disabled"}
    ]
    defaults = [a for a in candidates if a.get("default") or a.get("is_default")]
    if defaults:
        return defaults[0]
    if candidates:
        return candidates[0]
    raise RuntimeError("No active Apollo sender email account available")


def choose_schedule(resp: dict[str, Any], override: str | None) -> dict[str, Any]:
    schedules = list_from_response(resp, ("emailer_schedules", "schedules", "data"))
    if override:
        for sched in schedules:
            if str(sched.get("id")) == str(override):
                return sched
        return {"id": override}
    defaults = [s for s in schedules if s.get("default") or s.get("is_default")]
    if defaults:
        return defaults[0]
    if schedules:
        return schedules[0]
    raise RuntimeError("No Apollo emailer schedule available")


def extract_people(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for resp in raw:
        for key in ("people", "matches", "contacts", "data"):
            val = resp.get(key)
            if isinstance(val, list):
                people.extend([p for p in val if isinstance(p, dict)])
                break
    return people


def email_from_person(person: dict[str, Any]) -> str:
    for key in ("email", "email_address", "work_email"):
        val = person.get(key)
        if isinstance(val, str) and "@" in val:
            return val.strip().lower()
    return ""


def contact_id_for_email(search_resp: dict[str, Any], email: str) -> str | None:
    expected = email.strip().lower()
    if not expected:
        return None
    for contact in list_from_response(search_resp, ("contacts", "people", "data")):
        if email_from_person(contact) != expected:
            continue
        cid = contact.get("id") or contact.get("contact_id")
        if cid:
            return str(cid)
    return None


def id_from_response(resp: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        val = resp.get(key)
        if isinstance(val, (str, int)):
            return str(val)
    for container in ("emailer_campaign", "emailer_step", "contact", "emailer_template", "emailer_touch"):
        nested = resp.get(container)
        if isinstance(nested, dict):
            found = id_from_response(nested, *keys)
            if found:
                return found
    return None


def nested_id(resp: dict[str, Any], container: str, *keys: str) -> str | None:
    nested = resp.get(container)
    if isinstance(nested, dict):
        found = id_from_response(nested, *keys)
        if found:
            return found
    return id_from_response(resp, *(key for key in keys if key != "id"))


def campaign_is_inactive(resp: dict[str, Any]) -> bool:
    campaign = resp.get("emailer_campaign") if isinstance(resp.get("emailer_campaign"), dict) else resp
    status = str(campaign.get("status") or campaign.get("state") or "").lower()
    if campaign.get("active") is True or campaign.get("is_active") is True:
        return False
    if status in {"active", "running", "scheduled", "approved"}:
        return False
    return True


def campaign_from_search(resp: dict[str, Any], campaign_id: str) -> dict[str, Any] | None:
    for campaign in list_from_response(resp, ("emailer_campaigns", "campaigns", "sequences", "data")):
        if str(campaign.get("id")) == str(campaign_id):
            return campaign
    return None


def verify_campaign_still_inactive(client: ApolloClient, campaign_id: str, sequence_name: str) -> dict[str, Any]:
    resp = client.search_campaigns(sequence_name)
    campaign = campaign_from_search(resp, campaign_id)
    if not campaign:
        raise RuntimeError("created Apollo campaign was not found before touch approval; refusing to approve touch")
    if not campaign_is_inactive(campaign):
        raise RuntimeError("Apollo campaign became active before touch approval; refusing to schedule messages")
    return campaign


def build_run_dir(out_dir: Path | None, query: str) -> Path:
    base = out_dir or DEFAULT_OUT
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{safe_slug(query)}-{uuid.uuid4().hex[:6]}"
    return (ROOT / base if not base.is_absolute() else base) / run_id


def selected_and_leads(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    resolved = resolve_sales_nav_state(
        args.query_hint,
        args.sales_nav_manifest,
        args.state,
        getattr(args, "run_dir", None),
    )
    if not resolved.get("ok"):
        raise RuntimeError(json.dumps(resolved))
    selected = dict(resolved["selected"])
    state_path = Path(selected["state_path"])
    leads = load_sales_nav_leads(state_path, args.limit)
    selected["lead_count"] = len(leads)
    selected["lead_load_summary"] = LAST_LEAD_LOAD_SUMMARY
    if resolved.get("manifest_path"):
        selected["manifest_path"] = resolved["manifest_path"]
    return selected, leads, state_path


def command_resolve(args: argparse.Namespace) -> int:
    resolved = resolve_sales_nav_state(args.query_hint, args.sales_nav_manifest, args.state, args.run_dir)
    if resolved.get("ok"):
        state_path = Path(resolved["selected"]["state_path"])
        leads = load_sales_nav_leads(state_path, args.limit)
        resolved["selected"]["lead_count"] = len(leads)
        resolved["selected"]["lead_load_summary"] = LAST_LEAD_LOAD_SUMMARY
    return emit(resolved, 0 if resolved.get("ok") else 2)


def command_preview(args: argparse.Namespace) -> int:
    selected, leads, _ = selected_and_leads(args)
    if args.sequence_json:
        sequence = load_sequence_json(resolve_existing_path(args.sequence_json, repo_root=ROOT))
    else:
        sequence = validate_sequence(default_sequence(args.instructions, selected.get("query") or ""))
    out_dir = Path(args.out_dir) if args.out_dir else build_run_dir(None, selected.get("query") or "preview")
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    paths = write_sequence_preview(sequence, selected, out_dir)
    write_json(out_dir / "selected_sales_nav.json", selected)
    return emit({
        "ok": True,
        "lead_count": len(leads),
        "selected_sales_nav": selected,
        "sequence": sequence,
        "artifacts": paths,
    })


def enrichment_plan(leads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "endpoint": "/people/bulk_match",
        "chunk_size": 10,
        "count": len(leads),
        "details": [{"linkedin_url": lead["linkedin_url"]} for lead in leads],
    }


def contact_payload_from_person(
    person: dict[str, Any],
    lead_by_url: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    email = email_from_person(person)
    linkedin = normalize_linkedin_url(str(person.get("linkedin_url") or person.get("linkedin_profile_url") or "")) or ""
    lead = lead_by_url.get(linkedin.lower(), {})
    first = str(person.get("first_name") or lead.get("first_name") or "").strip()
    last = str(person.get("last_name") or lead.get("last_name") or "").strip()
    if not (email and first and last):
        return None
    return {
        "email": email,
        "first_name": first,
        "last_name": last,
        "title": str(person.get("title") or lead.get("title") or "").strip() or None,
        "organization_name": (
            str(person.get("organization_name") or person.get("company") or lead.get("company") or "").strip()
            or None
        ),
        "linkedin_url": linkedin or lead.get("linkedin_url"),
        "run_dedupe": True,
    }


def dedupe_contact_payloads(
    people: list[dict[str, Any]],
    lead_by_url: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    payloads: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    skipped = 0
    for person in people:
        payload = contact_payload_from_person(person, lead_by_url)
        if not payload:
            skipped += 1
            continue
        email = payload["email"]
        if email in seen_emails:
            skipped += 1
            continue
        seen_emails.add(email)
        payloads.append(payload)
    return payloads, skipped


def dry_run_apollo_payloads(
    sequence: dict[str, Any],
    selected: dict[str, Any],
    leads: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    campaign_id = "<created_campaign_id>"
    cache = f"build-outbound-dry-run-{uuid.uuid4().hex[:8]}"
    return {
        "campaign": campaign_payload(
            sequence,
            args.user_id or "<sender_user_id>",
            args.emailer_schedule_id or "<schedule_id>",
            cache,
        ),
        "steps": [step_payload(campaign_id, step, i, cache) for i, step in enumerate(sequence["steps"], 1)],
        "add_contacts": {
            "url": f"/emailer_campaigns/{campaign_id}/add_contact_ids",
            "body": {
                "emailer_campaign_id": campaign_id,
                "contact_ids": ["<deduped_or_created_contact_ids>"],
                "send_email_from_email_account_id": args.send_email_from_email_account_id or "<sender_id>",
                "sequence_active_in_other_campaigns": False,
            },
        },
        "selected_query": selected.get("query"),
        "lead_count": len(leads),
    }


def campaign_payload(sequence: dict[str, Any], user_id: str, schedule_id: str, cache_key: str) -> dict[str, Any]:
    return {
        "name": sequence["name"],
        "user_id": user_id,
        "type": "day_interval",
        "emailer_schedule_id": schedule_id,
        "creation_type": "manual",
        "analytics": {},
        "source": SOURCE,
        "cacheKey": cache_key,
    }


def step_payload(campaign_id: str, step: dict[str, Any], position: int, cache_key: str) -> dict[str, Any]:
    return {
        "type": "auto_email",
        "emailer_campaign_id": campaign_id,
        "wait_time": step.get("wait_time", 0),
        "wait_mode": step.get("wait_mode", "day"),
        "position": position,
        "note": "",
        "max_emails_per_day": None,
        "auto_skip_in_x_days": None,
        "cacheKey": cache_key,
    }


def write_common_artifacts(
    run_dir: Path,
    selected: dict[str, Any],
    sequence: dict[str, Any],
    leads: list[dict[str, Any]],
) -> None:
    write_json(run_dir / "selected_sales_nav.json", selected)
    write_json(run_dir / "sequence.json", sequence)
    write_text(run_dir / "sequence_preview.md", markdown_preview(sequence, selected))
    write_json(run_dir / "enrichment_request.json", enrichment_plan(leads))


def write_mutation_artifacts(run_dir: Path, manifest: dict[str, Any], created: dict[str, Any]) -> None:
    write_json(run_dir / "apollo_created.json", created)
    write_json(run_dir / "manifest.json", manifest)


def command_build(args: argparse.Namespace) -> int:
    selected, leads, _ = selected_and_leads(args)
    if args.sequence_json:
        sequence = load_sequence_json(resolve_existing_path(args.sequence_json, repo_root=ROOT))
    elif args.allow_default_copy or args.dry_run:
        sequence = validate_sequence(default_sequence(args.instructions, selected.get("query") or ""))
    else:
        raise RuntimeError("non-dry-run build requires --sequence-json unless --allow-default-copy is passed")
    run_dir = build_run_dir(Path(args.out_dir) if args.out_dir else None, selected.get("query") or sequence["name"])
    write_common_artifacts(run_dir, selected, sequence, leads)
    manifest: dict[str, Any] = {
        "ok": True,
        "primitive": "apollo_build_outbound",
        "source": SOURCE,
        "created_at": now_iso(),
        "run_dir": str(run_dir),
        "dry_run": bool(args.dry_run),
        "selected_sales_nav_path": str(run_dir / "selected_sales_nav.json"),
        "sequence_path": str(run_dir / "sequence.json"),
        "campaign_id": None,
    }
    raw_enrichment: list[dict[str, Any]] = []
    if args.dry_run:
        if args.allow_enrichment_in_dry_run:
            client = ApolloClient()
            raw_enrichment = client.bulk_match([lead["linkedin_url"] for lead in leads])
        write_json(run_dir / "enrichment_raw.json", raw_enrichment)
        write_json(run_dir / "enrichment_summary.json", {
            "enrichment_called": bool(raw_enrichment),
            "matched_people": len(extract_people(raw_enrichment)),
        })
        write_json(run_dir / "contacts.json", {"planned_from_enrichment": bool(raw_enrichment), "contacts": []})
        write_json(run_dir / "apollo_payload_preview.json", {
            "dry_run_payloads": dry_run_apollo_payloads(sequence, selected, leads, args),
        })
        write_text(
            run_dir / "activate_command.txt",
            "Activation unavailable until a non-dry-run campaign_id is created.\n",
        )
        write_json(run_dir / "manifest.json", manifest)
        return emit({
            "ok": True,
            "dry_run": True,
            "run_dir": str(run_dir),
            "lead_count": len(leads),
            "manifest": str(run_dir / "manifest.json"),
        })

    client = ApolloClient()
    sender = choose_sender(client.get_email_accounts(), args.send_email_from_email_account_id)
    schedule = choose_schedule(client.get_emailer_schedules(), args.emailer_schedule_id)
    sender_id = str(sender.get("id"))
    schedule_id = str(schedule.get("id"))
    user_id = str(args.user_id or sender.get("user_id") or sender.get("user", {}).get("id") or "")
    if not sender_id or not schedule_id or not user_id:
        raise RuntimeError("Apollo sender, schedule, and user_id are required")

    raw_enrichment = client.bulk_match([lead["linkedin_url"] for lead in leads])
    write_json(run_dir / "enrichment_raw.json", raw_enrichment)
    people = extract_people(raw_enrichment)
    lead_by_url = {lead["linkedin_url"].lower(): lead for lead in leads}
    contact_payloads, skipped_contact_payloads = dedupe_contact_payloads(people, lead_by_url)
    write_json(run_dir / "enrichment_summary.json", {
        "matched_people": len(people),
        "with_email": sum(1 for person in people if email_from_person(person)),
        "usable_contact_payloads": len(contact_payloads),
        "skipped_contact_payloads": skipped_contact_payloads,
    })
    if not contact_payloads:
        write_json(run_dir / "contacts.json", {
            "contacts": [],
            "contact_ids": [],
            "skipped_contact_payloads": skipped_contact_payloads,
        })
        write_json(run_dir / "manifest.json", manifest)
        raise RuntimeError(
            "Apollo enrichment produced no usable contacts with email, first_name, and last_name; "
            "refusing to create an empty campaign"
        )

    cache_key = f"build-outbound-{uuid.uuid4().hex}"
    camp_resp = client.create_campaign(campaign_payload(sequence, user_id, schedule_id, cache_key))
    campaign_id = id_from_response(camp_resp, "id", "emailer_campaign_id")
    if not campaign_id:
        raise RuntimeError("Apollo campaign response did not include id")
    manifest["campaign_id"] = campaign_id
    manifest["sender_id"] = sender_id
    manifest["schedule_id"] = schedule_id
    manifest["user_id"] = user_id
    manifest["campaign_owned_by_current_build"] = True
    manifest["build_complete"] = False
    manifest["build_stage"] = "campaign_created"
    created: dict[str, Any] = {
        "campaign": camp_resp,
        "steps": [],
        "templates": [],
        "touch_approvals": [],
        "add_contacts": None,
    }
    activate_cmd = (
        f"{sys.executable} {Path(__file__).as_posix()} activate "
        f"--manifest {run_dir / 'manifest.json'} --confirm-activation {campaign_id}\n"
    )
    write_text(run_dir / "activate_command.txt", activate_cmd)
    write_mutation_artifacts(run_dir, manifest, created)
    if not campaign_is_inactive(camp_resp):
        manifest["build_stage"] = "campaign_not_inactive"
        write_mutation_artifacts(run_dir, manifest, created)
        raise RuntimeError("created Apollo campaign is already active; refusing to mutate active sequence")

    for pos, step in enumerate(sequence["steps"], 1):
        step_resp = client.create_step(step_payload(campaign_id, step, pos, cache_key))
        created["steps"].append(step_resp)
        manifest["build_stage"] = f"step_{pos}_created"
        write_mutation_artifacts(run_dir, manifest, created)
        template_id = nested_id(step_resp, "emailer_template", "id", "emailer_template_id", "template_id")
        touch_id = nested_id(step_resp, "emailer_touch", "id", "emailer_touch_id", "touch_id")
        if template_id:
            created["templates"].append(client.patch_template(template_id, {
                "name": f"{sequence['name']} step {pos}",
                "subject": step["subject"],
                "body_text": step["body_text"],
                "body_html": step["body_html"],
            }))
            manifest["build_stage"] = f"step_{pos}_template_patched"
            write_mutation_artifacts(run_dir, manifest, created)
        if touch_id:
            verify_campaign_still_inactive(client, campaign_id, sequence["name"])
            created["touch_approvals"].append(client.approve_touch(touch_id))
            manifest["build_stage"] = f"step_{pos}_touch_approved"
            write_mutation_artifacts(run_dir, manifest, created)

    contact_ids: list[str] = []
    seen_contact_ids: set[str] = set()
    contacts_detail: list[dict[str, Any]] = []
    for payload in contact_payloads:
        search_resp = client.search_contacts(payload["email"])
        cid = contact_id_for_email(search_resp, payload["email"])
        action = "deduped"
        create_resp = None
        if not cid:
            create_resp = client.create_contact({k: v for k, v in payload.items() if v is not None})
            cid = id_from_response(create_resp, "id", "contact_id")
            action = "created"
        if cid and cid not in seen_contact_ids:
            contact_ids.append(cid)
            seen_contact_ids.add(cid)
        contacts_detail.append({
            "email": payload["email"],
            "contact_id": cid,
            "action": action,
            "create_response": create_resp,
        })
    write_json(run_dir / "contacts.json", {"contacts": contacts_detail, "contact_ids": contact_ids})
    manifest["build_stage"] = "contacts_created_or_deduped"
    write_mutation_artifacts(run_dir, manifest, created)
    if not contact_ids:
        manifest["build_stage"] = "no_contact_ids"
        write_mutation_artifacts(run_dir, manifest, created)
        raise RuntimeError(
            "Apollo contact search/create did not return any contact ids; refusing to mark empty campaign build complete"
        )
    if contact_ids:
        created["add_contacts"] = client.add_contact_ids(campaign_id, contact_ids, sender_id)
        manifest["build_stage"] = "contacts_enrolled"
        write_mutation_artifacts(run_dir, manifest, created)
    manifest["build_complete"] = True
    manifest["build_stage"] = "complete"
    write_mutation_artifacts(run_dir, manifest, created)
    return emit({
        "ok": True,
        "dry_run": False,
        "campaign_id": campaign_id,
        "run_dir": str(run_dir),
        "contacts": len(contact_ids),
        "manifest": str(run_dir / "manifest.json"),
    })


def message_status_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"messages_scheduled": 0, "messages_delayed": 0, "messages_sent_or_delivered": 0}
    for msg in messages:
        status = str(msg.get("status") or msg.get("state") or "").lower()
        if "sched" in status:
            counts["messages_scheduled"] += 1
        elif "delay" in status:
            counts["messages_delayed"] += 1
        elif "sent" in status or "deliver" in status:
            counts["messages_sent_or_delivered"] += 1
    return counts


def message_recipient_count(messages: list[dict[str, Any]]) -> int:
    recipients: set[str] = set()
    for message in messages:
        recipient = str(
            message.get("recipient_email")
            or message.get("to_email")
            or message.get("email")
            or ""
        ).strip().lower()
        if recipient:
            recipients.add(recipient)
    return len(recipients)


def local_contact_count(run_dir: Path) -> int | None:
    contacts_path = run_dir / "contacts.json"
    if not contacts_path.exists():
        return None
    contacts = read_json(contacts_path)
    contact_ids = contacts.get("contact_ids")
    if isinstance(contact_ids, list):
        return len([contact_id for contact_id in contact_ids if contact_id])
    contact_rows = contacts.get("contacts")
    if isinstance(contact_rows, list):
        return len([row for row in contact_rows if isinstance(row, dict) and row.get("contact_id")])
    return None


def command_activate(args: argparse.Namespace) -> int:
    manifest_path = resolve_existing_path(args.manifest, repo_root=ROOT)
    manifest = read_json(manifest_path)
    campaign_id = str(manifest.get("campaign_id") or "")
    if manifest.get("primitive") != "apollo_build_outbound" or manifest.get("source") != SOURCE:
        raise RuntimeError("manifest was not created by build_outbound.py")
    if not campaign_id or str(args.confirm_activation) != campaign_id:
        raise RuntimeError("--confirm-activation must exactly match manifest campaign_id")
    client = ApolloClient()
    approve_resp = client.approve_campaign(campaign_id)
    messages: list[dict[str, Any]] = []
    for _ in range(5):
        resp = client.search_messages(campaign_id)
        messages = list_from_response(resp, ("emailer_messages", "messages", "data"))
        if messages:
            break
        time.sleep(2)
    counts = message_status_counts(messages)
    contacts_enrolled_count = local_contact_count(manifest_path.parent)
    status = {
        "campaign_active": True,
        "campaign_id": campaign_id,
        "approve_response": approve_resp,
        "contacts_active_at_step": None,
        "contacts_active_at_step_note": "not measured by activation poll; Apollo message counts are reported separately",
        "contacts_enrolled_count": contacts_enrolled_count,
        "messages_count": len(messages),
        "message_recipient_count": message_recipient_count(messages),
        **counts,
        "recipients": [
            {
                "email": mask_email(str(
                    message.get("recipient_email")
                    or message.get("to_email")
                    or message.get("email")
                    or ""
                )),
                "status": message.get("status") or message.get("state"),
            }
            for message in messages[:100]
        ],
    }
    out = manifest_path.parent / "activation_status.json"
    write_json(out, status)
    return emit({"ok": True, "campaign_id": campaign_id, "activation_status": str(out), **counts})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build inactive Apollo outbound sequences from Sales Nav artifacts")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("resolve-sales-nav")
    r.add_argument("--query-hint")
    r.add_argument("--sales-nav-manifest", type=Path)
    r.add_argument("--state", type=Path)
    r.add_argument("--run-dir", type=Path)
    r.add_argument("--limit", type=int)
    r.set_defaults(func=command_resolve)
    pr = sub.add_parser("preview")
    pr.add_argument("--instructions", required=True)
    pr.add_argument("--sales-nav-manifest", type=Path)
    pr.add_argument("--state", type=Path)
    pr.add_argument("--query-hint")
    pr.add_argument("--limit", type=int)
    pr.add_argument("--sequence-json", type=Path)
    pr.add_argument("--out-dir", type=Path)
    pr.set_defaults(func=command_preview)
    b = sub.add_parser("build")
    b.add_argument("--instructions", required=True)
    b.add_argument("--sales-nav-manifest", type=Path)
    b.add_argument("--state", type=Path)
    b.add_argument("--query-hint")
    b.add_argument("--sequence-json", type=Path)
    b.add_argument("--allow-default-copy", action="store_true")
    b.add_argument("--limit", type=int)
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--allow-enrichment-in-dry-run", action="store_true")
    b.add_argument("--out-dir", type=Path)
    b.add_argument("--send-email-from-email-account-id")
    b.add_argument("--emailer-schedule-id")
    b.add_argument("--user-id")
    b.set_defaults(func=command_build)
    a = sub.add_parser("activate")
    a.add_argument("--manifest", type=Path, required=True)
    a.add_argument("--confirm-activation", required=True)
    a.set_defaults(func=command_activate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        return emit({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
