#!/usr/bin/env python3
"""Unified local people enrichment flow.

Self-contained Powerpacks implementation copied/adapted from the Aleph ingestion
provider merge logic. No imports from aleph-mvp or network-search-api.

Input: a shared people schema CSV, usually merge_network_sources output.
Output: enriched people schema CSV plus raw provider responses.

Spend-bearing provider calls are approval-gated.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
        parse_jsonish,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
        parse_jsonish,
    )

DEFAULT_LEDGER = Path(".powerpacks/network-import/enrichment/import-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
HARMONIC_BASE_URL = "https://api.harmonic.ai"
RAPIDAPI_BASE_URL = "https://professional-network-data.p.rapidapi.com"
PIPELINE_STEPS = ["prepare_queue", "enrich_linkedin", "merge_people"]

QUEUE_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["enrichment_route", "enrichment_reason"]
PROVIDER_COLUMNS = QUEUE_COLUMNS + [
    "harmonic_status_code",
    "harmonic_error",
    "harmonic_response",
    "rapidapi_status_code",
    "rapidapi_error",
    "rapidapi_response_enriched",
    "provider_enriched_at",
]


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


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def generate_person_id(public_identifier: str, fallback: str = "") -> str:
    import uuid
    if public_identifier:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"linkedin:{public_identifier.lower().strip()}"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"person:{fallback.lower().strip()}"))


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


def profile_richness(experiences: Any, education: Any) -> int:
    return count_items(experiences) + count_items(education)


def current_position(experiences: list[dict[str, Any]]) -> tuple[str, str, str]:
    for exp in experiences or []:
        if not isinstance(exp, dict):
            continue
        if exp.get("is_current_position") or exp.get("is_current") or exp.get("current") or not (exp.get("ends_at") or exp.get("end_date")):
            return (
                str(exp.get("title") or exp.get("position") or ""),
                str(exp.get("company_name") or exp.get("company") or exp.get("organization") or ""),
                str(exp.get("company_urn") or exp.get("company_id") or ""),
            )
    if experiences:
        exp = experiences[0]
        if isinstance(exp, dict):
            return (
                str(exp.get("title") or exp.get("position") or ""),
                str(exp.get("company_name") or exp.get("company") or exp.get("organization") or ""),
                str(exp.get("company_urn") or exp.get("company_id") or ""),
            )
    return "", "", ""


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
    location_str = profile.get("location_str") or (profile.get("location") if isinstance(profile.get("location"), str) else "")
    exp_list = experiences if isinstance(experiences, list) else []
    title, company, company_urn = current_position(exp_list)
    return {
        "public_identifier": profile.get("public_identifier") or profile.get("username") or public_identifier,
        "linkedin_url": normalize_linkedin_url(profile.get("linkedin_url") or profile.get("profile_url") or profile.get("profileURL") or linkedin_url),
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
        "work_experiences": exp_list,
        "education": education if isinstance(education, list) else [],
        "current_title": title,
        "current_company": company,
        "current_company_urn": company_urn,
        "entity_urn": profile.get("entity_urn") or "",
    }


def normalize_harmonic(data: dict[str, Any] | None, public_identifier: str, linkedin_url: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    # Harmonic may return an async token/detail payload instead of a full profile.
    if "full_name" not in data and "first_name" not in data:
        return {}
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    socials = data.get("socials") if isinstance(data.get("socials"), dict) else {}
    linkedin = socials.get("LINKEDIN") if isinstance(socials.get("LINKEDIN"), dict) else {}
    experiences = data.get("experience") if isinstance(data.get("experience"), list) else []
    education = data.get("education") if isinstance(data.get("education"), list) else []
    title, company, company_urn = current_position(experiences)
    return {
        "public_identifier": public_identifier,
        "linkedin_url": normalize_linkedin_url(linkedin.get("url") or data.get("linkedin_url") or linkedin_url),
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


def row_has_profile_gaps(row: dict[str, str]) -> bool:
    if not row.get("full_name") and not (row.get("first_name") and row.get("last_name")):
        return True
    if not row.get("headline"):
        return True
    if not row.get("current_company") and not row.get("current_title"):
        return True
    if profile_richness(row.get("work_experiences"), row.get("education")) == 0:
        return True
    return False


def route_row(row: dict[str, str], force: bool = False) -> tuple[str, str]:
    linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
    public_identifier = row.get("public_identifier") or extract_public_identifier(linkedin_url)
    if linkedin_url or public_identifier:
        if force or row_has_profile_gaps(row):
            return "linkedin_provider", "linkedin identifier present and profile has gaps"
        return "skip_complete", "linkedin identifier present and profile appears complete"
    if row.get("primary_email") or row.get("primary_phone") or row.get("twitter_handle") or row.get("full_name"):
        return "needs_resolution", "no linkedin identifier; needs resolution/research before provider enrichment"
    return "skip_no_identifier", "no usable identifier"


def merge_provider_profile(base: dict[str, Any], harmonic: dict[str, Any], rapid: dict[str, Any], harmonic_raw: dict[str, Any] | None, rapid_raw: dict[str, Any] | None) -> dict[str, Any]:
    base = normalize_people_row(base)
    public_identifier = base.get("public_identifier") or extract_public_identifier(base.get("linkedin_url") or "")
    h_score = profile_richness(harmonic.get("work_experiences", []), harmonic.get("education", []))
    r_score = profile_richness(rapid.get("work_experiences", []), rapid.get("education", []))
    rich = rapid if r_score > h_score else harmonic
    fallback = harmonic if rich is rapid else rapid
    provider = "rapidapi" if rich is rapid else "harmonic"
    if harmonic and rapid:
        provider = f"{provider}_preferred_harmonic_rapidapi"
    elif not harmonic and not rapid:
        provider = base.get("enrichment_provider") or "existing_only"

    row: dict[str, Any] = dict(base)
    row["id"] = row.get("id") or generate_person_id(public_identifier, row.get("full_name") or row.get("primary_email") or row.get("primary_phone") or "")
    row["public_identifier"] = public_identifier
    row["linkedin_url"] = normalize_linkedin_url(row.get("linkedin_url") or rich.get("linkedin_url") or fallback.get("linkedin_url") or "")
    row["enriched_at"] = now_iso() if (harmonic or rapid) else row.get("enriched_at", "")
    row["enrichment_provider"] = provider
    for key in [
        "linkedin_url", "first_name", "last_name", "full_name", "headline", "summary",
        "city", "state", "country", "location_raw", "profile_picture_url",
        "current_title", "current_company", "current_company_urn", "entity_urn", "harmonic_location",
    ]:
        row[key] = rich.get(key) or fallback.get(key) or row.get(key, "")
    if not row.get("full_name"):
        row["full_name"] = f"{row.get('first_name','')} {row.get('last_name','')}".strip()
    row["work_experiences"] = json.dumps(rich.get("work_experiences") or fallback.get("work_experiences") or parse_jsonish(row.get("work_experiences"), []))
    row["education"] = json.dumps(rich.get("education") or fallback.get("education") or parse_jsonish(row.get("education"), []))
    if harmonic_raw:
        row["harmonic_response"] = json.dumps(harmonic_raw)
    if rapid_raw:
        row["rapidapi_response"] = json.dumps(rapid_raw)
    return normalize_people_row(row)


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "enrich_people")
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
        "continue_command": f"uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py approve --ledger {ledger_path} && uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py continue --ledger {ledger_path}",
    })


def step_prepare_queue(ledger: dict[str, Any]) -> dict[str, Any]:
    rows = [normalize_people_row(row) for row in read_csv(Path(ledger["input"]["input_csv"]))]
    limit = ledger["input"].get("limit")
    if limit:
        rows = rows[: int(limit)]
    queue: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    route_counts: dict[str, int] = {}
    for row in rows:
        route, reason = route_row(row, force=bool(ledger["input"].get("force")))
        row["enrichment_route"] = route
        row["enrichment_reason"] = reason
        route_counts[route] = route_counts.get(route, 0) + 1
        if route == "linkedin_provider":
            queue.append(row)
        elif route == "needs_resolution":
            unresolved.append(row)
        else:
            skipped.append(row)
    run_dir = Path(ledger["run_dir"])
    queue_path = run_dir / "linkedin_enrichment_queue.csv"
    unresolved_path = run_dir / "needs_resolution_queue.csv"
    skipped_path = run_dir / "skipped_enrichment.csv"
    write_csv(queue_path, QUEUE_COLUMNS, queue)
    write_csv(unresolved_path, QUEUE_COLUMNS, unresolved)
    write_csv(skipped_path, QUEUE_COLUMNS, skipped)
    ledger["artifacts"].update({
        "linkedin_enrichment_queue_csv": str(queue_path),
        "needs_resolution_queue_csv": str(unresolved_path),
        "skipped_enrichment_csv": str(skipped_path),
    })
    ledger["queue_count"] = len(queue)
    return {"input_rows": len(rows), "queue_rows": len(queue), "unresolved_rows": len(unresolved), "skipped_rows": len(skipped), "route_counts": route_counts}


def step_enrich_linkedin(ledger: dict[str, Any]) -> dict[str, Any]:
    rows = read_csv(Path(ledger["artifacts"]["linkedin_enrichment_queue_csv"]))
    if not rows:
        out_path = Path(ledger["run_dir"]) / "provider_enriched.csv"
        write_csv(out_path, PROVIDER_COLUMNS, [])
        ledger["artifacts"]["provider_enriched_csv"] = str(out_path)
        return {"processed": 0, "output_file": str(out_path), "providers": {"harmonic": False, "rapidapi": False}}
    harmonic_key = os.getenv("HARMONIC_API_KEY", "").strip()
    rapid_key = os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()
    use_harmonic = ledger["input"].get("use_harmonic", True)
    use_rapidapi = ledger["input"].get("use_rapidapi", True)
    if not use_harmonic and not use_rapidapi:
        raise PipelineFailed("At least one LinkedIn enrichment provider must be enabled")
    if use_harmonic and not harmonic_key:
        raise PipelineFailed("HARMONIC_API_KEY is not set")
    if use_rapidapi and not rapid_key:
        raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")
    raw_dir = Path(ledger["run_dir"]) / "raw_provider_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
        if not public_identifier and linkedin_url:
            public_identifier = extract_public_identifier(linkedin_url)
        harmonic = {"status_code": "", "data": None, "error": ""}
        rapid = {"status_code": "", "data": None, "error": ""}
        if use_harmonic:
            harmonic = harmonic_enrich(linkedin_url, harmonic_key)
            time.sleep(float(ledger["input"].get("sleep_seconds") or 0.0))
        if use_rapidapi:
            rapid = rapidapi_profile(public_identifier, linkedin_url, rapid_key)
            time.sleep(float(ledger["input"].get("sleep_seconds") or 0.0))
        write_json(raw_dir / f"{public_identifier or sha(linkedin_url or row.get('id',''))}.json", {"input": row, "harmonic": harmonic, "rapidapi": rapid})
        out = dict(row)
        out.update({
            "public_identifier": public_identifier,
            "linkedin_url": linkedin_url,
            "harmonic_status_code": harmonic.get("status_code", ""),
            "harmonic_error": harmonic.get("error", ""),
            "harmonic_response": json.dumps(harmonic.get("data")) if harmonic.get("data") else row.get("harmonic_response", ""),
            "rapidapi_status_code": rapid.get("status_code", ""),
            "rapidapi_error": rapid.get("error", ""),
            "rapidapi_response_enriched": json.dumps(rapid.get("data")) if rapid.get("data") else "",
            "provider_enriched_at": now_iso(),
        })
        enriched.append(out)
    out_path = Path(ledger["run_dir"]) / "provider_enriched.csv"
    write_csv(out_path, PROVIDER_COLUMNS, enriched)
    ledger["artifacts"].update({"provider_enriched_csv": str(out_path), "raw_provider_responses_dir": str(raw_dir)})
    return {"processed": len(enriched), "output_file": str(out_path), "providers": {"harmonic": use_harmonic, "rapidapi": use_rapidapi}}


def step_merge_people(ledger: dict[str, Any]) -> dict[str, Any]:
    original_rows = [normalize_people_row(row) for row in read_csv(Path(ledger["input"]["input_csv"]))]
    by_key: dict[str, dict[str, Any]] = {}
    for row in original_rows:
        key = row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or sha(json.dumps(row, sort_keys=True))
        by_key[key] = row
    provider_path = Path(ledger["artifacts"].get("provider_enriched_csv") or ledger["artifacts"].get("linkedin_enrichment_queue_csv"))
    enriched_rows = read_csv(provider_path) if provider_path and provider_path.exists() else []
    for row in enriched_rows:
        harmonic_raw = json.loads(row["harmonic_response"]) if row.get("harmonic_response") else None
        rapid_raw = json.loads(row["rapidapi_response_enriched"]) if row.get("rapidapi_response_enriched") else (json.loads(row["rapidapi_response"]) if row.get("rapidapi_response") else None)
        public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
        harmonic = normalize_harmonic(harmonic_raw, public_identifier, row.get("linkedin_url", ""))
        rapid = normalize_rapidapi(rapid_raw, public_identifier, row.get("linkedin_url", ""))
        merged = merge_provider_profile(row, harmonic, rapid, harmonic_raw, rapid_raw)
        key = row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or sha(json.dumps(row, sort_keys=True))
        by_key[key] = merged
    output = Path(ledger["run_dir"]) / "people_enriched.csv"
    rows = list(by_key.values())
    write_csv(output, PEOPLE_SCHEMA_COLUMNS, rows)
    ledger["artifacts"]["people_enriched_csv"] = str(output)
    return {"rows": len(rows), "output_file": str(output)}


def execute_step(ledger: dict[str, Any], step_id: str) -> dict[str, Any]:
    if step_id == "prepare_queue":
        return step_prepare_queue(ledger)
    if step_id == "enrich_linkedin":
        return step_enrich_linkedin(ledger)
    if step_id == "merge_people":
        return step_merge_people(ledger)
    raise PipelineFailed(f"unknown step: {step_id}")


def ensure_keys(ledger: dict[str, Any]) -> None:
    use_harmonic = ledger["input"].get("use_harmonic", True)
    use_rapidapi = ledger["input"].get("use_rapidapi", True)
    if not use_harmonic and not use_rapidapi:
        raise PipelineFailed("At least one LinkedIn enrichment provider must be enabled")
    if use_harmonic and not os.getenv("HARMONIC_API_KEY"):
        raise PipelineFailed("HARMONIC_API_KEY is not set")
    if use_rapidapi and not (os.getenv("RAPIDAPI_LINKEDIN_KEY") or os.getenv("RAPIDAPI_KEY")):
        raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")


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
        try:
            if step_id == "enrich_linkedin" and int(ledger.get("queue_count") or 0) > 0 and not is_approved(ledger, step_id):
                ensure_keys(ledger)
                block_for_approval(ledger_path, ledger, step_id, f"Run paid LinkedIn provider enrichment for {ledger.get('queue_count')} people?")
            if step_id == "enrich_linkedin" and int(ledger.get("queue_count") or 0) > 0:
                ensure_keys(ledger)
            mark_step(ledger, step_id, "running")
            save_ledger(ledger_path, ledger)
            summary = execute_step(ledger, step_id)
            mark_step(ledger, step_id, "completed", summary=summary)
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
        except PipelineFailed as exc:
            mark_step(ledger, step_id, "failed", error=str(exc))
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": step_id, "error": str(exc), "ledger": str(ledger_path)})
            return 1
        except PipelineBlocked:
            raise


def command_run(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"enrich-{sha(str(args.input) + ':' + now_iso())}"
    run_dir = Path(args.output_dir) / "enrichment" / run_id
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force_ledger:
        existing = load_ledger(ledger_path)
        if existing.get("status") not in {"completed", "failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force-ledger."})
            return 0
    ledger = {
        "primitive": "enrich_people",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ledger": str(ledger_path),
        "input": {
            "input_csv": str(Path(args.input)),
            "limit": args.limit,
            "force": args.force,
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
    step_id = args.step or blocked.get("step_id") or next_pending_step(ledger)
    if not step_id:
        emit({"status": "no_pending_approval", "ledger": str(ledger_path)})
        return 1
    app_id = approval_id(ledger, step_id)
    ledger.setdefault("approvals", {})[app_id] = {"approved_at": now_iso(), "source": "operator", "step_id": step_id}
    if ledger.get("blocked", {}).get("step_id") == step_id:
        ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "approved", "approval_id": app_id, "ledger": str(ledger_path)})
    return 0


def command_status(args: argparse.Namespace) -> int:
    ledger = load_ledger(Path(args.ledger))
    emit({"status": ledger.get("status", "unknown"), "blocked": ledger.get("blocked"), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {}), "queue_count": ledger.get("queue_count")})
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
    parser = argparse.ArgumentParser(description="Unified people enrichment flow for shared people schema CSVs")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True, help="Input shared people schema CSV, e.g. merged people CSV")
    run.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--run-id")
    run.add_argument("--force", action="store_true", help="Re-enrich rows even if they appear complete")
    run.add_argument("--force-ledger", action="store_true", help="Overwrite an active ledger")
    run.add_argument("--no-harmonic", action="store_true")
    run.add_argument("--no-rapidapi", action="store_true")
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.set_defaults(func=command_run)

    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=command_continue)

    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.add_argument("--step", choices=PIPELINE_STEPS)
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
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
