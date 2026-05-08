#!/usr/bin/env python3
"""Deep-research a contacts research queue via Parallel.ai. Stdlib-only.

Drop-in replacement for `aleph-mvp/data_pipeline_v2/pipelines/synthetic/research_parallel.py`,
re-implemented against the Parallel HTTP API directly so Powerpacks does not
depend on the `parallel` SDK or pydantic.

Subcommands:
    estimate   Show the queue size + per-processor cost estimate. No network.
    submit     Create a task group + submit all eligible rows; save state for poll.
    poll       Poll an existing task group + fetch results into per-handle JSON.
    status     One-shot status check on a task group.
    run        submit + poll (the common case).

Artifacts produced under --output-dir (matching aleph-mvp's layout):
    <handle>/00_parallel_raw.json       - raw Parallel output `content`
    <handle>/01_research_parallel.json  - transformed `01_research.json` shape
    _taskgroup.json                     - submission state for resumability
    _manifest.json                      - final run summary

Privacy contract:
- The primitive sends only the fields explicitly built into the input shape
  (`handle, display_name, bio, known_info, source_channel, phone_number,
   area_code`). It does not read or send message content.
- Inputs already filtered by `prepare_research_queue` and `llm_review_contacts`.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines into os.environ without overriding env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


load_dotenv(Path(__file__).resolve().parents[4] / ".env")


DEFAULT_BASE_URL = os.environ.get("POWERPACKS_PARALLEL_BASE_URL", "https://api.parallel.ai")
DEFAULT_BETA_HEADER = os.environ.get(
    "POWERPACKS_PARALLEL_BETA", "search-extract-2025-10-10"
)
DEFAULT_PROCESSOR = os.environ.get("POWERPACKS_PARALLEL_PROCESSOR", "core2x")
# Cost guardrail: Powerpacks contact research may use core/core2x/pro only.
ALLOWED_PROCESSORS = {"core", "core2x", "pro"}
PROCESSOR_PRICING_USD = {"core": 0.025, "core2x": 0.05, "pro": 0.10}
PROCESSOR_LATENCY = {
    "core": {
        "per_task": "60s-5min",
        "wall_clock": "about 1-5 min once submitted",
    },
    "core2x": {
        "per_task": "60s-10min",
        "wall_clock": "about 10-15 min once submitted",
    },
    "pro": {
        "per_task": "2-10min",
        "wall_clock": "about 2-10 min once submitted",
    },
}

DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages/research")
DEFAULT_BATCH_SIZE = 50
DEFAULT_POLL_INTERVAL = 15
DEFAULT_MAX_WAIT = 7200  # 2 hours
DEFAULT_RESULT_WORKERS = 4


# ---------------------------------------------------------------------------
# Research instructions + JSON schemas (port from aleph-mvp/research_parallel)
# ---------------------------------------------------------------------------

RESEARCH_INSTRUCTIONS = """You are a professional investigator building a comprehensive profile for a person based on their online presence.

The input contains identifying information about a person. This may include:
- A Twitter/X handle + bio (social media discovery)
- A name + email + company domain (email contact discovery)
- A name + phone number + area code + message context (phone contact discovery)
- Or any combination of the above

## Research Objectives (in priority order)

### 1. REAL NAME & LINKEDIN (most important)
Find their full legal name and LinkedIn profile URL:
- If you have an email, search for the email address across platforms
- If you have a company domain, search "{name} {domain} LinkedIn"
- If you have a phone number, search the number, area code, and the name together
- Search their name + company on LinkedIn directly
- If pseudonymous, search their handle across platforms: GitHub, Medium, Substack, LinkedIn, AngelList, Crunchbase
- If they have GitHub, check repo commit history for real names
- Read their Substack/blog posts for author bios
- Check conference speaker lists, podcast appearances

When the source is a phone contact:
- Treat the phone number, area code, messaging app context, and recency/volume clues as supporting evidence only,
  not as proof that a public-profile person with the same or similar name is the right match
- Use the phone mostly as a geography / network-context prior:
  country code, area code, and app context can help determine whether a candidate is directionally plausible,
  even when the exact phone number is not publicly attributable
- If `known_info` includes `User retarget hint`, treat that hint as the strongest clue from the user:
  - If it contains a LinkedIn URL, research that exact LinkedIn/person first and enrich that profile
  - If it names a company, title, school, location, or alternate spelling, search the person's name together with those terms
  - Prefer a candidate that matches the user hint over a generic phone/name match
  - If the hint conflicts with other signals, explain the conflict in research_notes and name_evidence
  - Do not ignore the hint unless it is impossible to reconcile with any plausible public profile
- Consider alternate spellings and transliterations of the person's name
- If several candidates are plausible, prefer the strongest professionally relevant candidate
  in startup, investor, operator, technical, academic, or other high-agency career paths
  rather than a random consumer match, provided the candidate is directionally consistent with the available signals
- You may return a best candidate even when the match is not fully proven, but in that case:
  - keep name_confidence appropriately low
  - explain clearly in research_notes and name_evidence why the match is uncertain
  - avoid fabricating a direct phone-number linkage that you do not have
- Only return null when there is no plausible high-signal candidate worth surfacing

### 2. WORK EXPERIENCE
Find ALL positions — titles, companies, approximate dates:
- Current role (from bio, LinkedIn, or company website)
- Previous roles mentioned in bio ("prev:", "ex-", "former")
- Companies they founded or co-founded
- Include company website domains when found
- For each role, try to capture a short role description when public evidence supports it.
- Good sources for role descriptions include firm bios, company team pages, official profiles,
  conference speaker pages, and reliable web results summarizing the person's responsibilities
  or practice focus.
- If a role description is available, prefer concrete scope: what they advise on, product area,
  team, function, sector focus, or transaction/investment domain.
- Do not invent role descriptions. Leave them blank/null if no trustworthy detail is available.

### 3. EDUCATION
- Schools, degrees, fields of study, graduation years
- Check for university club memberships, alumni mentions

### 4. LOCATION
- Current city/country
- Timezone clues, event attendance, office mentions

### 5. SOCIAL PROFILES
- LinkedIn URL, GitHub, Substack, personal website

Be extremely thorough. Follow every lead. Read actual page content, not just snippets.

IMPORTANT OUTPUT RULES:
- For real_name: output ONLY the name, no qualifiers like "Likely", "possibly", "unverified". If unsure, output your best guess or null.
- For location fields: output ONLY the city name, no parenthetical alternatives like "(also Bay Area)". Pick the most specific one.
- For work_experience: output ONLY the company name, not descriptions in the company field.
- For work_experience descriptions: when public evidence exists, include a concise description of the
  person's role, responsibilities, practice area, or focus. Keep it factual and source-grounded.
- For linkedin_url: it is acceptable to return the strongest candidate's LinkedIn even when confidence is limited,
  but only if research_notes makes the ambiguity explicit.
- Do NOT include reasoning or uncertainty in data fields — put that in research_notes instead."""


PERSON_RESEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {"type": "string", "description": "Identifier — Twitter handle, email address, or slug"},
        "display_name": {"type": "string", "description": "Display name / full name"},
        "bio": {"type": "string", "description": "Bio text, job title, or role description"},
        "known_info": {"type": "string", "description": "Any additional known information"},
        "source_channel": {"type": ["string", "null"], "description": "twitter | email | phone"},
        "phone_number": {"type": ["string", "null"], "description": "E.164 phone number if available"},
        "area_code": {"type": ["string", "null"], "description": "Area code if available"},
    },
    "required": ["handle", "display_name", "bio", "known_info"],
}

PERSON_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "real_name": {"type": ["string", "null"], "description": "Full legal name"},
        "name_confidence": {"type": "number", "description": "0.0-1.0"},
        "name_evidence": {"type": "string", "description": "How the real name was discovered"},
        "work_experience": {"type": "string", "description": "JSON array of work positions"},
        "education": {"type": "string", "description": "JSON array of education entries"},
        "location_city": {"type": ["string", "null"]},
        "location_country": {"type": ["string", "null"]},
        "linkedin_url": {"type": ["string", "null"]},
        "github_url": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "research_notes": {"type": "string"},
    },
    "required": [
        "name_confidence", "name_evidence", "work_experience", "education",
        "summary", "research_notes",
    ],
}


# ---------------------------------------------------------------------------
# JSON / IO helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), flush=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Parallel HTTP wrapper
# ---------------------------------------------------------------------------

class ParallelClient:
    """Minimal stdlib client for the Parallel.ai task-group API."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, beta_header: str = DEFAULT_BETA_HEADER):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.beta_header = beta_header

    def _request(
        self, method: str, path: str, *,
        body: Any = None, query: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> tuple[int, Any, str]:
        url = self.base_url + path
        if query:
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(cleaned)
        data: bytes | None = None
        headers = {
            "x-api-key": self.api_key,
            "parallel-beta": self.beta_header,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw.decode("utf-8")) if raw else None, ""
                except (UnicodeDecodeError, json.JSONDecodeError):
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

    # ---- task group + run lifecycle --------------------------------------

    def create_group(self, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        status, body, raw = self._request("POST", "/v1beta/tasks/groups",
                                          body={"metadata": metadata or {}})
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"create_group failed (HTTP {status}): {raw[:200]}")
        return body

    def add_runs(self, group_id: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        status, body, raw = self._request(
            "POST", f"/v1beta/tasks/groups/{group_id}/runs",
            body={"inputs": inputs},
        )
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"add_runs failed (HTTP {status}): {raw[:200]}")
        return body

    def get_group(self, group_id: str) -> dict[str, Any]:
        status, body, raw = self._request("GET", f"/v1beta/tasks/groups/{group_id}")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_group failed (HTTP {status}): {raw[:200]}")
        return body

    def get_run_result(self, run_id: str, *, api_timeout: int = 60) -> dict[str, Any] | None:
        status, body, raw = self._request(
            "GET", f"/v1/tasks/runs/{run_id}/result",
            query={"beta": "true", "api_timeout": api_timeout},
            timeout=api_timeout + 10,
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_run_result failed for {run_id} (HTTP {status}): {raw[:200]}")
        return body


# ---------------------------------------------------------------------------
# Input shaping
# ---------------------------------------------------------------------------

def build_known_info(row: dict[str, str]) -> str:
    parts: list[str] = []
    for key, label in (
        ("primary_email", "Email"),
        ("all_emails", "All emails"),
        ("domain", "Company domain"),
        ("website_url", "Website"),
    ):
        value = (row.get(key) or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    if (row.get("follower_count") or "").strip():
        parts.append(f"Followers: {row['follower_count']}")
    if (row.get("whale_names") or "").strip():
        parts.append(f"Followed by: {row['whale_names']}")
    if (row.get("moe_top_reasoning") or "").strip():
        parts.append(f"Profile assessment: {row['moe_top_reasoning'][:200]}")
    if (row.get("retarget_hint") or "").strip():
        parts.append(f"User retarget hint: {row['retarget_hint']}")
    if (row.get("total_messages") or "").strip():
        parts.append(f"Message count: {row['total_messages']}")
    if (row.get("phone_e164") or "").strip():
        parts.append(f"Phone: {row['phone_e164']}")
    if (row.get("area_code") or "").strip():
        parts.append(f"Area code: {row['area_code']}")
    if (row.get("message_source") or "").strip():
        parts.append(f"Message source: {row['message_source']}")
    if (row.get("last_message") or "").strip():
        parts.append(f"Last message timestamp: {row['last_message']}")
    if (row.get("is_in_group_chats") or "").strip():
        parts.append(f"In group chats: {row['is_in_group_chats']}")
    if (row.get("group_names") or "").strip():
        parts.append(f"Group names: {row['group_names']}")
    return "\n".join(parts)


def candidate_handle(row: dict[str, str]) -> str:
    handle = (row.get("handle") or "").strip()
    if handle:
        return handle
    if row.get("primary_email"):
        return row["primary_email"].split("@")[0].lower().replace(".", "_")
    if row.get("phone_e164"):
        digits = re.sub(r"\D", "", str(row["phone_e164"]))
        if digits:
            return f"phone-{digits[-10:]}"
    name = (row.get("display_name") or row.get("first_name") or "").strip().lower()
    if row.get("last_name") and not row.get("display_name"):
        name = f"{name} {row['last_name']}".strip()
    if name:
        return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "unknown"
    return "unknown"


def build_input(row: dict[str, str], handle: str) -> dict[str, Any]:
    name = (row.get("display_name") or "").strip()
    if not name and row.get("first_name"):
        name = row["first_name"]
        if row.get("last_name"):
            name = f"{name} {row['last_name']}".strip()
    return {
        "handle": handle,
        "display_name": name or handle,
        "bio": (row.get("bio") or "").strip(),
        "known_info": build_known_info(row),
        "source_channel": (row.get("source_channel") or "phone") or None,
        "phone_number": (row.get("phone_e164") or None) or None,
        "area_code": (row.get("area_code") or None) or None,
    }


def task_spec() -> dict[str, Any]:
    return {
        "instructions": RESEARCH_INSTRUCTIONS,
        "input_schema": {"json_schema": PERSON_RESEARCH_INPUT_SCHEMA},
        "output_schema": {"json_schema": PERSON_RESEARCH_OUTPUT_SCHEMA},
    }


# ---------------------------------------------------------------------------
# 01_research_parallel.json transform (port from aleph-mvp)
# ---------------------------------------------------------------------------

def parallel_to_research_json(
    result: dict[str, Any], row: dict[str, str], handle: str, name: str, bio: str, *,
    research_method: str = "parallel-core2x",
) -> dict[str, Any]:
    real_name = (result.get("real_name") or name) or handle
    source_channel = (row.get("source_channel") or "phone").strip().lower()
    name_parts = real_name.split(" ", 1) if real_name else [name, ""]
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    positions: list[dict[str, Any]] = []
    try:
        we_raw = json.loads(result.get("work_experience", "[]") or "[]")
        for pos in we_raw:
            if isinstance(pos, dict):
                positions.append({
                    "title": pos.get("title") or pos.get("position", "") or "",
                    "company_name": (
                        pos.get("company")
                        or pos.get("organization")
                        or pos.get("employer")
                        or pos.get("company_name")
                        or pos.get("name", "")
                        or ""
                    ),
                    "company_domain": pos.get("domain") or pos.get("company_domain"),
                    "company_linkedin_url": None,
                    "description": pos.get("description"),
                    "start_date": pos.get("start_date"),
                    "end_date": pos.get("end_date"),
                    "is_current": pos.get("current") or pos.get("is_current", False),
                    "confidence": pos.get("confidence", 0.7),
                    "sources": pos.get("evidence", []) if isinstance(pos.get("evidence"), list) else (
                        [pos.get("source", "")] if pos.get("source") else []
                    ),
                })
            elif isinstance(pos, str):
                positions.append({
                    "title": "",
                    "company_name": pos,
                    "company_domain": None,
                    "company_linkedin_url": None,
                    "description": None,
                    "start_date": None,
                    "end_date": None,
                    "is_current": False,
                    "confidence": 0.5,
                    "sources": [],
                })
    except (json.JSONDecodeError, TypeError):
        pass

    education: list[dict[str, Any]] = []
    try:
        ed_raw = json.loads(result.get("education", "[]") or "[]")
        for edu in ed_raw:
            if isinstance(edu, dict):
                education.append({
                    "school_name": (
                        edu.get("school")
                        or edu.get("school_name")
                        or edu.get("institution")
                        or edu.get("university")
                        or edu.get("name", "")
                        or ""
                    ),
                    "degree": edu.get("degree"),
                    "field_of_study": edu.get("field") or edu.get("field_of_study"),
                    "start_year": edu.get("start_year"),
                    "end_year": edu.get("end_year"),
                    "confidence": edu.get("confidence", 0.7),
                    "source": str(edu.get("evidence", "")) if edu.get("evidence") else "",
                })
            elif isinstance(edu, str):
                education.append({
                    "school_name": edu, "degree": None, "field_of_study": None,
                    "start_year": None, "end_year": None,
                    "confidence": 0.5, "source": "",
                })
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "research_id": f"{handle}-{date.today().isoformat()}",
        "query": f"@{handle} ({name}): {bio[:100]}",
        "status": "draft",
        "research_method": research_method,
        "person": {
            "full_name": real_name,
            "first_name": first_name,
            "last_name": last_name,
            "also_known_as": [handle, name] if real_name != name else [handle],
            "confidence": result.get("name_confidence", 0.3),
            "sources": [],
            "notes": result.get("name_evidence", ""),
        },
        "location": {
            "city": result.get("location_city") or "",
            "state": "",
            "country": result.get("location_country") or "",
            "raw": "",
            "confidence": 0.5 if (result.get("location_city") or result.get("location_country")) else 0.0,
            "source": "",
        },
        "headline": {
            "text": bio[:200] if bio else "",
            "confidence": 0.95 if bio else 0.0,
            "source": f"https://x.com/{handle}",
        },
        "summary": {
            "text": result.get("summary", "") or "",
            "confidence": 0.7,
            "source": "Parallel Deep Research",
        },
        "positions": positions,
        "education": education,
        "social": {
            "twitter_handle": handle if source_channel == "twitter" else None,
            "linkedin_url": result.get("linkedin_url"),
            "linkedin_status": "found" if result.get("linkedin_url") else "not_found",
            "github_url": result.get("github_url"),
            "personal_website": result.get("personal_website"),
            "primary_email": row.get("primary_email") if source_channel == "email" else None,
            "primary_phone": row.get("phone_e164") if source_channel == "phone" else None,
        },
        "metadata": {
            "total_sources_consulted": 0,
            "estimated_completeness": _estimate_completeness(result),
            "gaps": _identify_gaps(result),
            "research_date": date.today().isoformat(),
            "research_method": research_method,
            "research_notes": result.get("research_notes", "") or "",
            "source_channel": source_channel or "unknown",
            "source_identifier": row.get("primary_email") or row.get("phone_e164") or handle,
        },
    }


def _estimate_completeness(result: dict[str, Any]) -> float:
    score = 0.0
    if result.get("real_name"):
        score += 0.3
    try:
        score += min(0.3, len(json.loads(result.get("work_experience", "[]") or "[]")) * 0.1)
    except Exception:
        pass
    try:
        score += min(0.2, len(json.loads(result.get("education", "[]") or "[]")) * 0.1)
    except Exception:
        pass
    if result.get("location_city"):
        score += 0.1
    if result.get("linkedin_url"):
        score += 0.1
    return round(min(1.0, score), 2)


def _identify_gaps(result: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    if not result.get("real_name"):
        gaps.append("Real name not identified")
    if not result.get("work_experience") or result.get("work_experience") == "[]":
        gaps.append("No work experience found")
    if not result.get("education") or result.get("education") == "[]":
        gaps.append("No education found")
    if not result.get("location_city") and not result.get("location_country"):
        gaps.append("Location unknown")
    if not result.get("linkedin_url"):
        gaps.append("No LinkedIn profile found")
    return gaps


# ---------------------------------------------------------------------------
# CSV reading + filtering
# ---------------------------------------------------------------------------

def load_queue(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"input CSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def filter_already_done(rows: list[dict[str, str]], output_dir: Path) -> tuple[list[dict[str, str]], int]:
    todo: list[dict[str, str]] = []
    skipped = 0
    seen: set[str] = set()
    for row in rows:
        handle = candidate_handle(row)
        if handle in seen:
            continue
        seen.add(handle)
        if (output_dir / handle / "01_research_parallel.json").exists():
            skipped += 1
            continue
        copy = dict(row)
        copy["handle"] = handle
        todo.append(copy)
    return todo, skipped


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _validate_processor(processor: str) -> str:
    if processor not in ALLOWED_PROCESSORS:
        allowed = ", ".join(sorted(ALLOWED_PROCESSORS))
        raise SystemExit(
            f"processor '{processor}' is blocked for Powerpacks contact research; "
            f"allowed processor: {allowed}"
        )
    return processor


def estimate_latency(processor: str, count: int) -> dict[str, Any]:
    latency = PROCESSOR_LATENCY[processor]
    if count <= 0:
        rough = "no paid Parallel work"
    else:
        rough = latency["wall_clock"]
        if count > DEFAULT_BATCH_SIZE:
            rough += "; larger queues can take longer depending on Parallel capacity"
    return {
        "processor": processor,
        "per_task": latency["per_task"],
        "rough_wall_clock": rough,
        "basis": "Parallel Task API processor docs; task-group runs are submitted together, so this is not multiplied per contact.",
    }


def _resolve_api_key(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    env = os.environ.get("PARALLEL_API_KEY")
    if env:
        return env
    raise SystemExit("PARALLEL_API_KEY not set (pass --api-key or add it to the repo .env)")


def _persisted_state_path(output_dir: Path) -> Path:
    return output_dir / "_taskgroup.json"


def cmd_estimate(args: argparse.Namespace) -> int:
    processor = _validate_processor(args.processor)
    rows = load_queue(Path(args.input))
    output_dir = Path(args.output_dir)
    todo, skipped_done = filter_already_done(rows, output_dir)
    if args.limit is not None:
        todo = todo[: args.limit]
    cost_per = PROCESSOR_PRICING_USD[processor]
    emit({
        "primitive": "deep_research_contacts",
        "command": "estimate",
        "input": str(args.input),
        "output_dir": str(output_dir),
        "queue_rows": len(rows),
        "skipped_already_done": skipped_done,
        "would_submit": len(todo),
        "processor": processor,
        "estimated_usd": round(len(todo) * cost_per, 4),
        "estimated_latency": estimate_latency(processor, len(todo)),
    })
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    api_key = _resolve_api_key(args.api_key)
    client = ParallelClient(api_key, args.base_url, args.beta_header)
    group_id = args.taskgroup_id or _load_group_id(Path(args.output_dir))
    if not group_id:
        emit({"primitive": "deep_research_contacts", "command": "status",
              "status": "failed", "error": "no taskgroup id"})
        return 1
    payload = client.get_group(group_id)
    emit({
        "primitive": "deep_research_contacts",
        "command": "status",
        "taskgroup_id": group_id,
        "group": payload,
    })
    return 0 if payload.get("status", {}).get("is_active") is False else 1


def _load_group_id(output_dir: Path) -> str | None:
    state = _persisted_state_path(output_dir)
    if not state.exists():
        return None
    try:
        return read_json(state).get("taskgroup_id")
    except (json.JSONDecodeError, OSError):
        return None


def cmd_submit(args: argparse.Namespace) -> int:
    processor = _validate_processor(args.processor)
    api_key = _resolve_api_key(args.api_key)
    rows = load_queue(Path(args.input))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    todo, skipped_done = filter_already_done(rows, output_dir)
    if args.limit is not None:
        todo = todo[: args.limit]

    if not todo:
        emit({
            "primitive": "deep_research_contacts",
            "command": "submit",
            "status": "no_work",
            "queue_rows": len(rows),
            "skipped_already_done": skipped_done,
        })
        return 0

    client = ParallelClient(api_key, args.base_url, args.beta_header)
    group = client.create_group(metadata={"source": "powerpacks", "submitted_at": now_iso()})
    group_id = group.get("taskgroup_id") or group.get("id")
    if not group_id:
        emit({"primitive": "deep_research_contacts", "command": "submit",
              "status": "failed", "error": "no taskgroup_id in response", "raw": group})
        return 1

    spec = task_spec()
    inputs = [
        {
            "task_spec": spec,
            "input": build_input(row, row["handle"]),
            "metadata": {"handle": row["handle"], "processor": processor},
            "processor": processor,
        }
        for row in todo
    ]

    all_run_ids: list[str] = []
    for i in range(0, len(inputs), args.batch_size):
        batch = inputs[i:i + args.batch_size]
        run_resp = client.add_runs(group_id, batch)
        run_ids = run_resp.get("run_ids") or []
        all_run_ids.extend(run_ids)

    state = {
        "taskgroup_id": group_id,
        "processor": processor,
        "submitted_at": now_iso(),
        "input_csv": str(args.input),
        "rows_submitted": len(todo),
        "skipped_already_done": skipped_done,
        "run_ids": all_run_ids,
        "handles": [row["handle"] for row in todo],
        "rows": todo,  # keep for poll-time CSV row lookup
    }
    write_json(_persisted_state_path(output_dir), state)

    cost_per = PROCESSOR_PRICING_USD[processor]
    emit({
        "primitive": "deep_research_contacts",
        "command": "submit",
        "status": "submitted",
        "taskgroup_id": group_id,
        "processor": processor,
        "submitted": len(all_run_ids),
        "skipped_already_done": skipped_done,
        "estimated_usd": round(len(all_run_ids) * cost_per, 4),
        "state_path": str(_persisted_state_path(output_dir)),
    })
    return 0


def _wait_for_group(client: ParallelClient, group_id: str, *, poll_interval: int, max_wait: int) -> dict[str, Any]:
    deadline = time.time() + max_wait
    last: dict[str, Any] = {}
    while time.time() < deadline:
        payload = client.get_group(group_id)
        last = payload
        status = payload.get("status") or {}
        counts = status.get("task_run_status_counts") or {}
        if counts:
            print(f"[deep_research_contacts] poll status {counts}", file=sys.stderr, flush=True)
        if status.get("is_active") is False:
            return payload
        time.sleep(poll_interval)
    return last


def cmd_poll(args: argparse.Namespace) -> int:
    api_key = _resolve_api_key(args.api_key)
    output_dir = Path(args.output_dir)
    state_path = _persisted_state_path(output_dir)
    if not state_path.exists() and not args.taskgroup_id:
        emit({"primitive": "deep_research_contacts", "command": "poll",
              "status": "failed", "error": f"no state file at {state_path} and no --taskgroup-id"})
        return 1

    state = read_json(state_path) if state_path.exists() else {}
    group_id = args.taskgroup_id or state.get("taskgroup_id")
    if not group_id:
        emit({"primitive": "deep_research_contacts", "command": "poll",
              "status": "failed", "error": "no taskgroup_id"})
        return 1
    run_ids: list[str] = state.get("run_ids") or []
    rows: list[dict[str, str]] = state.get("rows") or []
    rows_by_handle = {row.get("handle"): row for row in rows if row.get("handle")}
    processor = state.get("processor") or DEFAULT_PROCESSOR
    research_method = f"parallel-{processor}"

    client = ParallelClient(api_key, args.base_url, args.beta_header)

    print(f"[deep_research_contacts] polling group {group_id}", file=sys.stderr)
    final_group = _wait_for_group(
        client, group_id,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
    )
    print(f"[deep_research_contacts] group complete, fetching {len(run_ids)} run results", file=sys.stderr)

    # Fetch each run's result. Use a small thread pool — Parallel rate limits
    # are generous, but 4 concurrent gets is fine.
    results_by_handle: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    def fetch_one(run_id: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            payload = client.get_run_result(run_id, api_timeout=args.api_timeout)
        except Exception as exc:
            return run_id, None, f"{type(exc).__name__}: {exc}"
        return run_id, payload, None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, rid): rid for rid in run_ids}
        for fut in as_completed(futures):
            run_id, payload, err = fut.result()
            if err:
                errors.append({"run_id": run_id, "error": err})
                continue
            if not payload:
                errors.append({"run_id": run_id, "error": "no payload (404)"})
                continue
            output = payload.get("output") or {}
            content = output.get("content")
            result = content if isinstance(content, dict) else {"raw": str(content)}
            run_meta = payload.get("run") or {}
            metadata = run_meta.get("metadata") or {}
            handle = metadata.get("handle")
            if not handle:
                inp = payload.get("input") or {}
                inner = inp.get("input") if isinstance(inp, dict) else None
                if isinstance(inner, dict):
                    handle = inner.get("handle")
            handle = handle or run_id
            results_by_handle[handle] = result

    # Write artifacts
    found_name = 0
    found_linkedin = 0
    for handle, result in results_by_handle.items():
        person_dir = output_dir / handle
        person_dir.mkdir(parents=True, exist_ok=True)
        write_json(person_dir / "00_parallel_raw.json", result)

        row = rows_by_handle.get(handle, {"handle": handle, "source_channel": "phone"})
        name = row.get("display_name") or handle
        bio = row.get("bio") or ""
        research = parallel_to_research_json(result, row, handle, name, bio, research_method=research_method)
        write_json(person_dir / "01_research_parallel.json", research)

        if result.get("real_name"):
            found_name += 1
        if result.get("linkedin_url"):
            found_linkedin += 1

    summary = {
        "primitive": "deep_research_contacts",
        "command": "poll",
        "status": "completed" if not errors else "completed_with_errors",
        "taskgroup_id": group_id,
        "completed_at": now_iso(),
        "output_dir": str(output_dir),
        "counts": {
            "run_ids": len(run_ids),
            "results_fetched": len(results_by_handle),
            "errors": len(errors),
            "real_name_found": found_name,
            "linkedin_found": found_linkedin,
        },
        "group_status": (final_group or {}).get("status"),
        "errors": errors,
    }
    write_json(output_dir / "_manifest.json", summary)
    emit(summary)
    return 0 if not errors else 2


def cmd_run(args: argparse.Namespace) -> int:
    rc = cmd_submit(args)
    if rc != 0:
        return rc
    # cmd_submit already wrote the state; cmd_poll picks it up.
    return cmd_poll(args)


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key", help="Parallel.ai API key (defaults to PARALLEL_API_KEY from env or repo .env)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--beta-header", default=DEFAULT_BETA_HEADER)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), type=Path)


def add_submit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, help="research_queue.csv (from prepare_research_queue)")
    parser.add_argument("--processor", default=DEFAULT_PROCESSOR,
                        choices=sorted(ALLOWED_PROCESSORS))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, help="Cap rows submitted (after dedup)")


def add_poll_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--taskgroup-id", help="Override the persisted task group id")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT)
    parser.add_argument("--workers", type=int, default=DEFAULT_RESULT_WORKERS)
    parser.add_argument("--api-timeout", type=int, default=60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep-research a contacts queue via Parallel.ai")
    sub = parser.add_subparsers(dest="command", required=True)

    estimate = sub.add_parser("estimate", help="Cost estimate without API calls")
    estimate.add_argument("--input", required=True)
    estimate.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), type=Path)
    estimate.add_argument("--processor", default=DEFAULT_PROCESSOR, choices=sorted(ALLOWED_PROCESSORS))
    estimate.add_argument("--limit", type=int)
    estimate.set_defaults(func=cmd_estimate)

    status = sub.add_parser("status", help="One-shot status check on a task group")
    add_common_args(status)
    status.add_argument("--taskgroup-id")
    status.set_defaults(func=cmd_status)

    submit = sub.add_parser("submit", help="Create a task group + add runs from a research queue")
    add_common_args(submit)
    add_submit_args(submit)
    submit.set_defaults(func=cmd_submit)

    poll = sub.add_parser("poll", help="Poll an existing task group + write per-handle JSON artifacts")
    add_common_args(poll)
    add_poll_args(poll)
    poll.set_defaults(func=cmd_poll)

    run = sub.add_parser("run", help="submit + poll in one go")
    add_common_args(run)
    add_submit_args(run)
    add_poll_args(run)
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
