#!/usr/bin/env python3
"""Resumable local LinkedIn network import orchestrator.

Ports the LinkedIn Connections.csv -> provider enrichment -> people_harmonic_all
shape into Powerpacks. Stdlib-only. All artifacts stay under .powerpacks/.
Paid external APIs are approval-gated.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS, normalize_people_row
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS, normalize_people_row

DEFAULT_LEDGER = Path(".powerpacks/network-import/linkedin/import-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
HARMONIC_BASE_URL = "https://api.harmonic.ai"
RAPIDAPI_BASE_URL = "https://professional-network-data.p.rapidapi.com"

CONNECTION_COLUMNS = [
    "person_id",
    "public_identifier",
    "linkedin_url",
    "first_name",
    "last_name",
    "source_user",
    "linkedin_company",
    "linkedin_position",
    "linkedin_email",
    "connected_on",
]
PROVIDER_COLUMNS = CONNECTION_COLUMNS + [
    "harmonic_status_code",
    "harmonic_error",
    "harmonic_response",
    "rapidapi_status_code",
    "rapidapi_error",
    "rapidapi_response",
    "enriched_at",
]
PIPELINE_STEPS = ["convert", "enrich_providers", "merge_people"]


class PipelineBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or "blocked")
        self.payload = payload
        self.code = code


class PipelineFailed(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def generate_person_id(public_identifier: str) -> str:
    """Deterministic UUID-ish ID compatible in spirit with legacy generated IDs.

    Uses UUIDv5-like stable hashing without importing uuid namespace constants from
    the old repo. The actual output is stable for Powerpacks artifacts.
    """
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"linkedin:{public_identifier.lower().strip()}"))


def extract_public_identifier(linkedin_url: str) -> str:
    if not linkedin_url:
        return ""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url, re.IGNORECASE)
    if not match:
        return ""
    return urllib.parse.unquote(match.group(1).strip().rstrip("/")).lower()


def normalize_linkedin_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith("linkedin.com/"):
        url = "https://www." + url
    elif url.startswith("www.linkedin.com/"):
        url = "https://" + url
    return url.split("?")[0].rstrip("/")


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def count_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return len(parsed) if isinstance(parsed, list) else 0
        except json.JSONDecodeError:
            return 0
    return 0


def profile_richness(experiences: list[Any], education: list[Any]) -> int:
    return len(experiences or []) + len(education or [])


def current_position(experiences: list[dict[str, Any]]) -> tuple[str, str, str]:
    for exp in experiences or []:
        if exp.get("is_current_position") or exp.get("is_current") or not (exp.get("ends_at") or exp.get("end_date")):
            return (
                str(exp.get("title") or ""),
                str(exp.get("company_name") or exp.get("company") or ""),
                str(exp.get("company") if exp.get("company_name") else exp.get("company_urn") or ""),
            )
    if experiences:
        exp = experiences[0]
        return (str(exp.get("title") or ""), str(exp.get("company_name") or exp.get("company") or ""), str(exp.get("company_urn") or ""))
    return "", "", ""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


@dataclass
class LinkedInConnection:
    first_name: str
    last_name: str
    linkedin_url: str
    email_address: str
    company: str
    position: str
    connected_on: str
    public_identifier: str
    person_id: str
    source_user: str

    def row(self) -> dict[str, str]:
        return {
            "person_id": self.person_id,
            "public_identifier": self.public_identifier,
            "linkedin_url": self.linkedin_url,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "source_user": self.source_user,
            "linkedin_company": self.company,
            "linkedin_position": self.position,
            "linkedin_email": self.email_address,
            "connected_on": self.connected_on,
        }


def parse_connections_csv(path: Path, source_user: str, limit: int | None = None) -> tuple[list[LinkedInConnection], dict[str, Any]]:
    if not path.exists():
        raise PipelineFailed(f"LinkedIn Connections CSV not found: {path}")
    connections: list[LinkedInConnection] = []
    seen: set[str] = set()
    duplicates = 0
    skipped = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        header_line = ""
        for line in handle:
            if line.strip().startswith("First Name,"):
                header_line = line.strip()
                break
        if not header_line:
            raise PipelineFailed("Could not find LinkedIn export header row starting with 'First Name,'")
        reader = csv.DictReader(handle, fieldnames=header_line.split(","))
        for row in reader:
            url = normalize_linkedin_url(row.get("URL", ""))
            pub_id = extract_public_identifier(url)
            if not pub_id:
                skipped += 1
                continue
            if pub_id in seen:
                duplicates += 1
                continue
            seen.add(pub_id)
            connections.append(
                LinkedInConnection(
                    first_name=(row.get("First Name") or "").strip(),
                    last_name=(row.get("Last Name") or "").strip(),
                    linkedin_url=url,
                    email_address=(row.get("Email Address") or "").strip(),
                    company=(row.get("Company") or "").strip(),
                    position=(row.get("Position") or "").strip(),
                    connected_on=(row.get("Connected On") or "").strip(),
                    public_identifier=pub_id,
                    person_id=generate_person_id(pub_id),
                    source_user=source_user,
                )
            )
            if limit and len(connections) >= limit:
                break
    return connections, {"parsed": len(connections), "duplicates": duplicates, "skipped_invalid": skipped}


def http_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any] | None, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else None, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return exc.code, data, raw[:1000]
    except Exception as exc:
        return 0, None, str(exc)


def harmonic_enrich(linkedin_url: str, api_key: str) -> dict[str, Any]:
    status, data, error = http_json(
        "POST",
        f"{HARMONIC_BASE_URL}/persons",
        headers={"accept": "application/json", "apikey": api_key},
        params={"linkedin_url": linkedin_url},
        timeout=90,
    )
    enrichment_id = ""
    if isinstance(data, dict):
        enrichment_id = str(data.get("enrichment_id") or data.get("enrichment_urn") or "")
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        enrichment_id = enrichment_id or str(detail.get("enrichment_urn") or "")
    return {"status_code": status, "data": data, "error": error, "enrichment_id": enrichment_id}


def rapidapi_profile(public_identifier: str, linkedin_url: str, api_key: str) -> dict[str, Any]:
    status, data, error = http_json(
        "GET",
        f"{RAPIDAPI_BASE_URL}/get-profile-data-by-url",
        headers={"x-rapidapi-host": "professional-network-data.p.rapidapi.com", "x-rapidapi-key": api_key},
        params={"url": linkedin_url or f"https://www.linkedin.com/in/{public_identifier}"},
        timeout=90,
    )
    return {"status_code": status, "data": data, "error": error}


def normalize_rapidapi(data: dict[str, Any] | None, public_identifier: str, linkedin_url: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    profile = data.get("data") if isinstance(data.get("data"), dict) else data
    # Common RapidAPI variants: top-level normalized, LinkedIn-ish camelCase, or nested data.
    full_name = profile.get("full_name") or profile.get("fullName") or profile.get("name") or ""
    first = profile.get("first_name") or profile.get("firstName") or ""
    last = profile.get("last_name") or profile.get("lastName") or ""
    if full_name and (not first or not last):
        sf, sl = split_name(full_name)
        first = first or sf
        last = last or sl
    experiences = profile.get("experiences") or profile.get("experience") or profile.get("positions") or []
    education = profile.get("education") or profile.get("educations") or []
    location = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    location_str = profile.get("location_str") or profile.get("location") if isinstance(profile.get("location"), str) else ""
    title, company, company_urn = current_position(experiences if isinstance(experiences, list) else [])
    return {
        "public_identifier": profile.get("public_identifier") or profile.get("username") or public_identifier,
        "linkedin_url": profile.get("linkedin_url") or profile.get("profile_url") or profile.get("profileURL") or linkedin_url,
        "first_name": first or "",
        "last_name": last or "",
        "full_name": full_name or f"{first} {last}".strip(),
        "headline": profile.get("headline") or "",
        "summary": profile.get("summary") or profile.get("about") or "",
        "city": profile.get("city") or location.get("city", ""),
        "state": profile.get("state") or location.get("state", ""),
        "country": profile.get("country") or location.get("country", ""),
        "location_raw": location_str or location.get("location", ""),
        "profile_picture_url": profile.get("profile_pic_url") or profile.get("profilePicture") or profile.get("profile_picture_url") or "",
        "work_experiences": experiences if isinstance(experiences, list) else [],
        "education": education if isinstance(education, list) else [],
        "current_title": title,
        "current_company": company,
        "current_company_urn": company_urn,
        "entity_urn": "",
    }


def normalize_harmonic(data: dict[str, Any] | None, public_identifier: str, linkedin_url: str) -> dict[str, Any]:
    if not isinstance(data, dict) or "full_name" not in data:
        return {}
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    socials = data.get("socials") if isinstance(data.get("socials"), dict) else {}
    linkedin = socials.get("LINKEDIN") if isinstance(socials.get("LINKEDIN"), dict) else {}
    experiences = data.get("experience") if isinstance(data.get("experience"), list) else []
    education = data.get("education") if isinstance(data.get("education"), list) else []
    title, company, company_urn = current_position(experiences)
    return {
        "public_identifier": public_identifier,
        "linkedin_url": linkedin.get("url") or data.get("linkedin_url") or linkedin_url,
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "full_name": data.get("full_name", ""),
        "headline": data.get("linkedin_headline", ""),
        "summary": data.get("summary", ""),
        "city": location.get("city", ""),
        "state": location.get("state", ""),
        "country": location.get("country", ""),
        "location_raw": location.get("location", ""),
        "profile_picture_url": data.get("profile_picture_url", ""),
        "work_experiences": experiences,
        "education": education,
        "current_title": title,
        "current_company": company,
        "current_company_urn": company_urn,
        "entity_urn": data.get("entity_urn", ""),
        "harmonic_location": json.dumps(location) if location else "",
    }


def merge_provider_profile(base: dict[str, Any], harmonic: dict[str, Any], rapid: dict[str, Any], harmonic_raw: dict[str, Any] | None, rapid_raw: dict[str, Any] | None) -> dict[str, Any]:
    h_score = profile_richness(harmonic.get("work_experiences", []), harmonic.get("education", []))
    r_score = profile_richness(rapid.get("work_experiences", []), rapid.get("education", []))
    rich = rapid if r_score > h_score else harmonic
    fallback = harmonic if rich is rapid else rapid
    provider = "rapidapi" if rich is rapid else "harmonic"
    if harmonic and rapid:
        provider = f"{provider}_preferred_harmonic_rapidapi"

    pub_id = base["public_identifier"]
    row: dict[str, Any] = {col: "" for col in PEOPLE_COLUMNS}
    row.update({
        "id": generate_person_id(pub_id),
        "public_identifier": pub_id,
        "linkedin_url": base.get("linkedin_url", ""),
        "first_name": base.get("first_name", ""),
        "last_name": base.get("last_name", ""),
        "enriched_at": now_iso(),
        "enrichment_provider": provider if (harmonic or rapid) else "linkedin_csv_only",
    })
    for key in [
        "linkedin_url", "first_name", "last_name", "full_name", "headline", "summary",
        "city", "state", "country", "location_raw", "profile_picture_url",
        "current_title", "current_company", "current_company_urn", "entity_urn", "harmonic_location",
    ]:
        row[key] = rich.get(key) or fallback.get(key) or row.get(key, "")
    if not row["full_name"]:
        row["full_name"] = f"{row['first_name']} {row['last_name']}".strip()
    row["work_experiences"] = json.dumps(rich.get("work_experiences") or fallback.get("work_experiences") or [])
    row["education"] = json.dumps(rich.get("education") or fallback.get("education") or [])
    row["harmonic_response"] = json.dumps(harmonic_raw) if harmonic_raw else ""
    row["rapidapi_response"] = json.dumps(rapid_raw) if rapid_raw else ""
    return row


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "linkedin_network_import")
    ledger.setdefault("version", 1)
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
    rec = ledger.setdefault("steps", {}).setdefault(step_id, {"id": step_id})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked_approval", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def next_pending_step(ledger: dict[str, Any]) -> str | None:
    for step_id in PIPELINE_STEPS:
        if ledger.setdefault("steps", {}).get(step_id, {}).get("status") != "completed":
            return step_id
    return None


def approval_id(ledger: dict[str, Any], step_id: str) -> str:
    return f"{ledger.get('run_id', 'run')}:{step_id}"


def is_approved(ledger: dict[str, Any], step_id: str) -> bool:
    return bool(ledger.setdefault("approvals", {}).get(approval_id(ledger, step_id)))


def block_for_approval(ledger_path: Path, ledger: dict[str, Any], step_id: str, message: str) -> None:
    app_id = approval_id(ledger, step_id)
    ledger["blocked"] = {"step_id": step_id, "approval_id": app_id, "approval_type": "external_api_spend"}
    mark_step(ledger, step_id, "blocked_approval", approval_id=app_id, approval_type="external_api_spend")
    save_ledger(ledger_path, ledger)
    raise PipelineBlocked({
        "status": "blocked_approval",
        "step_id": step_id,
        "approval_id": app_id,
        "approval_type": "external_api_spend",
        "message": message,
        "ledger": str(ledger_path),
        "continue_command": f"uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py approve --ledger {ledger_path} && uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py continue --ledger {ledger_path}",
    })


def step_convert(ledger: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(ledger["run_dir"])
    inp = Path(ledger["input"]["csv"])
    connections, stats = parse_connections_csv(inp, ledger["input"]["source_user"], ledger["input"].get("limit"))
    rows = [conn.row() for conn in connections]
    out = run_dir / "connections_for_enrichment.csv"
    write_csv(out, CONNECTION_COLUMNS, rows)
    ledger["artifacts"]["connections_csv"] = str(out)
    return {**stats, "output_file": str(out)}


def step_enrich_providers(ledger: dict[str, Any]) -> dict[str, Any]:
    connections_path = Path(ledger["artifacts"]["connections_csv"])
    rows = read_csv(connections_path)
    limit = ledger["input"].get("limit")
    if limit:
        rows = rows[: int(limit)]
    harmonic_key = os.getenv("HARMONIC_API_KEY", "").strip()
    rapid_key = os.getenv("RAPIDAPI_KEY", "").strip() or os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()
    use_harmonic = ledger["input"].get("use_harmonic", True)
    use_rapidapi = ledger["input"].get("use_rapidapi", True)
    if use_harmonic and not harmonic_key:
        raise PipelineFailed("HARMONIC_API_KEY is not set")
    if use_rapidapi and not rapid_key:
        raise PipelineFailed("RAPIDAPI_KEY/RAPIDAPI_LINKEDIN_KEY is not set")

    enriched: list[dict[str, Any]] = []
    raw_dir = Path(ledger["run_dir"]) / "raw_provider_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(rows, start=1):
        pub_id = row["public_identifier"]
        result = dict(row)
        harmonic = {"status_code": "", "data": None, "error": ""}
        rapid = {"status_code": "", "data": None, "error": ""}
        if use_harmonic:
            harmonic = harmonic_enrich(row["linkedin_url"], harmonic_key)
            time.sleep(ledger["input"].get("sleep_seconds", 0.0))
        if use_rapidapi:
            rapid = rapidapi_profile(pub_id, row["linkedin_url"], rapid_key)
            time.sleep(ledger["input"].get("sleep_seconds", 0.0))
        write_json(raw_dir / f"{pub_id}.json", {"connection": row, "harmonic": harmonic, "rapidapi": rapid})
        result.update({
            "harmonic_status_code": harmonic.get("status_code", ""),
            "harmonic_error": harmonic.get("error", ""),
            "harmonic_response": json.dumps(harmonic.get("data")) if harmonic.get("data") else "",
            "rapidapi_status_code": rapid.get("status_code", ""),
            "rapidapi_error": rapid.get("error", ""),
            "rapidapi_response": json.dumps(rapid.get("data")) if rapid.get("data") else "",
            "enriched_at": now_iso(),
        })
        enriched.append(result)
    out = Path(ledger["run_dir"]) / "provider_enriched.csv"
    write_csv(out, PROVIDER_COLUMNS, enriched)
    ledger["artifacts"]["provider_enriched_csv"] = str(out)
    ledger["artifacts"]["raw_provider_responses_dir"] = str(raw_dir)
    return {"processed": len(enriched), "output_file": str(out), "providers": {"harmonic": use_harmonic, "rapidapi": use_rapidapi}}


def step_merge_people(ledger: dict[str, Any]) -> dict[str, Any]:
    provider_path = Path(ledger["artifacts"].get("provider_enriched_csv") or ledger["artifacts"]["connections_csv"])
    rows = read_csv(provider_path)
    people: list[dict[str, Any]] = []
    for row in rows:
        harmonic_raw = json.loads(row["harmonic_response"]) if row.get("harmonic_response") else None
        rapid_raw = json.loads(row["rapidapi_response"]) if row.get("rapidapi_response") else None
        harmonic = normalize_harmonic(harmonic_raw, row["public_identifier"], row["linkedin_url"])
        rapid = normalize_rapidapi(rapid_raw, row["public_identifier"], row["linkedin_url"])
        people.append(merge_provider_profile(row, harmonic, rapid, harmonic_raw, rapid_raw))
    out = Path(ledger["run_dir"]) / "people_harmonic_all.csv"
    write_csv(out, PEOPLE_COLUMNS, people)
    ledger["artifacts"]["people_harmonic_all_csv"] = str(out)
    return {"rows": len(people), "output_file": str(out)}


def execute_step(ledger: dict[str, Any], step_id: str) -> dict[str, Any]:
    if step_id == "convert":
        return step_convert(ledger)
    if step_id == "enrich_providers":
        return step_enrich_providers(ledger)
    if step_id == "merge_people":
        return step_merge_people(ledger)
    raise PipelineFailed(f"unknown step: {step_id}")


def run_until_blocked_or_done(ledger_path: Path) -> int:
    ledger = load_ledger(ledger_path)
    while True:
        step_id = next_pending_step(ledger)
        if step_id is None:
            ledger["status"] = "completed"
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
            emit({"status": "completed", "ledger": str(ledger_path), "run_dir": ledger.get("run_dir"), "artifacts": ledger.get("artifacts", {})})
            return 0
        if step_id == "enrich_providers" and not is_approved(ledger, step_id):
            providers = []
            if ledger["input"].get("use_harmonic", True):
                providers.append("Harmonic")
            if ledger["input"].get("use_rapidapi", True):
                providers.append("RapidAPI")
            block_for_approval(ledger_path, ledger, step_id, f"Run paid external LinkedIn enrichment for {ledger.get('connection_count', 'converted')} connections via {', '.join(providers)}?")
        try:
            mark_step(ledger, step_id, "running")
            save_ledger(ledger_path, ledger)
            summary = execute_step(ledger, step_id)
            if step_id == "convert":
                ledger["connection_count"] = summary.get("parsed", 0)
            mark_step(ledger, step_id, "completed", summary=summary)
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
        except PipelineFailed as exc:
            mark_step(ledger, step_id, "failed", error=str(exc))
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": step_id, "error": str(exc), "ledger": str(ledger_path)})
            return 1


def command_run(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"linkedin-{sha(str(args.csv) + ':' + now_iso())}"
    run_dir = Path(args.output_dir) / "linkedin" / run_id
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force:
        existing = load_ledger(ledger_path)
        if existing.get("status") not in {"completed", "failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    ledger = {
        "primitive": "linkedin_network_import",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ledger": str(ledger_path),
        "input": {
            "csv": str(Path(args.csv)),
            "source_user": args.source_user,
            "operator_id": args.operator_id,
            "limit": args.limit,
            "use_harmonic": not args.no_harmonic,
            "use_rapidapi": not args.no_rapidapi,
            "sleep_seconds": args.sleep_seconds,
        },
        "steps": {},
        "approvals": {},
        "artifacts": {},
    }
    save_ledger(ledger_path, ledger)
    try:
        return run_until_blocked_or_done(ledger_path)
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_continue(args: argparse.Namespace) -> int:
    if not Path(args.ledger).exists():
        emit({"status": "missing_ledger", "ledger": args.ledger})
        return 2
    try:
        return run_until_blocked_or_done(Path(args.ledger))
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
    ledger = load_ledger(Path(args.ledger))
    emit({"status": ledger.get("status", "unknown"), "blocked": ledger.get("blocked"), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
    return 0


def command_check_keys(_: argparse.Namespace) -> int:
    emit({
        "status": "ok",
        "keys_present": {
            "HARMONIC_API_KEY": bool(os.getenv("HARMONIC_API_KEY", "").strip()),
            "RAPIDAPI_KEY": bool(os.getenv("RAPIDAPI_KEY", "").strip()),
            "RAPIDAPI_LINKEDIN_KEY": bool(os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()),
        },
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LinkedIn Connections.csv network import")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--csv", required=True)
    run.add_argument("--source-user", required=True)
    run.add_argument("--operator-id", default="local")
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--run-id")
    run.add_argument("--force", action="store_true")
    run.add_argument("--no-harmonic", action="store_true")
    run.add_argument("--no-rapidapi", action="store_true")
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.set_defaults(func=command_run)

    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=command_continue)

    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.add_argument("--approval-id")
    approve.set_defaults(func=command_approve)

    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=command_status)

    keys = sub.add_parser("check-keys")
    keys.set_defaults(func=command_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
