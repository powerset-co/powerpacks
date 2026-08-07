"""Manifest emitted by dossier composition."""

from packs.ingestion.primitives.pipeline.contract import StageManifest


class ComposeDossierManifest(StageManifest):
    source: str = "compose_dossier"
    dossiers_written: int = 0
    orphans_removed: int = 0
    dossier_dir: str = ""
    index_md: str = ""
    elapsed_ms: int = 0
