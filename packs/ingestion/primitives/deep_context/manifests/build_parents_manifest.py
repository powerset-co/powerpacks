"""Manifest emitted by incremental parent merge and dossier projection."""

from packs.ingestion.primitives.pipeline.contract import StageManifest


class BuildParentsManifest(StageManifest):
    source: str = "build_parents"
    merge_components: int = 0
    parents_changed: int = 0
    parents_merged: int = 0
    singletons_written: int = 0
    owner_excluded: int = 0
    orphans_removed: int = 0
    parents_dir: str = ""
    elapsed_ms: int = 0
