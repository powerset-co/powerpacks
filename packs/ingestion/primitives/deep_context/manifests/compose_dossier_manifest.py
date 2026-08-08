"""Manifest emitted by dossier composition."""

from dataclasses import dataclass

from packs.ingestion.primitives.pipeline.contract import StageManifest


@dataclass(frozen=True)
class DossierSkip:
    """One parent whose dossier composition was skipped this run, and why.

    Composition isolates each parent (see ``ComposeDossier.execute``): a bad
    row is recorded here and the run continues rather than aborting for
    everyone after it in ``sorted(facts.items())``.
    """

    parent_id: str
    reason: str


class ComposeDossierManifest(StageManifest):
    source: str = "compose_dossier"
    dossiers_written: int = 0
    orphans_removed: int = 0
    # status stays "completed" even when parents were skipped — the repo
    # convention (reconcile_linkedin's `errors`, synthesize's `errors` +
    # `stop_reasons`) is a visible count/detail field, not a distinct status
    # value; `skipped`/`skip_reasons` are that field for this stage. A
    # reader checks `skipped` the same way it already checks `errors`
    # elsewhere.
    skipped: int = 0
    skip_reasons: tuple[DossierSkip, ...] = ()
    dossier_dir: str = ""
    index_md: str = ""
    elapsed_ms: int = 0
