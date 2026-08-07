"""Manifest emitted by merge-candidate clustering and judging."""

from packs.ingestion.primitives.pipeline.contract import StageManifest


class ClusterMergeManifest(StageManifest):
    source: str = "cluster_merge_candidates"
    judge: str = ""
    people: int = 0
    pairs_total: int = 0
    pairs_deterministic: int = 0
    pairs_judged: int = 0
    pairs_reused: int = 0
    pairs_unsettled: int = 0
    candidate_pairs: int = 0
    clusters: int = 0
    confidence_threshold: float = 0.0
    tokens: dict[str, int] = {}
    estimated_cost_usd: float = 0.0
    out_csv: str = ""
    out_md: str = ""
    elapsed_ms: int = 0
