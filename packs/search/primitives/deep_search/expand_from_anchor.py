"""Turn judged-strong candidates ("anchors") into "more like this" search seeds.

Expand-from-anchor: once the judge confirms strong candidates, search for their neighbors by
building a work-described query from each anchor's OWN profile (headline + recent positions +
companies + skills). Those seeds feed deep_search/run_wide_search.py exactly like decompose_jd seeds, and
because they describe a *proven-good* profile they surface similar people the generic JD seeds
missed.

GUARDRAIL: anchors MUST come from the recipe's own judged-strong set (e.g. judge_consensus's
ground_truth_ranked.json from a run) — NEVER from the evaluation ground-truth answer key. Seeding
from the GT would just look up the answers and inflate the recall metric. This primitive only
reads a candidates file you point it at; keep that contract.

No LLM, no network — pure template over profile fields. Output: seeds.json = [{key, query, anchor}].
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def anchor_to_seed(profile: dict[str, Any]) -> dict[str, str] | None:
    """Build one work-described seed sentence from an anchor's profile fields."""
    name = profile.get("name") or profile.get("person_id") or "anchor"
    parts: list[str] = []
    headline = (profile.get("headline") or "").strip()
    if headline:
        parts.append(headline)
    for pos in (profile.get("positions") or [])[:3]:
        title = (pos.get("title") or "").strip()
        company = (pos.get("company_name") or "").strip()
        desc = (pos.get("company_description") or "").strip()
        seg = " ".join(x for x in (title, ("at " + company) if company else "", desc) if x).strip()
        if seg:
            parts.append(seg)
    skills = [s for s in (profile.get("tech_skills") or []) if isinstance(s, str)][:8]
    if skills:
        parts.append("skills: " + ", ".join(skills))
    body = ". ".join(p for p in parts if p).strip()
    if not body:
        # fall back to title/company if no rich profile attached
        body = " ".join(x for x in (profile.get("current_title") or "", profile.get("current_company") or "") if x).strip()
    if not body:
        return None
    query = f"Engineer whose background looks like this proven-strong profile: {body}"
    return {"query": query, "anchor": str(name)}


def select_anchors(records: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    """Take the top-K by mean_score if present, else file order. (Records are judged-strong.)"""
    if any("mean_score" in r for r in records):
        records = sorted(records, key=lambda r: -float(r.get("mean_score") or 0.0))
    return records[:top_k]


def build_seeds(records: list[dict[str, Any]], top_k: int) -> list[dict[str, str]]:
    seeds: list[dict[str, str]] = []
    for i, rec in enumerate(select_anchors(records, top_k)):
        seed = anchor_to_seed(rec)
        if seed:
            seed["key"] = f"anchor{i:02d}"
            seeds.append(seed)
    return seeds


def _load(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return json.loads(text) if text[0] == "[" else [json.loads(l) for l in text.splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build 'more like this' seeds from judged-strong anchors (no LLM).")
    ap.add_argument("--anchors", required=True, help="judged-strong file (e.g. shortlist/ground_truth_ranked.json) — NEVER the eval GT")
    ap.add_argument("--top-k", type=int, default=3, help="How many top anchors to expand from")
    ap.add_argument("--out", required=True, help="Where to write seeds.json")
    args = ap.parse_args()

    records = _load(Path(args.anchors))
    seeds = build_seeds(records, args.top_k)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(seeds, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "expand_from_anchor", "status": "completed", "anchors": len(seeds), "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
