#!/usr/bin/env python3
"""Amplify a real people.csv + precomputed artifacts to benchmark scale.

Runs inside the Modal sandbox with the repo at /repo. Clones real people rows
into K namespaced copies, mutating identities, role titles (with deterministic
new title_hash), and company names, then emits matching synthetic precomputed
artifacts so the full pipeline runs with ZERO paid OpenAI calls:

  - role classifications + embeddings keyed by title_hash
  - company corpus + embeddings keyed by company_name
  - summary embeddings + tech skills keyed by person_id
  - founder_enrichment / inferred_ages pre-seeds keyed by position_id / person_id

Key consistency is guaranteed by running the repo's own flatten_people() on
the synthetic CSV and deriving ids from its output.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from packs.indexing.lib.artifacts import build_company_corpus  # noqa: E402
from packs.indexing.lib.artifact_io import iter_artifact_rows  # noqa: E402
from packs.indexing.lib.people import flatten_people  # noqa: E402
from packs.indexing.modal.sandbox_common import merge_cache_file  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

CLASSIFIED_AT = "2026-06-11T00:00:00Z"


def role_hash(base_hash: str, ns: int) -> str:
    return hashlib.sha256(f"{base_hash}|r{ns}".encode()).hexdigest()[:16]


def mutate_exp(exp: dict, role_ns: int, company_ns: int) -> dict:
    out = dict(exp)
    if role_ns and out.get("title_hash"):
        out["title_hash"] = role_hash(str(exp["title_hash"]), role_ns)
        if out.get("title"):
            out["title"] = f"{exp['title']} v{role_ns}"
    if company_ns:
        for key in ("company", "company_name"):
            if out.get(key) and isinstance(out[key], str):
                out[key] = f"{exp[key]} K{company_ns}"
        if out.get("company_key"):
            out["company_key"] = f"{exp['company_key']}-k{company_ns}"
        if out.get("company_public_identifier"):
            out["company_public_identifier"] = f"{exp['company_public_identifier']}-k{company_ns}"
        if out.get("company_linkedin_url"):
            out["company_linkedin_url"] = f"{exp['company_linkedin_url']}-k{company_ns}"
    return out


def mutate_row(row: dict, clone: int, role_ns: int, company_ns: int) -> dict:
    out = dict(row)
    for key in ("id", "public_identifier", "merge_key", "entity_urn"):
        if out.get(key):
            out[key] = f"{row[key]}-c{clone}"
    if out.get("linkedin_url"):
        out["linkedin_url"] = f"{row['linkedin_url']}-c{clone}"
    if out.get("primary_email"):
        out["primary_email"] = f"c{clone}.{row['primary_email']}"
    if out.get("all_emails"):
        out["all_emails"] = ",".join(f"c{clone}.{e.strip()}" for e in row["all_emails"].split(",") if e.strip())
    if out.get("primary_phone"):
        out["primary_phone"] = f"{row['primary_phone']}{clone}"
    for key in ("last_name", "full_name"):
        if out.get(key):
            out[key] = f"{row[key]} C{clone}"
    raw = row.get("work_experiences") or ""
    if raw.strip().startswith("["):
        try:
            exps = json.loads(raw)
        except json.JSONDecodeError:
            exps = []
        if isinstance(exps, list):
            out["work_experiences"] = json.dumps(
                [mutate_exp(e, role_ns, company_ns) if isinstance(e, dict) else e for e in exps]
            )
    return out


def stream_mutate_jsonl(src: Path, dst: Path, namespaces: int, mutate) -> int:
    """Copy src jsonl to dst, appending one mutated copy per namespace 1..namespaces-1."""
    count = 0
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            fout.write(json.dumps(row) + "\n")
            count += 1
            for ns in range(1, namespaces):
                fout.write(json.dumps(mutate(row, ns)) + "\n")
                count += 1
    return count


def stream_mutate_artifact(src: Path, dst: Path, namespaces: int, mutate) -> int:
    """Stream an artifact into JSONL scratch space with namespaced copies."""
    count = 0
    with dst.open("w", encoding="utf-8") as fout:
        for row in iter_artifact_rows(src):
            fout.write(json.dumps(row) + "\n")
            count += 1
            for ns in range(1, namespaces):
                fout.write(json.dumps(mutate(row, ns)) + "\n")
                count += 1
    return count


def finish_embedding_cache(
    scratch: Path,
    destination: Path,
    key_fields: tuple[str, ...],
    vector_field: str,
) -> None:
    """Materialize one synthetic embedding cache as Parquet and remove scratch."""
    merge_cache_file(scratch, destination, key_fields, vector_field=vector_field)
    scratch.unlink()


def mutate_role_artifact(row: dict, ns: int) -> dict:
    out = dict(row)
    if out.get("title_hash"):
        out["title_hash"] = role_hash(str(row["title_hash"]), ns)
    if out.get("raw_title"):
        out["raw_title"] = f"{row['raw_title']} v{ns}"
    return out


def mutate_company_artifact(row: dict, ns: int) -> dict:
    out = dict(row)
    if out.get("company_name"):
        out["company_name"] = f"{row['company_name']} K{ns}"
    if out.get("company_urn"):
        out["company_urn"] = hashlib.sha256(f"{row['company_urn']}|k{ns}".encode()).hexdigest()[:32]
    if out.get("char_text"):
        out["char_text"] = f"{row['char_text']} K{ns}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people-csv", required=True)
    ap.add_argument("--artifacts-dir", required=True, help="dir with real precomputed artifacts")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--target-people", type=int, default=6200)
    ap.add_argument("--target-roles", type=int, default=39400)
    ap.add_argument("--target-companies", type=int, default=28800)
    args = ap.parse_args()

    src = Path(args.artifacts_dir)
    out = Path(args.output_dir)
    art_out = out / "artifacts"
    art_out.mkdir(parents=True, exist_ok=True)

    with open(args.people_csv, newline="", encoding="utf-8-sig") as f:
        reader = CsvIO.dict_reader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    base_flat = flatten_people(rows)
    base_people = len(base_flat)
    base_roles = {
        str(e.get("title_hash"))
        for p in base_flat
        for e in (p.get("work_experiences") or [])
        if isinstance(e, dict) and e.get("title_hash")
    }
    base_companies = {
        str(e.get("company_name") or e.get("company") or "").strip().lower()
        for p in base_flat
        for e in (p.get("work_experiences") or [])
        if isinstance(e, dict) and (e.get("company_name") or e.get("company"))
    }
    base_companies.discard("")

    clones = max(
        math.ceil(args.target_people / max(base_people, 1)),
        math.ceil(args.target_companies / max(len(base_companies), 1)),
        2,
    )
    role_namespaces = min(clones, max(1, round(args.target_roles / max(len(base_roles), 1))))
    company_namespaces = min(clones, max(1, round(args.target_companies / max(len(base_companies), 1))))
    print(
        f"[amplify] base people={base_people} roles={len(base_roles)} companies={len(base_companies)} "
        f"-> clones={clones} role_ns={role_namespaces} company_ns={company_namespaces}",
        flush=True,
    )

    # 1. synthetic people.csv — clone 0 is the original rows verbatim
    synth_csv = out / "people.csv"
    with synth_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for clone in range(clones):
            role_ns = clone if 0 < clone < role_namespaces else 0
            company_ns = clone if 0 < clone < company_namespaces else 0
            for row in rows:
                writer.writerow(row if clone == 0 else mutate_row(row, clone, role_ns, company_ns))
    del rows

    # 2. role + company artifacts (streamed; embeddings files are the big ones)
    n = stream_mutate_jsonl(src / "roles_with_dense_text.jsonl", art_out / "roles_with_dense_text.jsonl", role_namespaces, mutate_role_artifact)
    print(f"[amplify] role classifications rows={n}", flush=True)
    role_embedding_scratch = art_out / ".roles_with_embeddings.jsonl"
    n = stream_mutate_artifact(
        src / "roles_with_embeddings.parquet",
        role_embedding_scratch,
        role_namespaces,
        mutate_role_artifact,
    )
    finish_embedding_cache(
        role_embedding_scratch,
        art_out / "roles_with_embeddings.parquet",
        ("title_hash",),
        "dense_embedding",
    )
    print(f"[amplify] role embeddings rows={n}", flush=True)
    n = stream_mutate_jsonl(src / "companies_corpus_v3.jsonl", art_out / "companies_corpus_v3.jsonl", company_namespaces, mutate_company_artifact)
    print(f"[amplify] company corpus rows={n}", flush=True)

    # 3. flatten the synthetic csv with the repo's own code to get exact ids
    synth_flat = flatten_people(synth_csv)
    print(f"[amplify] synthetic flattened people={len(synth_flat)}", flush=True)

    # 3b. company embeddings must be keyed by the pipeline-computed company_urn
    # (uuid5 of the canonical company key), so derive them from the repo's own
    # corpus builder rather than mutating the real embeddings file.
    company_templates: list[list[float]] = []
    for row in iter_artifact_rows(src / "company_embeddings_v3.parquet", ["embedding"]):
        vec = row.get("embedding")
        if isinstance(vec, list):
            company_templates.append(vec)
        if len(company_templates) >= 64:
            break
    corpus_rows = build_company_corpus(synth_flat)
    company_embedding_scratch = art_out / ".company_embeddings_v3.jsonl"
    with company_embedding_scratch.open("w", encoding="utf-8") as f:
        for i, row in enumerate(corpus_rows):
            f.write(json.dumps({
                "company_urn": row.get("company_urn") or row.get("id"),
                "company_name": row.get("company_name", ""),
                "semantic_text": row.get("semantic_text", ""),
                "embedding": company_templates[i % len(company_templates)],
            }) + "\n")
    finish_embedding_cache(
        company_embedding_scratch,
        art_out / "company_embeddings_v3.parquet",
        ("company_urn", "company_name"),
        "embedding",
    )
    print(f"[amplify] company embeddings rows={len(corpus_rows)} (corpus-derived)", flush=True)
    del corpus_rows

    # template vectors for summary embeddings (reuse real ones round-robin)
    templates: list[list[float]] = []
    for row in iter_artifact_rows(src / "summary_embeddings.parquet", ["embedding"]):
        vec = row.get("embedding")
        if isinstance(vec, list):
            templates.append(vec)
        if len(templates) >= 64:
            break

    seeds_dir = out / "seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    unique_roles: set[str] = set()
    unique_companies: set[str] = set()
    summary_embedding_scratch = art_out / ".summary_embeddings.jsonl"
    with summary_embedding_scratch.open("w", encoding="utf-8") as emb_f, \
            (art_out / "person_tech_skills.jsonl").open("w", encoding="utf-8") as skills_f, \
            (seeds_dir / "founder_enrichment.jsonl").open("w", encoding="utf-8") as founder_f, \
            (seeds_dir / "inferred_ages.jsonl").open("w", encoding="utf-8") as ages_f:
        for i, person in enumerate(synth_flat):
            pid = str(person["id"])
            emb_f.write(json.dumps({"person_id": pid, "embedding": templates[i % len(templates)]}) + "\n")
            skills_f.write(json.dumps({"person_id": pid, "tech_skills": []}) + "\n")
            ages_f.write(json.dumps({"person_id": pid, "birth_year": "1990"}) + "\n")
            exps = [e for e in (person.get("work_experiences") or []) if isinstance(e, dict)]
            for idx, exp in enumerate(exps):
                if exp.get("title_hash"):
                    unique_roles.add(str(exp["title_hash"]))
                name = str(exp.get("company_name") or exp.get("company") or "").strip().lower()
                if name:
                    unique_companies.add(name)
                # mirror detect_ceo_founders' position_id fallback exactly
                position_id = str(exp.get("id") or exp.get("position_id") or f"{pid}-{idx}").strip()
                founder_f.write(json.dumps({
                    "person_id": pid,
                    "person_name": str(person.get("full_name") or ""),
                    "position_id": position_id,
                    "position_title": str(exp.get("title") or ""),
                    "company_name": str(exp.get("company_name") or exp.get("company") or ""),
                    "is_founder": "False",
                    "confidence": "0.0",
                    "model": "seed",
                    "reasoning": "synthetic benchmark seed",
                    "classified_at": CLASSIFIED_AT,
                }) + "\n")

    finish_embedding_cache(
        summary_embedding_scratch,
        art_out / "summary_embeddings.parquet",
        ("person_id",),
        "embedding",
    )

    summary = {
        "people": len(synth_flat),
        "unique_roles": len(unique_roles),
        "unique_companies": len(unique_companies),
        "clones": clones,
        "role_namespaces": role_namespaces,
        "company_namespaces": company_namespaces,
        "people_csv": str(synth_csv),
        "artifacts_dir": str(art_out),
        "seeds_dir": str(seeds_dir),
    }
    (out / "amplify_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
