#!/usr/bin/env python3
"""Build reusable local network bootstrap bundles from existing export CSVs.

This primitive is intentionally generic: callers provide the operator mapping
and source directory. It does not hardcode Aleph paths into Powerpacks runtime
logic, but it can consume Aleph-style export filenames when explicitly passed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import tarfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

csv.field_size_limit(1024 * 1024 * 1024)

RESOLUTION_COLUMNS = ["handle", "status", "linkedin_url", "confidence", "matched_name", "matched_headline", "evidence", "reasoning"]
CONTACT_COLUMNS = ["display_name", "primary_email", "all_emails", "total_sent", "total_received", "total_messages", "thread_count", "first_interaction", "last_interaction", "source_files"]
SOURCE_COLUMNS = ["path", "kind", "rows", "size_bytes", "columns"]
BOOTSTRAP_SOURCE_COLUMNS = ["file", "kind", "rows", "source_path"]
URL_COLUMNS = [
    "confirmed_linkedin_url",
    "human_confirmed_linkedin",
    "final_linkedin_url",
    "proposed_linkedin_url",
    "linkedin_url",
    "linkedin_profile_url",
    "profile_url",
    "pass1_linkedin_url",
    "llm_selected_linkedin",
]
HIGH_CONFIDENCE_URL_COLUMNS = {"confirmed_linkedin_url", "human_confirmed_linkedin", "final_linkedin_url"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def csv_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return next(csv.reader(handle), [])


def csv_row_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def parse_jsonish(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def short_uuid(value: str) -> str:
    return value.split("-", 1)[0]


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


def extract_public_identifier(value: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?#]+)", value or "", re.IGNORECASE)
    return urllib.parse.unquote(match.group(1).strip().rstrip("/")).lower() if match else ""


def safe_cache_slug(public_identifier: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in public_identifier.lower().strip())
    return cleaned.strip("._")


def emails_from_value(value: Any) -> list[str]:
    parsed = parse_jsonish(value, None)
    found: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            found.extend(emails_from_value(item))
    elif isinstance(parsed, dict):
        for item in parsed.values():
            found.extend(emails_from_value(item))
    else:
        found.extend(match.group(0).lower() for match in EMAIL_RE.finditer(str(value or "")))
    return sorted(set(found))


def emails_from_row(row: dict[str, str]) -> list[str]:
    emails: list[str] = []
    for key in ("primary_email", "email", "all_emails", "emails"):
        emails.extend(emails_from_value(row.get(key, "")))
    return sorted(set(emails))


def display_name(row: dict[str, str]) -> str:
    full = row.get("display_name") or row.get("full_name") or row.get("harmonic_full_name") or ""
    if full:
        return full.strip()
    return f"{row.get('first_name', '').strip()} {row.get('last_name', '').strip()}".strip()


def source_kind(path: Path) -> str:
    name = path.name
    if name.startswith("confirmed_candidates"):
        return "confirmed_candidates"
    if name.startswith("linkedin_candidates"):
        return "linkedin_candidates"
    if name.startswith("llm_reviewed"):
        return "llm_reviewed"
    if name.startswith("parallel_enriched"):
        return "parallel_enriched"
    if name.startswith("targeted_emails"):
        return "targeted_emails"
    if name.startswith("harmonic_enriched"):
        return "harmonic_enriched"
    if name.startswith("enriched_profiles"):
        return "enriched_profiles"
    return "other"


def choose_linkedin_url(row: dict[str, str]) -> tuple[str, str]:
    for col in URL_COLUMNS:
        url = normalize_linkedin_url(row.get(col) or "")
        if extract_public_identifier(url):
            return url, col
    return "", ""


def confidence(row: dict[str, str], url_col: str) -> float:
    if url_col in HIGH_CONFIDENCE_URL_COLUMNS:
        return 1.0
    for key in ("confidence", "llm_confidence", "basis.linkedin_url.confidence"):
        raw = (row.get(key) or "").strip().lower()
        if raw in {"high", "confirmed", "exact"}:
            return 0.95
        if raw in {"medium", "med"}:
            return 0.8
        if raw == "low":
            return 0.5
        try:
            value = float(raw)
            return value / 100.0 if value > 1 else value
        except ValueError:
            pass
    return 0.9 if (row.get("status") or "").strip().lower() in {"completed", "found", "success"} else 0.8


def source_priority(kind: str, url_col: str) -> int:
    if url_col in {"confirmed_linkedin_url", "human_confirmed_linkedin"}:
        return 100
    if kind == "confirmed_candidates":
        return 90
    if kind == "parallel_enriched":
        return 80
    if url_col in {"final_linkedin_url", "linkedin_url"}:
        return 70
    if url_col in {"pass1_linkedin_url", "llm_selected_linkedin"}:
        return 60
    return 50


def load_operators(mapping_path: Path) -> dict[str, dict[str, Any]]:
    mapping = read_json(mapping_path)
    users = mapping.get("_users") or {}
    if not isinstance(users, dict):
        raise SystemExit(f"operator mapping is missing _users: {mapping_path}")
    operators: dict[str, dict[str, Any]] = {}
    for operator_id, slug in users.items():
        operators[str(slug)] = {
            "slug": str(slug),
            "operator_id": str(operator_id),
            "operator_short": short_uuid(str(operator_id)),
            "token_ids": list(mapping.get(operator_id) or []),
        }
    return operators


def discover_sources(source_dir: Path, operator: dict[str, Any]) -> list[Path]:
    op_short = operator["operator_short"]
    token_shorts = [short_uuid(token) for token in operator.get("token_ids", [])]
    patterns = [
        f"linkedin_candidates_merged_{op_short}.csv",
        f"confirmed_candidates_{op_short}.csv",
        f"confirmed_candidates_merged_{op_short}.csv",
        f"llm_reviewed_{op_short}.csv",
        f"human_review_{op_short}.csv",
        f"enriched_profiles_{op_short}.csv",
        f"harmonic_enriched_{op_short}.csv",
        f"harmonic_enriched_linkedin_csv_{op_short}.csv",
        f"parallel_enriched_*_{op_short}*.csv",
        f"targeted_emails_*_{op_short}.csv",
        f"gmail_contacts_aggregated_*_{op_short}.csv",
        f"linkedin_candidates_*_{op_short}.csv",
        f"enrichment_pass1_*_{op_short}.csv",
    ]
    for token_short in token_shorts:
        patterns.extend([f"parallel_enriched_{token_short}_{op_short}*.csv", f"targeted_emails_{token_short}_{op_short}.csv"])
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(source_dir.glob(pattern))
    blocked_bits = (".bkup", ".backup", "_input.csv", "_taskgroup")
    return sorted(path for path in paths if path.is_file() and not any(bit in path.name for bit in blocked_bits))


def source_manifest(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.append({"path": str(path.resolve()), "kind": source_kind(path), "rows": csv_row_count(path), "size_bytes": path.stat().st_size, "columns": json.dumps(csv_header(path))})
    return rows


def copy_bootstrap_source_files(paths: list[Path], target_dir: Path) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for path in paths:
        kind = source_kind(path)
        if kind != "linkedin_candidates":
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        shutil.copy2(path, target)
        manifest.append({
            "file": str(target),
            "kind": kind,
            "rows": csv_row_count(path),
            "source_path": str(path.resolve()),
        })
    return manifest


def build_contacts(paths: list[Path]) -> list[dict[str, Any]]:
    by_email: dict[str, dict[str, Any]] = {}
    for path in paths:
        if source_kind(path) not in {"targeted_emails", "linkedin_candidates", "confirmed_candidates", "llm_reviewed", "parallel_enriched"}:
            continue
        for row in read_csv(path):
            emails = emails_from_row(row)
            if not emails:
                continue
            primary = (row.get("primary_email") or row.get("email") or emails[0]).strip().lower()
            rec = by_email.setdefault(
                primary,
                {
                    "display_name": display_name(row),
                    "primary_email": primary,
                    "all_emails": set(),
                    "total_sent": row.get("total_sent") or "",
                    "total_received": row.get("total_received") or "",
                    "total_messages": row.get("total_messages") or "",
                    "thread_count": row.get("thread_count") or "",
                    "first_interaction": row.get("first_interaction") or "",
                    "last_interaction": row.get("last_interaction") or "",
                    "source_files": set(),
                },
            )
            rec["all_emails"].update(emails)
            rec["source_files"].add(str(path.resolve()))
            if not rec.get("display_name") and display_name(row):
                rec["display_name"] = display_name(row)
    rows = []
    for rec in sorted(by_email.values(), key=lambda item: item["primary_email"]):
        out = dict(rec)
        out["all_emails"] = json.dumps(sorted(rec["all_emails"]))
        out["source_files"] = json.dumps(sorted(rec["source_files"]))
        rows.append(out)
    return rows


def build_resolutions(paths: list[Path]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for path in paths:
        kind = source_kind(path)
        if kind not in {"confirmed_candidates", "linkedin_candidates", "llm_reviewed", "parallel_enriched", "enriched_profiles"}:
            continue
        for row in read_csv(path):
            url, url_col = choose_linkedin_url(row)
            if not url:
                continue
            conf = confidence(row, url_col)
            priority = source_priority(kind, url_col)
            evidence = {"source_file": str(path.resolve()), "source_kind": kind, "url_column": url_col, "public_identifier": extract_public_identifier(url)}
            for email in emails_from_row(row):
                candidate = {
                    "handle": email,
                    "status": "found" if conf >= 0.75 else "low_confidence",
                    "linkedin_url": url,
                    "confidence": f"{conf:.2f}",
                    "matched_name": display_name(row),
                    "matched_headline": row.get("matched_headline") or row.get("headline") or row.get("harmonic_headline") or "",
                    "evidence": json.dumps(evidence, sort_keys=True),
                    "reasoning": row.get("llm_reasoning") or row.get("basis.linkedin_url.reasoning") or "",
                    "_priority": priority,
                }
                current = best.get(email)
                if not current or (conf, priority) > (float(current.get("confidence") or 0), int(current.get("_priority") or 0)):
                    best[email] = candidate
    return [{col: row.get(col, "") for col in RESOLUTION_COLUMNS} for row in sorted(best.values(), key=lambda item: item["handle"])]


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def date_dict(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", value.strip())
    if not match:
        return None
    return {"year": int(match.group(1)), "month": int(match.group(2)) if match.group(2) else None, "day": int(match.group(3)) if match.group(3) else None}


def normalize_experience(exp: Any) -> dict[str, Any] | None:
    if not isinstance(exp, dict):
        return None
    company = exp.get("company_name") or exp.get("companyName") or (exp.get("company") if isinstance(exp.get("company"), str) and not str(exp.get("company")).startswith("urn:") else "")
    return {
        "title": exp.get("title") or exp.get("position") or exp.get("role") or "",
        "company_name": company,
        "company": company,
        "description": exp.get("description") or "",
        "starts_at": date_dict(exp.get("start_date") or exp.get("starts_at") or ""),
        "ends_at": date_dict(exp.get("end_date") or exp.get("ends_at") or ""),
        "is_current_position": bool(exp.get("is_current_position")) if exp.get("is_current_position") is not None else not bool(exp.get("end_date") or exp.get("ends_at")),
        "location": exp.get("location") or "",
    }


def normalize_education(edu: Any) -> dict[str, Any] | None:
    if not isinstance(edu, dict):
        return None
    school = edu.get("school")
    school_name = school.get("name") if isinstance(school, dict) else (school if isinstance(school, str) else "")
    out = dict(edu)
    out["school"] = school_name or edu.get("school_name") or edu.get("name") or ""
    out["starts_at"] = date_dict(edu.get("start_date") or edu.get("starts_at") or "")
    out["ends_at"] = date_dict(edu.get("end_date") or edu.get("ends_at") or "")
    return out


def empty_profile() -> dict[str, Any]:
    return {
        "success": True,
        "error": "",
        "public_identifier": "",
        "member_id": "",
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "headline": "",
        "summary": "",
        "location_str": "",
        "city": "",
        "state": "",
        "country": "",
        "profile_pic_url": "",
        "linkedin_url": "",
        "connections": "",
        "skills": [],
        "languages": [],
        "certifications": [],
        "education": [],
        "experiences": [],
    }


def cache_payload_from_harmonic(row: dict[str, str], source: Path) -> dict[str, Any] | None:
    raw = parse_jsonish(row.get("harmonic_response"), {})
    raw = raw if isinstance(raw, dict) else {}
    socials = raw.get("socials") if isinstance(raw.get("socials"), dict) else {}
    linked = socials.get("LINKEDIN") if isinstance(socials.get("LINKEDIN"), dict) else {}
    linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or linked.get("url") or "")
    public_id = row.get("public_identifier") or extract_public_identifier(linkedin_url)
    if not public_id:
        return None
    location = raw.get("location") if isinstance(raw.get("location"), dict) else parse_jsonish(row.get("harmonic_location"), {})
    location = location if isinstance(location, dict) else {}
    full_name = raw.get("full_name") or row.get("harmonic_full_name") or display_name(row)
    first, last = (raw.get("first_name") or row.get("first_name") or "", raw.get("last_name") or row.get("last_name") or "")
    if full_name and (not first or not last):
        first, last = split_name(full_name)
    profile = empty_profile()
    profile.update(
        {
            "public_identifier": public_id,
            "member_id": str(raw.get("id") or ""),
            "first_name": first,
            "last_name": last,
            "full_name": full_name,
            "headline": row.get("harmonic_headline") or raw.get("linkedin_headline") or row.get("headline") or "",
            "summary": raw.get("summary") or "",
            "location_str": location.get("location") or row.get("location_raw") or "",
            "city": location.get("city") or row.get("city") or "",
            "state": location.get("state") or row.get("state") or "",
            "country": location.get("country") or row.get("country") or "",
            "profile_pic_url": raw.get("profile_picture_url") or row.get("profile_picture_url") or "",
            "linkedin_url": linkedin_url or f"https://www.linkedin.com/in/{public_id}",
            "connections": raw.get("linkedin_connections") or "",
            "education": [item for item in (normalize_education(edu) for edu in (raw.get("education") or [])) if item],
            "experiences": [item for item in (normalize_experience(exp) for exp in (raw.get("experience") or raw.get("experiences") or [])) if item],
        }
    )
    if not any([profile["full_name"], profile["headline"], profile["education"], profile["experiences"]]):
        return None
    return {"fetched_at": row.get("enriched_at") or raw.get("last_refreshed_at") or now_iso(), "public_identifier": public_id, "linkedin_url": profile["linkedin_url"], "raw_response": profile, "normalized_profile": profile, "source": {"provider": "existing_export_bootstrap", "source_file": str(source.resolve())}}


def cache_payload_from_enriched_profile(row: dict[str, str], source: Path) -> dict[str, Any] | None:
    linkedin_url = normalize_linkedin_url(row.get("linkedin_profile_url") or row.get("linkedin_url") or "")
    public_id = row.get("public_identifier") or extract_public_identifier(linkedin_url)
    if not public_id:
        return None
    full_name = row.get("display_name") or row.get("full_name") or ""
    first = row.get("first_name") or ""
    last = row.get("last_name") or ""
    if full_name and (not first or not last):
        first, last = split_name(full_name)
    profile = empty_profile()
    profile.update(
        {
            "public_identifier": public_id,
            "first_name": first,
            "last_name": last,
            "full_name": full_name,
            "headline": row.get("headline") or row.get("occupation") or "",
            "summary": row.get("summary") or "",
            "location_str": row.get("location_str") or "",
            "city": row.get("city") or "",
            "state": row.get("state") or "",
            "country": row.get("country") or row.get("country_full_name") or "",
            "profile_pic_url": row.get("profile_pic_url") or "",
            "linkedin_url": linkedin_url or f"https://www.linkedin.com/in/{public_id}",
            "connections": row.get("connections") or "",
            "education": parse_jsonish(row.get("education_json"), []),
            "experiences": parse_jsonish(row.get("experiences_json"), []),
        }
    )
    return {"fetched_at": row.get("enriched_at") or now_iso(), "public_identifier": public_id, "linkedin_url": profile["linkedin_url"], "raw_response": profile, "normalized_profile": profile, "source": {"provider": "existing_export_bootstrap", "source_file": str(source.resolve())}}


def write_profile_cache(paths: list[Path], cache_dir: Path) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    public_ids: set[str] = set()
    source_counts: dict[str, int] = {}
    for path in paths:
        kind = source_kind(path)
        if kind not in {"harmonic_enriched", "enriched_profiles"}:
            continue
        for row in read_csv(path):
            payload = cache_payload_from_harmonic(row, path) if kind == "harmonic_enriched" else cache_payload_from_enriched_profile(row, path)
            if not payload:
                continue
            slug = safe_cache_slug(payload["public_identifier"])
            if not slug:
                continue
            write_json(cache_dir / f"{slug}.json", payload)
            public_ids.add(payload["public_identifier"])
            source_counts[path.name] = source_counts.get(path.name, 0) + 1
            written += 1
    return {"cache_files_written": written, "public_identifiers": sorted(public_ids), "source_counts": source_counts}


def filter_cached_resolutions(resolutions: list[dict[str, Any]], cached_public_ids: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cached: list[dict[str, Any]] = []
    uncached: list[dict[str, Any]] = []
    for row in resolutions:
        public_id = extract_public_identifier(row.get("linkedin_url") or "")
        if public_id in cached_public_ids:
            cached.append(row)
        else:
            miss = dict(row)
            miss["public_identifier"] = public_id
            uncached.append(miss)
    return cached, uncached


def linkedin_export_header(line: str) -> bool:
    lowered = line.strip().lower()
    return lowered.startswith("first name,") or ("first name" in lowered and "url" in lowered and "," in lowered)


def write_cached_linkedin_subset(linkedin_csv: str, output_path: Path, cached_public_ids: set[str]) -> dict[str, Any]:
    source = Path(linkedin_csv)
    if not linkedin_csv or not source.exists():
        return {"status": "missing", "rows": 0, "full_rows": 0, "cache_misses": 0}
    with source.open("r", encoding="utf-8-sig", errors="replace") as handle:
        header = ""
        for line in handle:
            if linkedin_export_header(line):
                header = line.strip()
                break
        if not header:
            return {"status": "missing_header", "rows": 0, "full_rows": 0, "cache_misses": 0}
        fieldnames = next(csv.reader([header]))
        rows: list[dict[str, str]] = []
        full_rows = 0
        for row in csv.DictReader(handle, fieldnames=fieldnames):
            public_id = extract_public_identifier(normalize_linkedin_url(row.get("URL", "")))
            if not public_id:
                continue
            full_rows += 1
            if public_id in cached_public_ids:
                rows.append(row)
    write_csv(output_path, fieldnames, rows)
    return {"status": "ok", "rows": len(rows), "full_rows": full_rows, "cache_misses": full_rows - len(rows), "path": str(output_path.resolve())}


def build_commands(operator: dict[str, Any], bundle_dir: Path, linkedin_csv: str, gmail_account_email: str) -> str:
    parts = [
        "uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run",
        f"--operator-id {operator['operator_id']}",
        f"--run-id network-bootstrap-{operator['slug']}",
        f"--ledger {bundle_dir}/outputs/import-network.ledger.json",
        "--force",
    ]
    if linkedin_csv:
        parts.extend([f"--linkedin-csv {linkedin_csv}", f"--linkedin-source-user {operator['slug']}"])
    if gmail_account_email:
        parts.append(f"--gmail-account-email {gmail_account_email}")
    return " \\\n  ".join(parts) + "\n"


def copy_cache(source_dir: Path, target_dir: Path) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in source_dir.glob("*.json"):
        shutil.copyfile(path, target_dir / path.name)
        copied += 1
    return copied


def build_bundle(args: argparse.Namespace, operator: dict[str, Any]) -> dict[str, Any]:
    output_root = Path(args.output_root)
    bundle_dir = output_root / "operators" / operator["slug"]
    if args.force and bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    inputs_dir = bundle_dir / "inputs"
    resolution_dir = bundle_dir / "resolution"
    cache_dir = bundle_dir / "enrichment" / "profile_cache_v2"
    outputs_dir = bundle_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    sources = discover_sources(Path(args.source_dir), operator)
    source_rows = source_manifest(sources)
    bootstrap_source_rows = copy_bootstrap_source_files(sources, inputs_dir / "linkedin_candidates")
    contacts = build_contacts(sources)
    resolutions = build_resolutions(sources)
    cache_stats = write_profile_cache(sources, cache_dir)
    cached_ids = set(cache_stats["public_identifiers"])
    cached_resolutions, uncached_resolutions = filter_cached_resolutions(resolutions, cached_ids)
    linkedin_subset = write_cached_linkedin_subset(args.linkedin_csv, inputs_dir / "linkedin_connections_cached.csv", cached_ids)

    write_csv(inputs_dir / "source_files_manifest.csv", SOURCE_COLUMNS, source_rows)
    write_csv(inputs_dir / "linkedin_candidates_manifest.csv", BOOTSTRAP_SOURCE_COLUMNS, bootstrap_source_rows)
    write_csv(inputs_dir / "contact_rows_min.csv", CONTACT_COLUMNS, contacts)
    write_csv(resolution_dir / "linkedin_resolutions.csv", RESOLUTION_COLUMNS, resolutions)
    write_csv(resolution_dir / "linkedin_resolutions_cached.csv", RESOLUTION_COLUMNS, cached_resolutions)
    write_csv(resolution_dir / "linkedin_resolutions_uncached.csv", RESOLUTION_COLUMNS + ["public_identifier"], uncached_resolutions)

    seeded = copy_cache(cache_dir, Path(args.profile_cache_dir)) if args.seed_profile_cache else 0
    command_linkedin = str(inputs_dir / "linkedin_connections_cached.csv") if linkedin_subset.get("rows") else args.linkedin_csv
    (outputs_dir / "commands.txt").write_text(build_commands(operator, bundle_dir, command_linkedin, args.gmail_account_email), encoding="utf-8")

    manifest = {
        "status": "ok",
        "generated_at": now_iso(),
        "operator": operator["slug"],
        "operator_id": operator["operator_id"],
        "operator_short": operator["operator_short"],
        "token_ids": operator.get("token_ids") or [],
        "source_dir": str(Path(args.source_dir).resolve()),
        "source_root_fingerprint": {"file_count": len(sources), "sha": sha("\n".join(str(path.resolve()) for path in sources), 16)},
        "artifacts": {
            "source_files_manifest": str((inputs_dir / "source_files_manifest.csv").resolve()),
            "linkedin_candidates_manifest": str((inputs_dir / "linkedin_candidates_manifest.csv").resolve()),
            "linkedin_candidates_dir": str((inputs_dir / "linkedin_candidates").resolve()),
            "contact_rows_min": str((inputs_dir / "contact_rows_min.csv").resolve()),
            "linkedin_resolutions": str((resolution_dir / "linkedin_resolutions.csv").resolve()),
            "linkedin_resolutions_cached": str((resolution_dir / "linkedin_resolutions_cached.csv").resolve()),
            "linkedin_resolutions_uncached": str((resolution_dir / "linkedin_resolutions_uncached.csv").resolve()),
            "linkedin_connections_cached": str((inputs_dir / "linkedin_connections_cached.csv").resolve()),
            "profile_cache_dir": str(cache_dir.resolve()),
            "commands": str((outputs_dir / "commands.txt").resolve()),
        },
        "counts": {
            "source_files": len(sources),
            "linkedin_candidate_source_files": len(bootstrap_source_rows),
            "linkedin_candidate_source_rows": sum(int(row.get("rows") or 0) for row in bootstrap_source_rows),
            "contact_min_rows": len(contacts),
            "linkedin_resolution_rows": len(resolutions),
            "linkedin_resolution_cached_rows": len(cached_resolutions),
            "linkedin_resolution_uncached_rows": len(uncached_resolutions),
            "linkedin_connections_cached_rows": int(linkedin_subset.get("rows") or 0),
            "linkedin_connections_full_rows": int(linkedin_subset.get("full_rows") or 0),
            "linkedin_connections_uncached_rows": int(linkedin_subset.get("cache_misses") or 0),
            "profile_cache_files": len(list(cache_dir.glob("*.json"))),
            "default_cache_files_seeded": seeded,
        },
        "privacy": {"raw_gmail_db_copied": False, "message_bodies_copied": False, "sample_subjects_copied": False, "secrets_copied": False},
    }
    write_json(bundle_dir / "manifest.json", manifest)
    write_json(outputs_dir / "counts.json", manifest["counts"])

    bundles_dir = output_root / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundles_dir / f"{operator['slug']}.tar.gz"
    if args.force and bundle_path.exists():
        bundle_path.unlink()
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(bundle_dir, arcname=operator["slug"])
    manifest["artifacts"]["bundle"] = str(bundle_path.resolve())
    write_json(bundle_dir / "manifest.json", manifest)
    return manifest


def cmd_generate(args: argparse.Namespace) -> int:
    operators = load_operators(Path(args.operator_mapping))
    selected = [slug.strip() for slug in args.operators.split(",") if slug.strip()]
    missing = [slug for slug in selected if slug not in operators]
    if missing:
        raise SystemExit(f"unknown operators: {', '.join(missing)}")
    manifests = [build_bundle(args, operators[slug]) for slug in selected]
    summary = {
        "status": "ok",
        "generated_at": now_iso(),
        "output_root": str(Path(args.output_root).resolve()),
        "operators": [
            {"operator": manifest["operator"], "operator_id": manifest["operator_id"], "token_ids": manifest["token_ids"], "counts": manifest["counts"], "bundle": manifest["artifacts"].get("bundle")}
            for manifest in manifests
        ],
    }
    write_json(Path(args.output_root) / "summary.json", summary)
    emit(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--operator-mapping", required=True, help="JSON mapping with _users and operator UUID token lists")
    gen.add_argument("--source-dir", required=True, help="Directory containing existing export CSVs")
    gen.add_argument("--operators", required=True, help="Comma-separated operator slugs to build")
    gen.add_argument("--output-root", default=".powerpacks/network-bootstrap")
    gen.add_argument("--linkedin-csv", default="")
    gen.add_argument("--gmail-account-email", default="")
    gen.add_argument("--seed-profile-cache", action="store_true")
    gen.add_argument("--profile-cache-dir", default=".powerpacks/network-import/profile_cache_v2")
    gen.add_argument("--force", action="store_true")
    gen.set_defaults(func=cmd_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
