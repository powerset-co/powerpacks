"""Derive parent display bundles without removing contact evidence."""
from __future__ import annotations

from pathlib import Path

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis.selection import effective_parent_bundles


def normalize_cached_bundles(db: Db, out_dir: Path) -> int:
    """Parent bundles are display inputs; contacts retain their own artifacts."""
    out_dir = Path(out_dir) / "parents"
    bundles = effective_parent_bundles(db)
    if not bundles:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for parent_id, bundle in bundles.items():
        path = out_dir / f"{parent_id}.json"
        write_json(path, bundle.to_payload())
        project_parent_source_bundle(db, path, parent_id)
    return len(bundles)
