#!/usr/bin/env python3
"""Merge uploaded cache payloads from /data/incoming into the shared cache.

Runs inside a sandbox after `linkedin_modal_pipeline.py preload` uploads
payloads. JSONL classification artifacts and Parquet embedding artifacts are
key-union merged (rows for keys already in the cache are equivalent content,
so either side winning is fine; nothing is ever dropped). A
profile_cache_v2.tar.gz is extracted and copied file-by-file only where absent.
Incoming payloads are removed after a successful merge.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/repo")
sys.path.insert(0, str(REPO))

from packs.indexing.modal.sandbox_common import merge_cache_file, merge_file_dir  # noqa: E402

INCOMING = Path("/data/incoming")

TARGETS = {
    "roles_with_dense_text.jsonl": ("artifacts/roles_with_dense_text.jsonl", ("title_hash",)),
    "roles_with_embeddings.parquet": ("artifacts/roles_with_embeddings.parquet", ("title_hash",)),
    "companies_corpus_v3.jsonl": ("artifacts/companies_corpus_v3.jsonl", ("company_urn", "company_name")),
    "company_embeddings_v3.parquet": ("artifacts/company_embeddings_v3.parquet", ("company_urn", "company_name")),
    "summary_embeddings.parquet": ("artifacts/summary_embeddings.parquet", ("person_id",)),
    "person_tech_skills.jsonl": ("artifacts/person_tech_skills.jsonl", ("person_id",)),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-root", default="/data/cache")
    args = ap.parse_args()
    cache_root = Path(args.cache_root)

    for name, (rel_cache, keys) in TARGETS.items():
        src = INCOMING / name
        if not src.exists():
            continue
        new_count, kept_count = merge_cache_file(src, cache_root / rel_cache, keys)
        print(f"[merge-incoming] {rel_cache}: {new_count} incoming + {kept_count} existing kept", flush=True)
        src.unlink()

    tarball = INCOMING / "profile_cache_v2.tar.gz"
    if tarball.exists():
        staging = Path("/tmp/profile_cache_incoming")
        staging.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-xzf", str(tarball), "-C", str(staging)], check=True)
        added, existing = merge_file_dir(staging, cache_root / "profile_cache_v2")
        print(f"[merge-incoming] profile_cache_v2: {added} added, {existing} already present", flush=True)
        shutil.rmtree(staging)
        tarball.unlink()

    print("[merge-incoming] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
