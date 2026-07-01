"""Drop homogeneous (shared) BM25 lead terms across a set of shotgun probes.

When many probes are LLM-expanded from a JD, expansion tends to give every probe the
same lead BM25 term ("distributed systems engineer", "ai product engineer", ...).
Those high-document-frequency terms make the probes' BM25 channel retrieve the SAME
people, so the union overlaps and recall saturates. Dropping just the shared lead
terms (keeping each probe's DISTINCTIVE terms) de-homogenizes the BM25 channel and
recovered ~7pts of recall in testing (90% -> 97% at top-200) — and it is
data-driven, not hardcoded, so it generalizes across JDs.

This operates ACROSS probes (it needs the whole set to compute document frequency),
so it is a set-level step, not a per-probe `prepare` flag. Pure functions
(`shared_bm25_terms`, `diversify_filters`) are unit-tested; the CLI rewrites a set of
prepared payloads. No network, no spend.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _filters(payload: dict[str, Any]) -> dict[str, Any]:
    f = payload.get("role_search_filters")
    return f if isinstance(f, dict) else payload


def shared_bm25_terms(payloads: list[dict[str, Any]], *, df_threshold: float = 0.35, min_df: int = 3) -> set[str]:
    """Terms whose document frequency across probes exceeds the threshold (lowercased).

    A term is "shared" if it appears in more than max(min_df, df_threshold*N) probes.
    These are the homogeneous lead terms to drop. With few probes, min_df guards
    against dropping everything.
    """
    n = len(payloads)
    if n == 0:
        return set()
    df: Counter = Counter()
    for p in payloads:
        for t in {str(x).lower() for x in (_filters(p).get("bm25_queries") or [])}:
            df[t] += 1
    cutoff = max(min_df, int(df_threshold * n))
    return {t for t, c in df.items() if c > cutoff}


def diversify_filters(filters: dict[str, Any], shared: set[str]) -> dict[str, Any]:
    """Return a copy of role_search_filters with shared BM25 terms removed."""
    out = dict(filters)
    bm25 = [t for t in (out.get("bm25_queries") or []) if str(t).lower() not in shared]
    out["bm25_queries"] = bm25
    out["bm25_diversified"] = True
    return out


def _apply(payload: dict[str, Any], shared: set[str]) -> dict[str, Any]:
    if isinstance(payload.get("role_search_filters"), dict):
        out = dict(payload)
        out["role_search_filters"] = diversify_filters(payload["role_search_filters"], shared)
        return out
    return diversify_filters(payload, shared)


def main() -> None:
    ap = argparse.ArgumentParser(description="Drop shared/homogeneous BM25 lead terms across a probe set.")
    ap.add_argument("--payloads", nargs="+", required=True, help="Prepared payload JSON files (one per probe)")
    ap.add_argument("--df-threshold", type=float, default=0.35, help="Drop terms in more than this fraction of probes")
    ap.add_argument("--min-df", type=int, default=3, help="Floor on the df cutoff (guards small probe sets)")
    ap.add_argument("--out-dir", help="Write diversified payloads here (basename preserved); default rewrites in place")
    args = ap.parse_args()

    paths = [Path(p) for p in args.payloads]
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    shared = shared_bm25_terms(payloads, df_threshold=args.df_threshold, min_df=args.min_df)

    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for path, payload in zip(paths, payloads):
        dest = (out_dir / path.name) if out_dir else path
        dest.write_text(json.dumps(_apply(payload, shared), indent=2) + "\n", encoding="utf-8")
        written.append(str(dest))

    print(json.dumps({
        "primitive": "diversify_probe_bm25",
        "status": "completed",
        "probes": len(paths),
        "dropped_shared_terms": sorted(shared),
        "written": written,
    }, indent=2))


if __name__ == "__main__":
    main()
