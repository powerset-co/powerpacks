#!/usr/bin/env python3
"""Merge/dedupe local network-import sources into one people schema CSV.

Dedupe rule:
1. Merge rows with the same LinkedIn public identifier / URL.
2. Keep non-LinkedIn rows separate, but emit similar-name review pairs.

Stdlib-only. Local artifacts only. No uploads or external API calls.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

csv.field_size_limit(sys.maxsize)

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        normalize_people_row,
        stable_linkedin_key,
        extract_public_identifier,
    )
    from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        normalize_people_row,
        stable_linkedin_key,
        extract_public_identifier,
    )
    from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile

DEFAULT_OUTPUT_DIR = Path(".powerpacks/network-import/merged")
MERGED_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["merge_key", "merge_confidence", "merge_sources", "merged_row_count", "needs_review"]
REVIEW_COLUMNS = ["left_id", "right_id", "left_name", "right_name", "similarity", "left_sources", "right_sources", "reason"]
NETWORK_CONTACT_COLUMNS = [
    "contact_id",
    "merge_key",
    "display_name",
    "linkedin_url",
    "public_identifier",
    "primary_email",
    "primary_phone",
    "source_channels",
    "source_count",
    "needs_review",
]
NETWORK_CONTACT_SOURCE_COLUMNS = [
    "contact_id",
    "merge_key",
    "source_channel",
    "source_identifier",
    "source_artifact",
    "display_name",
    "linkedin_url",
    "public_identifier",
    "primary_email",
    "primary_phone",
]
NETWORK_COMPANY_COLUMNS = [
    "company_id",
    "company_key",
    "company_name",
    "company_urn",
    "source_channels",
    "contact_count",
    "contact_ids",
    "contact_names",
]
MAX_SOURCE_ARTIFACTS_PER_ROW = 12
MAX_SOURCE_ARTIFACT_TEXT = 4096


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def sha(value: str, n: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def normalize_name(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    parts = [p for p in value.split() if p not in {"jr", "sr", "ii", "iii", "phd", "mba", "md"}]
    return " ".join(parts)


def row_name(row: dict[str, str]) -> str:
    return row.get("full_name") or " ".join(x for x in [row.get("first_name", ""), row.get("last_name", "")] if x).strip() or row.get("name", "") or row.get("display_name", "")


def listish_values(value: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


def source_artifact_values(value: Any, *, _depth: int = 0) -> list[str]:
    """Flatten source_artifacts provenance without preserving nested JSON blobs.

    Earlier merges could include an already-merged row whose source_artifacts was
    itself a JSON array. Treating that whole JSON array as one artifact caused
    exponential growth on repeated fan-ins. Keep only a bounded, flat list of
    readable artifact references; source_artifacts is debug provenance, not a
    searchable/indexing payload.
    """
    if value is None or _depth > 6:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(source_artifact_values(item, _depth=_depth + 1))
        return out
    text = str(value).strip()
    if not text:
        return []
    if len(text) > MAX_SOURCE_ARTIFACT_TEXT and not text.startswith("["):
        return [text[:MAX_SOURCE_ARTIFACT_TEXT] + "…"]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            # Malformed huge provenance is not useful enough to retain.
            return [] if len(text) > MAX_SOURCE_ARTIFACT_TEXT else [text]
        return source_artifact_values(parsed, _depth=_depth + 1)
    return [text]


def compact_source_artifacts(values: list[Any], *, limit: int = MAX_SOURCE_ARTIFACTS_PER_ROW) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for artifact in source_artifact_values(value):
            artifact = artifact.strip()
            if not artifact or artifact in seen:
                continue
            seen.add(artifact)
            out.append(artifact)
    out.sort()
    if len(out) > limit:
        remaining = len(out) - limit
        out = out[:limit] + [f"... {remaining} more source artifact(s) omitted"]
    return json.dumps(out, ensure_ascii=False)


def usable_rapidapi_payload(value: str) -> bool:
    if not value:
        return False
    try:
        payload = json.loads(value)
    except Exception:
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    return normalize_linkedin_profile(payload).get("success") is True


def has_rapidapi_profile(row: dict[str, Any]) -> bool:
    return usable_rapidapi_payload(str(row.get("rapidapi_response") or ""))


def keep_people_csv_row(row: dict[str, Any]) -> bool:
    return bool(stable_linkedin_key(row)) and has_rapidapi_profile(row)


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_phone(value: str) -> str:
    phone = (value or "").strip()
    if not phone:
        return ""
    digits = re.sub(r"\D+", "", phone)
    return f"+{digits}" if phone.startswith("+") and digits else digits


def stable_source_key(row: dict[str, str]) -> str:
    for email in [row.get("primary_email", ""), *listish_values(row.get("all_emails", ""))]:
        email = normalize_email(email)
        if email:
            return f"email:{email}"
    for phone in [row.get("primary_phone", ""), *listish_values(row.get("all_phones", ""))]:
        phone = normalize_phone(phone)
        if phone:
            return f"phone:{phone}"
    handle = (row.get("twitter_handle") or "").strip().lower().lstrip("@")
    if handle:
        return f"twitter:{handle}"
    source_id = (row.get("id") or "").strip()
    if source_id and not source_id.startswith("merged:"):
        return f"id:{source_id}"
    name = normalize_name(row_name(row))
    channel = ",".join(row_source_channels(row))
    if name:
        return f"name:{sha(channel + ':' + name, 16)}"
    return f"row:{sha(json.dumps({col: row.get(col, '') for col in PEOPLE_SCHEMA_COLUMNS if col != 'source_artifacts'}, sort_keys=True), 16)}"


def discover_inputs(base: Path) -> list[Path]:
    paths_by_dir: dict[Path, Path] = {}
    for p in base.glob("network-import/*/*/people.csv"):
        paths_by_dir[p.parent] = p
    for p in base.glob("network-import/*/*/people_harmonic_all.csv"):
        paths_by_dir.setdefault(p.parent, p)
    paths = list(paths_by_dir.values())
    return sorted(set(paths))


def source_label(path: Path) -> str:
    text = str(path)
    if "/linkedin/" in text:
        return "linkedin"
    if "/twitter/" in text:
        return "twitter"
    if "/gmail/" in text:
        return "gmail"
    if "/messages/" in text:
        return "messages"
    return path.parent.name


def message_row_to_people(row: dict[str, str], path: Path) -> dict[str, str]:
    linkedin = row.get("matched_linkedin_url", "")
    full_name = row.get("matched_name") or row.get("name") or ""
    parts = full_name.split(" ", 1)
    people = {
        "id": row.get("matched_person_id") or f"message:{sha((row.get('phone') or '') + full_name)}",
        "linkedin_url": linkedin,
        "public_identifier": extract_public_identifier(linkedin),
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "full_name": full_name,
        "primary_phone": row.get("phone", ""),
        "all_phones": row.get("phone", ""),
        "source_channels": row.get("source") or "messages",
        "source_artifacts": str(path),
        "summary": f"message_count={row.get('message_count','')}; last_message={row.get('last_message','')}",
        "enrichment_provider": row.get("match_method") or "messages_contact_match",
    }
    return normalize_people_row(people)


def load_people_file(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    label = source_label(path)
    out: list[dict[str, str]] = []
    for row in rows:
        if path.name == "contacts.csv" and label == "messages":
            normalized = message_row_to_people(row, path)
        else:
            normalized = normalize_people_row(row)
            normalized["source_artifacts"] = normalized.get("source_artifacts") or str(path)
            normalized["source_channels"] = normalized.get("source_channels") or label
        out.append(normalized)
    return out


def choose(current: str, incoming: str) -> str:
    if not current and incoming:
        return incoming
    if incoming and len(incoming) > len(current) and current in {"", "[]", "{}"}:
        return incoming
    return current


def row_source_channels(row: dict[str, str]) -> list[str]:
    channels: list[str] = []
    for src in (row.get("source_channels") or "").split(","):
        src = src.strip()
        if src and src not in channels:
            channels.append(src)
    return channels or ["unknown"]


def source_identifier(row: dict[str, str], channel: str = "") -> str:
    linkedin_key = stable_linkedin_key(row)
    if linkedin_key:
        return row.get("linkedin_url") or linkedin_key
    if channel.startswith("gmail") or row.get("primary_email"):
        return row.get("primary_email") or row.get("all_emails", "")
    if channel in {"imessage", "whatsapp", "messages"} or row.get("primary_phone"):
        return row.get("primary_phone") or row.get("all_phones", "")
    if channel == "twitter" or row.get("twitter_handle"):
        return row.get("twitter_handle", "")
    return row.get("id") or row_name(row)


def network_contact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": row.get("id", ""),
        "merge_key": row.get("merge_key", ""),
        "display_name": row_name(row),
        "linkedin_url": row.get("linkedin_url", ""),
        "public_identifier": row.get("public_identifier", ""),
        "primary_email": row.get("primary_email", ""),
        "primary_phone": row.get("primary_phone", ""),
        "source_channels": row.get("source_channels", ""),
        "source_count": len([s for s in (row.get("source_channels") or "").split(",") if s.strip()]),
        "needs_review": row.get("needs_review", "false"),
    }


def normalize_company_key(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return value or "unknown"


def network_company_rows(people_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    companies: dict[str, dict[str, Any]] = {}
    for row in people_rows:
        company_name = (row.get("current_company") or "").strip()
        company_urn = (row.get("current_company_urn") or "").strip()
        if not company_name and not company_urn:
            continue
        key = company_urn or f"name:{normalize_company_key(company_name)}"
        rec = companies.setdefault(key, {
            "company_id": f"company:{sha(key, 16)}",
            "company_key": key,
            "company_name": company_name,
            "company_urn": company_urn,
            "source_channels": set(),
            "contact_ids": [],
            "contact_names": [],
        })
        if company_name and not rec.get("company_name"):
            rec["company_name"] = company_name
        if company_urn and not rec.get("company_urn"):
            rec["company_urn"] = company_urn
        for src in (row.get("source_channels") or "").split(","):
            if src.strip():
                rec["source_channels"].add(src.strip())
        contact_id = row.get("id") or row.get("merge_key") or ""
        if contact_id and contact_id not in rec["contact_ids"]:
            rec["contact_ids"].append(contact_id)
        name = row_name(row)
        if name and name not in rec["contact_names"]:
            rec["contact_names"].append(name)
    out: list[dict[str, Any]] = []
    for rec in companies.values():
        out.append({
            "company_id": rec["company_id"],
            "company_key": rec["company_key"],
            "company_name": rec.get("company_name", ""),
            "company_urn": rec.get("company_urn", ""),
            "source_channels": ",".join(sorted(rec["source_channels"])),
            "contact_count": len(rec["contact_ids"]),
            "contact_ids": json.dumps(rec["contact_ids"], ensure_ascii=False),
            "contact_names": json.dumps(rec["contact_names"], ensure_ascii=False),
        })
    out.sort(key=lambda row: (-int(row["contact_count"]), str(row.get("company_name") or row.get("company_key"))))
    return out


def source_fact_rows(contact: dict[str, Any], source_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in source_rows:
        artifacts = source_artifact_values(row.get("source_artifacts", ""))[:MAX_SOURCE_ARTIFACTS_PER_ROW] or [""]
        for channel in row_source_channels(row):
            identifier = source_identifier(row, channel)
            for artifact in artifacts:
                key = (channel, identifier, artifact)
                if key in seen:
                    continue
                seen.add(key)
                facts.append({
                    "contact_id": contact.get("id", ""),
                    "merge_key": contact.get("merge_key", ""),
                    "source_channel": channel,
                    "source_identifier": identifier,
                    "source_artifact": artifact,
                    "display_name": row_name(row),
                    "linkedin_url": row.get("linkedin_url", ""),
                    "public_identifier": row.get("public_identifier", ""),
                    "primary_email": row.get("primary_email", ""),
                    "primary_phone": row.get("primary_phone", ""),
                })
    return facts


def merge_group(key: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    merged = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
    sources: set[str] = set()
    artifacts: set[str] = set()
    for row in rows:
        for col in PEOPLE_SCHEMA_COLUMNS:
            merged[col] = choose(merged.get(col, ""), row.get(col, ""))
        for src in (row.get("source_channels") or "").split(","):
            if src.strip():
                sources.add(src.strip())
        for artifact in source_artifact_values(row.get("source_artifacts")):
            artifacts.add(artifact)
    merged["source_channels"] = ",".join(sorted(sources))
    merged["source_artifacts"] = compact_source_artifacts(sorted(artifacts))
    merged["merge_key"] = key
    merged["merge_confidence"] = "1.0" if key.startswith("linkedin:") else "0.0"
    merged["merge_sources"] = merged["source_channels"]
    merged["merged_row_count"] = len(rows)
    merged["needs_review"] = "false"
    if not merged.get("id"):
        merged["id"] = f"merged:{sha(key + row_name(merged))}"
    return merged


def build_groups(rows: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    singletons: list[dict[str, str]] = []
    for row in rows:
        key = stable_linkedin_key(row) or stable_source_key(row)
        if key:
            groups.setdefault(key, []).append(row)
        else:
            singletons.append(row)
    return groups, singletons


def similar_pairs(rows: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    review: list[dict[str, Any]] = []
    named = [(i, normalize_name(row_name(r)), r) for i, r in enumerate(rows) if normalize_name(row_name(r))]
    for i in range(len(named)):
        _, left_name, left = named[i]
        for j in range(i + 1, len(named)):
            _, right_name, right = named[j]
            if left.get("merge_key") == right.get("merge_key"):
                continue
            if not left_name or not right_name:
                continue
            ratio = difflib.SequenceMatcher(None, left_name, right_name).ratio()
            exact_parts = set(left_name.split()) == set(right_name.split()) and len(left_name.split()) >= 2
            if ratio >= threshold or exact_parts:
                left["needs_review"] = "true"
                right["needs_review"] = "true"
                review.append({
                    "left_id": left.get("id", ""),
                    "right_id": right.get("id", ""),
                    "left_name": row_name(left),
                    "right_name": row_name(right),
                    "similarity": round(ratio, 3),
                    "left_sources": left.get("source_channels", ""),
                    "right_sources": right.get("source_channels", ""),
                    "reason": "similar_name_no_shared_linkedin",
                })
    return review


def cmd_run(args: argparse.Namespace) -> int:
    if args.input:
        inputs = [Path(p) for p in args.input]
    elif args.discover and not args.no_discover:
        inputs = discover_inputs(Path(args.base_dir))
    else:
        inputs = []
    all_rows: list[dict[str, str]] = []
    per_file: dict[str, int] = {}
    for path in inputs:
        if not path.exists():
            continue
        rows = load_people_file(path)
        all_rows.extend(rows)
        per_file[str(path)] = len(rows)
    groups, singletons = build_groups(all_rows)
    merged_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        merged = merge_group(key, rows)
        merged_rows.append(merged)
        source_rows.extend(source_fact_rows(merged, rows))
    for row in singletons:
        normalized = normalize_people_row(row)
        normalized.update({
            "merge_key": stable_source_key(normalized),
            "merge_confidence": "0.0",
            "merge_sources": normalized.get("source_channels", ""),
            "merged_row_count": 1,
            "needs_review": "false",
        })
        if not normalized.get("id"):
            normalized["id"] = f"merged:{sha(normalized['merge_key'])}"
        merged_rows.append(normalized)
        source_rows.extend(source_fact_rows(normalized, [normalized]))
    unfiltered_merged_rows = len(merged_rows)
    filtered_without_linkedin = sum(1 for row in merged_rows if not stable_linkedin_key(row))
    filtered_without_rapidapi_payload = sum(1 for row in merged_rows if not has_rapidapi_profile(row))
    merged_rows = [row for row in merged_rows if keep_people_csv_row(row)]
    kept_merge_keys = {row.get("merge_key", "") for row in merged_rows}
    source_rows = [row for row in source_rows if row.get("merge_key", "") in kept_merge_keys]
    review = similar_pairs(merged_rows, args.name_threshold)
    output_dir = Path(args.output_dir)
    output = output_dir / "people.csv"
    legacy_output = output_dir / "people_harmonic_all.merged.csv"
    review_path = output_dir / "possible_duplicates_review.csv"
    network_contacts_path = output_dir / "network_contacts.csv"
    network_contact_sources_path = output_dir / "network_contact_sources.csv"
    network_companies_path = output_dir / "network_companies.csv"
    manifest = output_dir / "merge_manifest.json"
    write_csv(output, MERGED_COLUMNS, merged_rows)
    shutil.copyfile(output, legacy_output)
    write_csv(review_path, REVIEW_COLUMNS, review)
    write_csv(network_contacts_path, NETWORK_CONTACT_COLUMNS, [network_contact_row(row) for row in merged_rows])
    write_csv(network_contact_sources_path, NETWORK_CONTACT_SOURCE_COLUMNS, source_rows)
    company_rows = network_company_rows(merged_rows)
    write_csv(network_companies_path, NETWORK_COMPANY_COLUMNS, company_rows)
    manifest_payload = {
        "created_at": now_iso(),
        "inputs": per_file,
        "input_rows": len(all_rows),
        "unfiltered_merged_rows": unfiltered_merged_rows,
        "filtered_without_linkedin": filtered_without_linkedin,
        "filtered_without_rapidapi_payload": filtered_without_rapidapi_payload,
        "filtered_people_csv_rows": unfiltered_merged_rows - len(merged_rows),
        "merged_rows": len(merged_rows),
        "rapidapi_payload_rows": len(merged_rows),
        "linkedin_groups": len(groups),
        "review_pairs": len(review),
        "source_rows": len(source_rows),
        "company_rows": len(company_rows),
        "output": str(output),
        "people_csv": str(output),
        "network_contacts_csv": str(network_contacts_path),
        "network_contact_sources_csv": str(network_contact_sources_path),
        "network_companies_csv": str(network_companies_path),
        "legacy_output": str(legacy_output),
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    emit({"status": "completed", **manifest_payload, "manifest": str(manifest)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge/dedupe local network import people artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", action="append", help="Input people.csv, legacy people_harmonic_all.csv, or messages contacts.csv; repeatable. If omitted, no inputs are used unless --discover is set")
    run.add_argument("--discover", action="store_true", help="Legacy recovery mode: discover run-dir inputs from the filesystem when --input is omitted")
    run.add_argument("--no-discover", action="store_true", help="Do not discover inputs from the filesystem when --input is omitted")
    run.add_argument("--base-dir", default=".powerpacks")
    run.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run.add_argument("--name-threshold", type=float, default=0.92)
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
