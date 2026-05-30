#!/usr/bin/env python3
"""Bootstrap local Powerpacks artifacts from existing Aleph pipeline_output.

This is a one-time migration helper: it reuses real Aleph/DVC outputs instead of
regenerating paid enrichment. It scopes rows per operator, writes the local
record artifacts DuckDB needs, and writes checkpoint/chunk files matching the
Powerpacks checkpointed stages so future runs are monotonic/resumable.

Operator access input is required for real per-operator bootstrap. Supported
formats:
- CSV with operator_id, person_id columns (operator_email optional)
- JSONL with operator_id and person_id/base_person_id (operator_email optional)

For local smoke only, pass --operator-id and --limit without --operator-access;
this assigns the first N seed people to that operator and marks mode=smoke.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
SEED_DEFAULT = ROOT / ".powerpacks/aleph-seed/2026-05-08"
EDU_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
WORD_RE = re.compile(r"[a-z0-9]+")
_STEMMER: Any | None = None
FUNDING_STAGE_MAP = {
    "PRE_SEED": 1, "SEED": 2, "SERIES_A": 3, "SERIES_B": 4, "SERIES_C": 5,
    "SERIES_D": 6, "SERIES_E": 7, "SERIES_F": 8, "SERIES_G": 9, "SERIES_H": 10,
    "SERIES_I": 11, "LATE_STAGE": 50, "IPO": 90, "PUBLIC": 91, "EXITED": 99,
    "VENTURE_UNKNOWN": 0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def listify(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [value]
    return [value]


def word_tokenize(text: str) -> list[str]:
    tokens = WORD_RE.findall((text or "").lower())
    return tokens + [f"{tokens[idx]} {tokens[idx + 1]}" for idx in range(len(tokens) - 1)]


def char_tokenize(text: str, min_n: int = 3, max_n: int = 5) -> list[str]:
    result: list[str] = []
    for word in (text or "").lower().split():
        padded = f" {word} "
        for n in range(min_n, max_n + 1):
            for idx in range(len(padded) - n + 1):
                result.append(padded[idx : idx + n])
    return result


def phrase_tokenize(text: str, *, max_source_tokens: int = 256, max_ngram: int = 4) -> list[str]:
    global _STEMMER
    raw = WORD_RE.findall((text or "").lower())[:max_source_tokens]
    try:
        if _STEMMER is None:
            import snowballstemmer  # type: ignore
            _STEMMER = snowballstemmer.stemmer("english")
        stems = [_STEMMER.stemWord(token) for token in raw]
    except Exception:
        stems = raw
    tokens: list[str] = []
    for n in range(1, min(max_ngram, len(stems)) + 1):
        for idx in range(len(stems) - n + 1):
            tokens.append(" ".join(stems[idx : idx + n]))
    return list(dict.fromkeys(tokens))


def epoch(value: Any) -> int:
    text = clean(value)
    if not text:
        return 0
    try:
        return max(0, int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()))
    except Exception:
        match = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", text)
        if not match:
            return 0
        try:
            month = int(match.group(2) or 1)
            day = int(match.group(3) or 1)
            return max(0, int(datetime(int(match.group(1)), month, day).timestamp()))
        except Exception:
            return 0


def resolve_title_hash(row: dict[str, Any]) -> str:
    direct = clean(row.get("title_hash"))
    if not direct:
        raise RuntimeError(f"missing upstream title_hash for position {clean(row.get('id')) or '<unknown>'}; bootstrap must copy existing DVC/Aleph checkpoints, not recompute hashes")
    return direct


def parse_customer_type(value: Any) -> list[str]:
    text = clean(value)
    return [code for code in ["B2B", "B2C", "B2G"] if code in text]


def parse_date_int(value: Any) -> int:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", clean(value))
    return int(match.group(1)) * 10000 + int(match.group(2)) * 100 + int(match.group(3)) if match else 0


def to_int(value: Any) -> int:
    try:
        return int(float(clean(value) or 0))
    except Exception:
        return 0


def to_float(value: Any) -> float:
    try:
        return float(clean(value) or 0)
    except Exception:
        return 0.0


def stage_int(value: Any) -> int:
    return FUNDING_STAGE_MAP.get(clean(value).upper(), 0)


def slugify(value: Any) -> str:
    text = clean(value).lower()
    text = re.sub(r"^https?://(www\.)?", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def linkedin_slug(url: Any, kind: str) -> str:
    text = clean(url).lower().rstrip("/")
    match = re.search(rf"linkedin\.com/(?:[^/]+/)?{kind}/([^/?#]+)", text)
    if not match:
        match = re.search(rf"linkedin\.com/{kind}/([^/?#]+)", text)
    return match.group(1) if match else ""


def company_local_id(row: dict[str, Any]) -> str:
    slug = linkedin_slug(row.get("linkedin_url"), "company")
    if slug:
        return f"linkedin:company:{slug}"
    domain = slugify(row.get("website_domain"))
    if domain != "unknown":
        return f"domain:{domain}"
    return f"company-name:{slugify(row.get('company_name'))}"


def school_local_id(row: dict[str, Any]) -> str:
    slug = linkedin_slug(row.get("linkedin_url"), "school")
    if slug:
        return f"linkedin:school:{slug}"
    return f"school-name:{slugify(row.get('school_name'))}"


def load_birth_years(path: Path, person_ids: set[str]) -> tuple[dict[str, int], int]:
    out: dict[str, int] = {}; scanned = 0
    for row in read_jsonl(path):
        scanned += 1
        pid = clean(row.get("person_id"))
        if pid in person_ids:
            try:
                out[pid] = int(row.get("birth_year") or row.get("inferred_birth_year"))
            except Exception:
                pass
            if len(out) >= len(person_ids):
                break
    return out, scanned


def load_founder_position_ids(path: Path, position_ids: set[str]) -> tuple[set[str], int]:
    out: set[str] = set(); scanned = 0
    for row in read_jsonl(path):
        scanned += 1
        pid = clean(row.get("position_id"))
        if pid in position_ids:
            try:
                if row.get("is_founder") and float(row.get("confidence") or 0) >= 0.7:
                    out.add(pid)
            except Exception:
                pass
            if len(out) >= len(position_ids):
                break
    return out, scanned


def load_people_education(path: Path, person_ids: set[str], limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []; scanned = 0
    for row in read_jsonl(path):
        scanned += 1
        if clean(row.get("person_id")) in person_ids:
            out.append(row)
            if limit and len(out) >= limit:
                break
    return out, scanned


def load_schools(path: Path, school_ids: set[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str], int]:
    schools: dict[str, dict[str, Any]] = {}; parent: dict[str, str] = {}; scanned = 0
    wanted = set(school_ids)
    for row in read_jsonl(path):
        scanned += 1
        urn = clean(row.get("entity_urn"))
        if not urn:
            continue
        dm = row.get("duplicate_metadata") if isinstance(row.get("duplicate_metadata"), dict) else {}
        for child in dm.get("children") or []:
            if isinstance(child, dict) and child.get("urn"):
                child_urn = clean(child.get("urn")); parent[child_urn] = urn
                if child_urn in school_ids:
                    wanted.add(urn)
        if urn in wanted:
            schools[urn] = row
        if {parent.get(s, s) for s in school_ids} <= set(schools):
            break
    return schools, parent, scanned


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return count


def chunked(rows: list[dict[str, Any]], size: int) -> Iterable[tuple[int, list[dict[str, Any]]]]:
    size = max(1, int(size))
    for idx, start in enumerate(range(0, len(rows), size), start=1):
        yield idx, rows[start : start + size]


def write_stage_chunks(root: Path, prefix: str, rows: list[dict[str, Any]], checkpoint_every: int, id_key: str, provider: str, output: Path) -> dict[str, Any]:
    chunks_dir = root / "chunks"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for idx, batch in chunked(rows, checkpoint_every):
        write_jsonl(chunks_dir / f"{prefix}.{idx:06d}.jsonl", batch)
    checkpoint = {
        "status": "completed",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "completed_at": now_iso(),
        "provider": provider,
        "checkpoint_every": checkpoint_every,
        "input_rows_processed": len(rows),
        "chunks_written": (len(rows) + checkpoint_every - 1) // checkpoint_every if rows else 0,
        "output": str(output),
        "ids": [clean(row.get(id_key)) for row in rows if clean(row.get(id_key))],
    }
    write_json(root / "checkpoint.json", checkpoint)
    return checkpoint


def load_operator_access(path: Path | None) -> tuple[dict[str, set[str]], dict[str, str]]:
    by_operator: dict[str, set[str]] = defaultdict(set)
    emails: dict[str, str] = {}
    if path is None:
        return by_operator, emails
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                op = clean(row.get("operator_id"))
                pid = clean(row.get("person_id") or row.get("base_person_id"))
                if op and pid:
                    by_operator[op].add(pid)
                    if clean(row.get("operator_email")):
                        emails[op] = clean(row.get("operator_email"))
        return by_operator, emails
    for row in read_jsonl(path):
        op = clean(row.get("operator_id"))
        pid = clean(row.get("person_id") or row.get("base_person_id"))
        if op and pid:
            by_operator[op].add(pid)
            if clean(row.get("operator_email")):
                emails[op] = clean(row.get("operator_email"))
    return by_operator, emails


def select_flattened(flattened_path: Path, person_ids: set[str], limit: int | None) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    scanned = 0
    for row in read_jsonl(flattened_path):
        scanned += 1
        pid = clean(row.get("base_person_id"))
        if (person_ids and pid in person_ids) or (not person_ids and (limit is None or len(selected) < limit)):
            selected.append(row)
        if not person_ids and limit is not None and len(selected) >= limit:
            break
    return selected, scanned


def load_by_ids(path: Path, ids: set[str], key: str) -> tuple[dict[str, dict[str, Any]], int]:
    out: dict[str, dict[str, Any]] = {}
    scanned = 0
    if not ids:
        return out, scanned
    remaining = set(ids)
    for row in read_jsonl(path):
        scanned += 1
        rid = clean(row.get(key))
        if rid in remaining:
            out[rid] = row
            remaining.remove(rid)
            if not remaining:
                break
    return out, scanned


def iter_csv_dicts(path: Path) -> Iterable[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open("rb") as raw:
        text = (line.decode("utf-8-sig", "replace").replace("\x00", "") for line in raw)
        for row in csv.DictReader(text):
            yield {str(k): (v or "") for k, v in row.items() if k is not None}


def load_csv_by_ids(path: Path, ids: set[str]) -> tuple[dict[str, dict[str, Any]], int]:
    out: dict[str, dict[str, Any]] = {}
    scanned = 0
    remaining = set(ids)
    for row in iter_csv_dicts(path):
        scanned += 1
        rid = clean(row.get("id") or row.get("person_id") or row.get("base_person_id"))
        if rid in remaining:
            out[rid] = row
            remaining.remove(rid)
            if not remaining:
                break
    return out, scanned


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(clean(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def parse_website_domain(value: Any) -> str:
    website = parse_json_object(value)
    if website:
        return clean(website.get("domain") or website.get("url"))
    return clean(value)


def load_company_harmonic_csv(path: Path, ids: set[str]) -> tuple[dict[str, dict[str, Any]], int]:
    out: dict[str, dict[str, Any]] = {}
    linkedin_by_name: dict[str, str] = {}
    domain_by_name: dict[str, str] = {}
    scanned = 0
    if not path.exists() or not ids:
        return out, scanned
    for row in iter_csv_dicts(path):
        scanned += 1
        name_key = slugify(row.get("company_name"))
        if clean(row.get("linkedin_url")) and name_key != "unknown":
            linkedin_by_name.setdefault(name_key, clean(row.get("linkedin_url")))
        domain = parse_website_domain(row.get("website"))
        if domain and name_key != "unknown":
            domain_by_name.setdefault(name_key, domain)
        urn = clean(row.get("company_urn"))
        if urn not in ids:
            continue
        out[urn] = {
            "company_urn": urn,
            "company_name": clean(row.get("company_name")),
            "original_name": clean(row.get("company_name")),
            "name_aliases": listify(row.get("name_aliases")),
            "description": clean(row.get("description") or row.get("short_description")),
            "city": clean(row.get("city")),
            "state": clean(row.get("state")),
            "country": clean(row.get("country")),
            "metro_area": " ".join(clean(v) for v in listify(row.get("metro_areas")) if clean(v)),
            "headcount": row.get("headcount"),
            "founded_year": row.get("founded_year"),
            "linkedin_url": clean(row.get("linkedin_url")),
            "logo_url": clean(row.get("logo_url")),
            "website_domain": domain,
            "customer_type": clean(row.get("customer_type")),
            "ownership_status": clean(row.get("ownership_status")),
            "company_type": clean(row.get("company_type")),
            "investor_urns": [clean(v) for v in listify(row.get("investor_urn")) if clean(v)],
        }
    for row in out.values():
        name_key = slugify(row.get("company_name"))
        if not clean(row.get("linkedin_url")) and name_key in linkedin_by_name:
            row["linkedin_url"] = linkedin_by_name[name_key]
        if not clean(row.get("website_domain")) and name_key in domain_by_name:
            row["website_domain"] = domain_by_name[name_key]
    return out, scanned


def merge_company_identity(identity: dict[str, Any], enrichment: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(enrichment or {})
    for key, value in identity.items():
        if value not in (None, "", []):
            row[key] = value
    return row


def role_dense_from_embedding_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key, [] if key in {"doc2query", "inferred_skills", "role_ids"} else "") for key in ["title_hash", "raw_title", "description", "cluster", "role_ids", "seniority_band", "role_type", "role_track", "specialization", "doc2query", "inferred_skills", "dense_text"]}


def build_operator(args: argparse.Namespace, operator_id: str, person_ids: set[str], operator_email: str | None, mode: str) -> dict[str, Any]:
    seed_root = Path(args.seed)
    seed = seed_root / "pipeline_output"
    company_csv_path = Path(args.company_csv) if args.company_csv else seed_root / "data/company_harmonic_all.csv"
    output_root = Path(args.output_dir)
    run_dir = output_root if getattr(args, "_single_operator", False) else output_root / f"operator-{operator_id[:8]}"
    if run_dir.exists() and args.force:
        shutil.rmtree(run_dir)
    elif run_dir.exists() and not args.force:
        raise SystemExit(f"output exists: {run_dir}. Use --force to replace")
    records_dir = run_dir / "records"
    stats_dir = run_dir / "stats"
    records_dir.mkdir(parents=True, exist_ok=True)
    stats_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    flattened, flattened_scanned = select_flattened(seed / "unified/flattened_people.jsonl", person_ids, args.limit if mode == "smoke" else None)
    selected_person_ids = {clean(row.get("base_person_id")) for row in flattened if clean(row.get("base_person_id"))}
    title_hashes = {resolve_title_hash(row) for row in flattened}
    company_ids = {clean(row.get("company_id")) for row in flattened if clean(row.get("company_id"))}
    position_ids = {clean(row.get("id")) for row in flattened if clean(row.get("id"))}

    role_dense, role_dense_scanned = load_by_ids(seed / "unified/roles/roles_with_dense_text_remapped.jsonl", title_hashes, "title_hash")
    role_embeddings, role_emb_scanned = load_by_ids(seed / "unified/roles/roles_with_embeddings.jsonl", title_hashes, "title_hash")
    for th, emb in role_embeddings.items():
        role_dense.setdefault(th, role_dense_from_embedding_row(emb))
    role_dense_rows = [role_dense[th] for th in sorted(role_dense)]
    role_embedding_rows = [role_embeddings[th] for th in sorted(role_embeddings)]

    write_jsonl(run_dir / "roles/roles_with_dense_text_remapped.jsonl", role_dense_rows)
    shutil.copy2(run_dir / "roles/roles_with_dense_text_remapped.jsonl", run_dir / "roles/roles_with_dense_text.jsonl")
    write_jsonl(run_dir / "unified/roles/roles_with_dense_text_remapped.jsonl", role_dense_rows)
    write_jsonl(run_dir / "roles/roles_with_embeddings.jsonl", role_embedding_rows)
    write_jsonl(run_dir / "unified/roles/roles_with_embeddings.jsonl", role_embedding_rows)
    write_jsonl(run_dir / "roles/raw_titles.jsonl", ({"title_hash": r.get("title_hash"), "raw_title": r.get("raw_title", ""), "description": r.get("description", "")} for r in role_dense_rows))
    with (run_dir / "roles/role_mapping.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title_hash", "raw_title", "expanded_title", "seniority_band", "role_track"])
        writer.writeheader()
        for row in role_dense_rows:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames or []})
    write_stage_chunks(run_dir / "roles", "roles", role_dense_rows, args.checkpoint_every, "title_hash", "input-classifications", run_dir / "roles/roles_with_dense_text_remapped.jsonl")
    write_stage_chunks(run_dir / "roles/embedding_checkpoints", "embeddings", [{"id": r.get("title_hash"), "embedding": r.get("dense_embedding"), **{k: v for k, v in r.items() if k != "dense_embedding"}} for r in role_embedding_rows], args.checkpoint_every, "id", "input-embeddings", run_dir / "roles/roles_with_embeddings.jsonl")

    company_corpus, company_scanned = load_by_ids(seed / "company/companies_corpus_v3.jsonl", company_ids, "company_urn")
    company_harmonic, company_harmonic_scanned = load_company_harmonic_csv(company_csv_path, company_ids)
    company_embeddings, company_emb_scanned = load_by_ids(seed / "company/company_embeddings_v3.jsonl", company_ids, "company_urn")
    company_source = {old_id: merge_company_identity(company_harmonic.get(old_id, {}), company_corpus.get(old_id)) for old_id in sorted(company_ids) if company_harmonic.get(old_id) or company_corpus.get(old_id)}
    company_id_map = {old_id: company_local_id(row) for old_id, row in company_source.items()}
    company_rows_by_local: dict[str, dict[str, Any]] = {}
    for old_id in sorted(company_source):
        local_id = company_id_map[old_id]
        row = dict(company_source[old_id])
        row["company_urn"] = local_id
        row["investor_urns"] = [company_id_map[v] for v in (clean(item) for item in listify(row.get("investor_urns"))) if v in company_id_map]
        if local_id not in company_rows_by_local or (clean(row.get("linkedin_url")) and not clean(company_rows_by_local[local_id].get("linkedin_url"))):
            company_rows_by_local[local_id] = row
    company_rows_seed = [company_rows_by_local[key] for key in sorted(company_rows_by_local)]
    company_embedding_by_local: dict[str, dict[str, Any]] = {}
    for old_id in sorted(company_embeddings):
        if old_id not in company_id_map:
            continue
        local_id = company_id_map[old_id]
        row = dict(company_embeddings[old_id])
        row["company_urn"] = local_id
        company_embedding_by_local[local_id] = row
    company_embedding_rows = [company_embedding_by_local[key] for key in sorted(company_embedding_by_local)]
    write_jsonl(run_dir / "company/companies_corpus_v3.jsonl", company_rows_seed)
    write_jsonl(run_dir / "company/company_embeddings_v3.jsonl", company_embedding_rows)
    write_stage_chunks(run_dir / "company/enrichment_checkpoints", "companies", company_rows_seed, args.checkpoint_every, "company_urn", "input-classifications", run_dir / "company/companies_corpus_v3.jsonl")
    write_stage_chunks(run_dir / "company/embedding_checkpoints", "embeddings", [{"id": r.get("company_urn"), "embedding": r.get("embedding"), **r} for r in company_embedding_rows], args.checkpoint_every, "id", "input-embeddings", run_dir / "company/company_embeddings_v3.jsonl")

    unified_rows, unified_scanned = load_csv_by_ids(seed / "unified/unified_person.csv", selected_person_ids)
    summary_embeddings, summary_emb_scanned = load_by_ids(seed / "unified/summary_embeddings.jsonl", selected_person_ids, "person_id")
    tech_skills, tech_scanned = load_by_ids(seed / "unified/person_tech_skills.jsonl", selected_person_ids, "person_id")
    write_jsonl(run_dir / "unified/flattened_people.jsonl", flattened)
    write_jsonl(run_dir / "unified/summary_embeddings.jsonl", [summary_embeddings[pid] for pid in sorted(summary_embeddings)])
    write_jsonl(run_dir / "unified/person_tech_skills.jsonl", [tech_skills[pid] for pid in sorted(tech_skills)])
    write_stage_chunks(run_dir / "summaries/embedding_checkpoints", "embeddings", [{"id": r.get("person_id"), "embedding": r.get("embedding"), **r} for r in summary_embeddings.values()], args.checkpoint_every, "id", "input-embeddings", run_dir / "unified/summary_embeddings.jsonl")
    with (run_dir / "unified/unified_person.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = sorted({k for row in unified_rows.values() for k in row}) or ["id"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for pid in sorted(unified_rows):
            writer.writerow(unified_rows[pid])

    ages, _ = load_birth_years(seed / "unified/inferred_ages.jsonl", selected_person_ids)
    founders, _ = load_founder_position_ids(seed / "unified/roles/founder_enrichment.jsonl", position_ids)

    def people_records() -> Iterable[dict[str, Any]]:
        for row in flattened:
            th = resolve_title_hash(row)
            dense = role_dense.get(th) or {}
            emb = role_embeddings.get(th) or {}
            position = row.get("position") if isinstance(row.get("position"), dict) else {}
            raw_title = clean(row.get("raw_title") or position.get("title"))
            description = clean(dense.get("description") or row.get("description") or position.get("description") or position.get("summary"))
            dense_text = clean(dense.get("dense_text") or " ".join(part for part in [raw_title, clean(row.get("company_name") or position.get("company_name") or position.get("company")), description] if part))
            role_track = clean(dense.get("role_track") or row.get("role_track"))
            role_ids = [clean(v) for v in listify(dense.get("role_ids") or row.get("role_ids")) if clean(v)]
            d2q_parts = [clean(v) for v in listify(dense.get("doc2query")) + listify(dense.get("inferred_skills")) if clean(v)]
            if role_track:
                d2q_parts.append(role_track)
            if clean(row.get("id")) in founders and "founder" not in role_ids:
                role_ids.append("founder")
            base_id = clean(row.get("base_person_id"))
            word_text = f"{raw_title} {description} {clean(dense.get('seniority_band') or row.get('seniority_band'))}".strip()
            yield {
                "id": clean(row.get("id")), "position_id": clean(row.get("id")), "person_id": base_id, "base_id": base_id,
                "vector": emb.get("dense_embedding"), "position_title": raw_title, "description": description, "dense_text": dense_text,
                "word_tokens": word_tokenize(word_text), "char_tokens": char_tokenize(word_text), "d2q_tokens": word_tokenize(" ".join(d2q_parts)), "phrase_tokens": phrase_tokenize(word_text),
                "seniority_band": clean(dense.get("seniority_band") or row.get("seniority_band")), "company_id": company_id_map.get(clean(row.get("company_id")), ""),
                "company_domain": clean(position.get("company_domain") or position.get("domain") or position.get("website_domain")),
                "company_linkedin_url": clean(position.get("company_linkedin_url") or position.get("linkedin_url") or position.get("company_url")),
                "company_description": clean(position.get("company_description")),
                "company_sector_types": listify(position.get("company_sector_types")),
                "company_entity_types": listify(position.get("company_entity_types")),
                "company_headcount": int(position.get("company_headcount") or 0),
                "company_funding_total": float(position.get("company_funding_total") or 0),
                "company_stage": clean(position.get("company_stage") or position.get("stage")),
                "investor_names": listify(position.get("investor_names")),
                "city": clean(row.get("city")), "state": clean(row.get("state")), "country": clean(row.get("country")), "macro_region": clean(row.get("macro_region")),
                "is_current": bool(row.get("is_current")), "total_years_experience": float(row.get("total_years_experience") or 0),
                "start_date_epoch": epoch(row.get("start_date")), "end_date_epoch": epoch(row.get("end_date")), "tenure_years": float(row.get("tenure_years") or 0),
                "inferred_birth_year": ages.get(base_id, int(row.get("inferred_birth_year") or 0)), "role_track": role_track, "role_type_category": clean(row.get("role_type_category")),
                "metro_areas": listify(row.get("metro_areas")), "allowed_operator_ids": [operator_id], "role_ids": role_ids, "title_hash": th, "raw_title": raw_title,
            }

    people_count = write_jsonl(records_dir / "people.records.jsonl", people_records())

    def summary_records() -> Iterable[dict[str, Any]]:
        for pid in sorted(selected_person_ids):
            summary = clean((unified_rows.get(pid) or {}).get("summary"))
            emb = summary_embeddings.get(pid, {}).get("embedding")
            if summary and emb:
                yield {"id": pid, "person_id": pid, "base_id": pid, "summary": summary, "word_tokens": word_tokenize(summary), "phrase_tokens": phrase_tokenize(summary), "tech_skills": (tech_skills.get(pid) or {}).get("tech_skills", []), "allowed_operator_ids": [operator_id], "vector": emb}

    summaries_count = write_jsonl(records_dir / "summaries.records.jsonl", summary_records())

    def company_records() -> Iterable[dict[str, Any]]:
        for local_urn in sorted(company_rows_by_local):
            row = company_rows_by_local.get(local_urn); emb = company_embedding_by_local.get(local_urn, {}).get("embedding")
            if not row or emb is None:
                continue
            name = clean(row.get("company_name")); aliases = [clean(v) for v in listify(row.get("name_aliases")) if clean(v)]
            d2q = clean(row.get("d2q_text")) or " ".join(clean(v) for v in listify(row.get("doc2query")) if clean(v))
            yield {"id": local_urn, "company_urn": local_urn, "vector": emb, "company_name": name, "aliases": aliases, "name_aliases_text": " ".join([name, *aliases]).strip(), "semantic_text": clean(row.get("semantic_text")), "entity_sector_text": clean(row.get("word_text")), "word_text": clean(row.get("word_text")), "doc2query_text": d2q, "doc2query": listify(row.get("doc2query")), "description": clean(row.get("description")), "city": clean(row.get("city")), "state": clean(row.get("state")), "country": clean(row.get("country")), "metro_area": clean(row.get("metro_area")), "macro_region": clean(row.get("macro_region")), "headcount": to_int(row.get("headcount")), "funding_stage": stage_int(row.get("funding_stage")), "funding_total": to_float(row.get("funding_total")), "last_funding_at": parse_date_int(row.get("last_funding_at")), "valuation": to_float(row.get("valuation")), "founded_year": to_int(row.get("founded_year")), "investor_urns": [company_id_map[v] for v in (clean(item) for item in listify(row.get("investor_urns"))) if v in company_id_map], "customer_type": parse_customer_type(row.get("customer_type")), "entity_types": listify(row.get("entity_types")), "sector_types": listify(row.get("sector_types")), "technology_types": listify(row.get("technology_types")), "accelerators": listify(row.get("accelerators")), "yc_batches": listify(row.get("yc_batches")), "linkedin_url": clean(row.get("linkedin_url")), "logo_url": clean(row.get("logo_url")), "website_domain": clean(row.get("website_domain")), "allowed_operator_ids": [operator_id]}

    companies_count = write_jsonl(records_dir / "companies.records.jsonl", company_records())

    people_education, people_education_scanned = load_people_education(seed / "education/people_education.jsonl", selected_person_ids, args.education_limit)
    school_ids = {clean(row.get("education_id")) for row in people_education if clean(row.get("education_id"))}
    schools, parent, schools_scanned = load_schools(seed / "education/schools_corpus.jsonl", school_ids)
    school_id_map = {old_id: school_local_id(row) for old_id, row in schools.items()}
    for child, canonical in parent.items():
        if canonical in school_id_map:
            school_id_map[child] = school_id_map[canonical]
    schools_count = write_jsonl(records_dir / "schools.records.jsonl", _school_rows(schools, parent, school_ids, school_id_map, operator_id))
    education_count = write_jsonl(records_dir / "education.records.jsonl", _education_rows(people_education, schools, parent, school_id_map, operator_id))

    duckdb_payload = run_duckdb_loader(run_dir, operator_id, operator_email or "") if not args.skip_duckdb else {}
    vector_counts = {"people": people_count, "summaries": summaries_count, "companies": companies_count}
    write_json(run_dir / "vectors/checkpoint.json", {"status": "completed", "provider": "input-embeddings", "dimension": 1536, "counts": vector_counts, "updated_at": now_iso()})

    stats = {"status": "ok", "mode": mode, "operator_id": operator_id, "operator_email": operator_email, "run_dir": str(run_dir), "id_scheme": "company_harmonic_csv_linkedin_first_no_harmonic_ids", "company_source": str(company_csv_path), "counts": {"people_records": people_count, "summaries_records": summaries_count, "companies_records": companies_count, "education_records": education_count, "schools_records": schools_count, "selected_people": len(selected_person_ids), "selected_positions": len(flattened), "roles": len(role_dense_rows), "role_embeddings": len(role_embedding_rows), "companies_from_company_harmonic_csv": len(company_harmonic), "companies_with_enrichment_cache": len(company_corpus)}, "id_backfill_needed": {"companies_without_linkedin_url": sum(1 for row in company_source.values() if not clean(row.get("linkedin_url"))), "schools_without_linkedin_url": sum(1 for row in schools.values() if not clean(row.get("linkedin_url")))}, "scanned_rows": {"flattened_people": flattened_scanned, "roles_dense": role_dense_scanned, "role_embeddings": role_emb_scanned, "companies_corpus_enrichment_cache": company_scanned, "company_harmonic_all_csv": company_harmonic_scanned, "company_embeddings": company_emb_scanned, "unified_person_csv": unified_scanned, "summary_embeddings": summary_emb_scanned, "tech_skills": tech_scanned, "people_education": people_education_scanned, "schools": schools_scanned}, "duckdb": duckdb_payload.get("duckdb"), "duckdb_tables": duckdb_payload.get("tables", {}), "elapsed_seconds": round(time.time() - started, 3)}
    write_json(stats_dir / "bootstrap_from_aleph.json", stats)
    return stats


def _school_rows(schools: dict[str, dict[str, Any]], parent: dict[str, str], school_ids: set[str], school_id_map: dict[str, str], operator_id: str) -> Iterable[dict[str, Any]]:
    emitted: set[str] = set()
    for old_sid in sorted({parent.get(sid, sid) for sid in school_ids}):
        row = schools.get(old_sid)
        new_sid = school_id_map.get(old_sid, "")
        if not row or not new_sid or new_sid in emitted:
            continue
        emitted.add(new_sid)
        name = clean(row.get("school_name"))
        yield {"id": new_sid, "canonical_education_id": new_sid, "school_name": name, "school_name_tokens": word_tokenize(name), "display_value": name, "person_count": int(row.get("person_count") or 0), "linkedin_url": clean(row.get("linkedin_url")), "logo_url": clean(row.get("logo_url")), "allowed_operator_ids": [operator_id]}


def _education_rows(people_education: list[dict[str, Any]], schools: dict[str, dict[str, Any]], parent: dict[str, str], school_id_map: dict[str, str], operator_id: str) -> Iterable[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    for row in people_education:
        person_id = clean(row.get("person_id")); education_id = clean(row.get("education_id"))
        if not person_id or not education_id or (person_id, education_id) in seen:
            continue
        seen.add((person_id, education_id))
        canonical = parent.get(education_id, education_id)
        school = schools.get(canonical) or schools.get(education_id) or {}
        local_education_id = school_id_map.get(education_id) or school_id_map.get(canonical) or f"school-name:{slugify(row.get('school_name'))}"
        local_canonical_id = school_id_map.get(canonical) or local_education_id
        yield {"id": str(uuid.uuid5(EDU_NAMESPACE, f"pe:{person_id}:{local_education_id}")), "person_id": person_id, "base_id": person_id, "education_id": local_education_id, "canonical_education_id": local_canonical_id, "school_name": clean(school.get("school_name") or row.get("school_name")), "degree": clean(row.get("degree")), "degree_normalized": clean(row.get("degree_normalized")), "field_of_study": clean(row.get("field_of_study")), "start_year": int(row.get("start_year") or 0), "end_year": int(row.get("end_year") or 0), "graduation_year": int(row.get("graduation_year") or 0), "allowed_operator_ids": [operator_id]}


def run_duckdb_loader(run_dir: Path, operator_id: str, operator_email: str) -> dict[str, Any]:
    cmd = [sys.executable, "scripts/build-local-duckdb-shim.py", "--records-dir", str(run_dir / "records"), "--operator-id", operator_id, "--operator-email", operator_email or f"{operator_id}@local", "--force"]
    completed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, file=sys.stderr, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        raise SystemExit(completed.returncode)
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=str(SEED_DEFAULT), help="Aleph seed root containing pipeline_output/ and data/")
    parser.add_argument("--company-csv", help="Primary collected company_harmonic_all.csv source; defaults to <seed>/data/company_harmonic_all.csv")
    parser.add_argument("--operator-access", help="CSV/JSONL operator_id/person_id mapping for real per-operator scoping")
    parser.add_argument("--operator-id", help="Single operator id. With --operator-access, filters to this operator. Without access, smoke-selects --limit people.")
    parser.add_argument("--operator-email")
    parser.add_argument("--output-dir", default=".powerpacks/search-index")
    parser.add_argument("--limit", type=int, default=5, help="Smoke row limit when --operator-access is omitted")
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--education-limit", type=int, default=5000)
    parser.add_argument("--skip-duckdb", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    access_path = Path(args.operator_access) if args.operator_access else None
    by_operator, emails = load_operator_access(access_path)
    mode = "operator_access" if by_operator else "smoke"
    if args.operator_id:
        selected_ids = by_operator.get(args.operator_id, set())
        if by_operator and not selected_ids:
            raise SystemExit(f"operator access has no rows for operator_id {args.operator_id}")
        operators = [(args.operator_id, selected_ids, args.operator_email or emails.get(args.operator_id))]
    else:
        operators = [(op, ids, emails.get(op)) for op, ids in sorted(by_operator.items())]
    if not operators:
        raise SystemExit("provide --operator-access or --operator-id for smoke bootstrap")
    args._single_operator = len(operators) == 1
    results = [build_operator(args, op, ids, email, mode) for op, ids, email in operators]
    emit({"status": "ok", "mode": mode, "operators": len(results), "results": results})


if __name__ == "__main__":
    main()
