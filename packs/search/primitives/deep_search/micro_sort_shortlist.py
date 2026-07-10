"""Micro-sort: agentic merge-sort ordering for the deep-search shortlist.

Port of network-search-api's proven reranker second pass (api_v2/search/rerank/
reranker.py + prompts/micro_sort.py). Judge scores are pointwise and SATURATE at
the top (dozens of candidates within a few hundredths), so rank-by-score is noise
ordering. This pass groups the shortlist into 0.1-wide score bands, packs adjacent
bands into batches (splitting oversized bands into sub-batches that are sorted then
merged), and has a fast LLM produce a RELATIVE ordering within each batch using the
judge's own evidence. Judge scores are never mutated — this reorders only.

Reads:  <run>/shortlist/ground_truth_ranked.json (+ plan.json, judges/loop.jsonl for evidence)
Writes: <run>/shortlist/ranked_final.json (same rows, micro-sorted, + micro_rank/band)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from openai_client import make_openai_client  # noqa: E402

DEFAULT_MODEL = os.environ.get("POWERPACKS_MICRO_SORT_MODEL", "gpt-4.1")

# Constants mirror network-search-api's measured defaults.
MIN_BAND_SIZE = 3     # bands smaller than this keep their existing order
MIN_SCORE = 0.5       # only micro-sort bands at or above this score
BATCH_CAP = 20        # max candidates per LLM call
MAX_BATCHES = 10      # cap total LLM calls (highest-scoring bands first)

SYSTEM_PROMPT = """You are ranking candidates who received similar judge scores in a recruiter search.

Your task: Given a batch of candidates with similar overall scores, produce a RELATIVE ORDERING from best fit to worst fit for the role.

=== RANKING PRIORITY (use these signals IN ORDER) ===

1. ROLE RELEVANCE — Does their current/recent work directly match the role's core traits?
   - Currently doing the core work > did it before > adjacent work > tangential
2. RECENCY — How recent is the matching evidence?
   - Currently in the role > left 1 year ago > left 3 years ago > left 5+ years ago
3. SENIORITY FIT — use the judge's `fit` verdict AS GIVEN (it was decided from the full
   profile). Do NOT demote a candidate for an ambiguous title string (e.g. "Principal/
   Director", "Team Lead") when fit=ideal/acceptable — the judge already resolved that.
4. LOCATION — Geographic alignment with the role (if the role states one)
5. CAREER DEPTH — Secondary signal for otherwise equal candidates
   - Multiple relevant roles across companies > single relevant role
   - Quality companies in the domain > unknown companies

=== RULES ===

- You MUST return ALL candidate IDs — no dropping, no duplicates
- The ordering must be DETERMINISTIC — same input should produce same output
- Do NOT re-evaluate scores — trust them, just ORDER within the band
- Use the judge's evidence/rationale to find quality differences the scores don't capture

=== OUTPUT FORMAT ===

Return strict JSON: {"ordering": ["<id best>", "<id 2nd>", ...]} including ALL input IDs exactly once."""

HUMAN_PROMPT = """Role core traits: {traits_list}

Candidates to rank (all scored similarly at ~{score_band}):

{candidates_block}

CRITICAL: You MUST return EXACTLY {candidate_count} ids. Every candidate id above must appear EXACTLY ONCE in your output. Count: {candidate_count} in, {candidate_count} out."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_batches(rows: list[dict[str, Any]], *, score_key: str = "mean_score") -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Group rows into 0.1-wide bands and greedily pack adjacent bands into batches.
    Returns (batches, passthrough) — passthrough rows keep their existing order."""
    bands: dict[float, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for r in rows:
        band = round(float(r.get(score_key) or 0), 1)
        if band < MIN_SCORE:
            passthrough.append(r)
        else:
            bands.setdefault(band, []).append(r)

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for band_key in sorted(bands, reverse=True):
        band_rows = bands[band_key]
        if len(band_rows) < MIN_BAND_SIZE:
            current.extend(band_rows)
            continue
        if len(current) + len(band_rows) <= BATCH_CAP:
            current.extend(band_rows)
        else:
            if current:
                batches.append(current)
            current = []
            if len(band_rows) <= BATCH_CAP:
                current = band_rows
            else:
                # Oversized band: split into sub-batches; each is sorted then merged
                # back in sequence (the "merge sort" step).
                for i in range(0, len(band_rows), BATCH_CAP):
                    sub = band_rows[i:i + BATCH_CAP]
                    if len(sub) >= MIN_BAND_SIZE:
                        batches.append(sub)
                    else:
                        current.extend(sub)
    if current:
        batches.append(current)
    return batches, passthrough


def cap_batches(batches: list[list[dict[str, Any]]], max_batches: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Deterministic runtime bound: keep only the first (highest-scoring) max_batches
    batches — effective sort region = max_batches * BATCH_CAP candidates (default 200).
    Dropped batches keep their score order and are appended after the sorted region."""
    if len(batches) <= max_batches:
        return batches, []
    dropped = [r for b in batches[max_batches:] for r in b]
    return batches[:max_batches], dropped


def validate_ordering(output_ids: list[str], batch: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    """Dedupe, drop unknown ids, append any the model lost — never lose a candidate."""
    by_id = {str(r.get(id_key)): r for r in batch}
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for oid in output_ids:
        oid = str(oid)
        if oid in by_id and oid not in seen:
            seen.add(oid)
            ordered.append(by_id[oid])
    ordered.extend(r for r in batch if str(r.get(id_key)) not in seen)
    return ordered


def candidate_block(row: dict[str, Any], evidence: dict[str, Any]) -> str:
    pid = row.get("person_id") or row.get("candidate_id")
    parts = [f"<candidate id=\"{pid}\">",
             f"{row.get('name')} — {row.get('current_title') or '?'} @ {row.get('current_company') or '?'}"
             + (f" · {row.get('location')}" if row.get("location") else "")]
    ev = evidence.get(str(pid)) or {}
    if ev.get("seniority_fit") or ev.get("verdict"):
        parts.append(f"fit: {ev.get('seniority_fit') or '?'} · verdict: {ev.get('verdict') or '?'}")
    if ev.get("rationale"):
        parts.append(f"judge: {str(ev['rationale'])[:400]}")
    traits = ev.get("must_have") or []
    if traits:
        parts.append("traits: " + "; ".join(f"{(t.get('trait') or '')[:40]}={t.get('status')}" for t in traits[:8]))
    parts.append("</candidate>")
    return "\n".join(parts)


def sort_batch(client: Any, model: str, batch: list[dict[str, Any]], traits_list: str,
               evidence: dict[str, Any], id_key: str) -> list[dict[str, Any]]:
    if len(batch) <= 1:
        return batch
    scores = [float(r.get("mean_score") or 0) for r in batch]
    prompt = HUMAN_PROMPT.format(
        traits_list=traits_list,
        score_band=f"{min(scores):.2f}-{max(scores):.2f}",
        candidates_block="\n".join(candidate_block(r, evidence) for r in batch),
        candidate_count=len(batch),
    )
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0.0,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        obj = json.loads(resp.choices[0].message.content or "{}")
        ordering = obj.get("ordering") or []
        ids = [o.get("id") if isinstance(o, dict) else o for o in ordering]
        return validate_ordering([str(i) for i in ids if i], batch, id_key)
    except Exception as exc:  # a failed batch keeps its original order
        print(json.dumps({"primitive": "micro_sort_shortlist", "batch_error": str(exc)[:200]}), file=sys.stderr)
        return batch


def main() -> None:
    ap = argparse.ArgumentParser(description="Micro-sort (agentic merge sort) the deep-search shortlist within score bands.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--input", default=None, help="Rows to sort (default <run>/shortlist/ground_truth_ranked.json)")
    ap.add_argument("--out", default=None, help="Output (default <run>/shortlist/ranked_final.json)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-batches", type=int, default=MAX_BATCHES,
                    help=f"Deterministic runtime bound: sort at most N batches of {BATCH_CAP} "
                         f"(default {MAX_BATCHES} -> top ~{MAX_BATCHES * BATCH_CAP}); 5 -> top ~100. "
                         "Everything past the cap keeps its judge-score order.")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    in_path = Path(args.input) if args.input else run_dir / "shortlist" / "ground_truth_ranked.json"
    out_path = Path(args.out) if args.out else run_dir / "shortlist" / "ranked_final.json"
    rows = json.loads(in_path.read_text(encoding="utf-8"))
    id_key = "person_id" if rows and "person_id" in rows[0] else "candidate_id"

    plan = {}
    for cand in (run_dir / "epoch0" / "plan.json", run_dir / "plan.json"):
        if cand.exists():
            plan = json.loads(cand.read_text(encoding="utf-8"))
            break
    traits = plan.get("traits", {}) or {}
    core = [t.get("trait") for t in traits.get("must_have", []) if t.get("tier") == "core" and t.get("trait")]
    traits_list = "; ".join(core or [t.get("trait") for t in traits.get("must_have", []) if t.get("trait")][:6]) or "role fit"

    evidence: dict[str, Any] = {}
    for r in _jsonl(run_dir / "judges" / "loop.jsonl"):
        if not r.get("error"):
            evidence[str(r.get("candidate_id") or r.get("person_id"))] = r

    batches, passthrough = build_batches(rows)
    batches, dropped = cap_batches(batches, max(1, args.max_batches))
    capped = len(dropped)
    passthrough = dropped + passthrough

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print(json.dumps({"primitive": "micro_sort_shortlist", "status": "failed", "error": "OPENAI_API_KEY not set"}))
        raise SystemExit(1)
    client = make_openai_client(key)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        sorted_batches = list(ex.map(lambda b: sort_batch(client, args.model, b, traits_list, evidence, id_key), batches))

    final = [r for b in sorted_batches for r in b] + passthrough
    for i, r in enumerate(final):
        r["micro_rank"] = i + 1
        r["micro_band"] = round(float(r.get("mean_score") or 0), 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(final, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "micro_sort_shortlist", "status": "completed", "sorted": sum(len(b) for b in batches),
                      "passthrough": len(passthrough), "batches": len(batches), "candidates_dropped_to_cap": capped,
                      "max_batches": args.max_batches, "model": args.model, "out": str(out_path)}, indent=2))


if __name__ == "__main__":
    main()
