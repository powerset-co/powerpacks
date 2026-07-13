"""Turn judged-strong candidates ("anchors") into "more like this" search seeds.

Expand-from-anchor: once the judge confirms strong candidates, search for their neighbors by
combining the approved role archetype with each anchor's current/recent titles and judged core
evidence. Those seeds feed deep_search/run_wide_search.py exactly like decompose_jd seeds, and
because they describe a *proven-good* profile they surface similar people the generic JD seeds
missed. Company descriptions are deliberately excluded: they describe the employer, not the work
that made the anchor strong.

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

try:
    from location_scope import location_scope_from_plan
except ImportError:  # pragma: no cover - package execution
    from .location_scope import location_scope_from_plan


QUALIFYING_STATUSES = {"experienced", "doing_now", "strong"}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _clip(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    prefix = value[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return prefix + "..."


def _role_label(plan: dict[str, Any] | None) -> str:
    plan = plan or {}
    return str(plan.get("normalized_archetype") or plan.get("job_title") or "candidate").strip()


def _role_titles(profile: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    current = str(profile.get("current_title") or "").strip()
    if current:
        titles.extend(part.strip() for part in current.split(";") if part.strip())
    headline = str(profile.get("headline") or "").strip()
    if headline:
        titles.append(headline)
    for position in profile.get("positions") or []:
        if not isinstance(position, dict):
            continue
        title = str(position.get("position_title") or position.get("title") or "").strip()
        if title:
            titles.append(title)

    out: list[str] = []
    seen: set[str] = set()
    for title in titles:
        key = _norm(title)
        if key and key not in seen:
            seen.add(key)
            out.append(title)
    return out[:6]


def _judged_core_evidence(profile: dict[str, Any], plan: dict[str, Any] | None) -> list[str]:
    must = ((plan or {}).get("traits") or {}).get("must_have") or []
    core = {
        _norm(item.get("trait"))
        for item in must
        if isinstance(item, dict) and item.get("tier") == "core" and item.get("trait")
    }
    if not core:
        return []

    verdicts: list[dict[str, Any]] = []
    per_judge = profile.get("per_judge") or {}
    if isinstance(per_judge, dict):
        verdicts.extend(value for _, value in sorted(per_judge.items()) if isinstance(value, dict))
    if isinstance(profile.get("must_have"), list):
        verdicts.append(profile)

    evidence: list[str] = []
    seen: set[str] = set()
    for verdict in verdicts:
        for item in verdict.get("must_have") or []:
            if not isinstance(item, dict) or _norm(item.get("trait")) not in core:
                continue
            if str(item.get("status") or "").strip().lower() not in QUALIFYING_STATUSES:
                continue
            trait = str(item.get("trait") or "").strip()
            cite = str(item.get("evidence") or "").strip()
            key = _norm(trait)
            if not trait or key in seen:
                continue
            seen.add(key)
            evidence.append(f"{trait}: {_clip(cite)}" if cite else trait)
    return evidence[:3]


def anchor_to_seed(profile: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Build one role-aware seed from titles and the judge's direct core evidence."""
    name = profile.get("name") or profile.get("person_id") or "anchor"
    titles = _role_titles(profile)
    evidence = _judged_core_evidence(profile, plan)
    if not titles and not evidence:
        return None
    parts = []
    if titles:
        parts.append("current/recent roles: " + "; ".join(titles))
    if evidence:
        parts.append("demonstrated core work: " + "; ".join(evidence))
    query = f"{_role_label(plan)} with a background like this proven-strong match: " + ". ".join(parts)
    location, location_filters = location_scope_from_plan(plan) if plan is not None else (None, {})
    seed = {"query": query, "anchor": str(name)}
    if plan is not None:
        seed["required_location"] = location or ""
        seed["location_filters"] = location_filters
    return seed


def select_anchors(records: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    """Take the top-K by mean_score if present, else file order. (Records are judged-strong.)"""
    if any("mean_score" in r for r in records):
        records = sorted(records, key=lambda r: -float(r.get("mean_score") or 0.0))
    return records[:top_k]


def build_seeds(
    records: list[dict[str, Any]],
    top_k: int,
    plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for i, rec in enumerate(select_anchors(records, top_k)):
        seed = anchor_to_seed(rec, plan)
        if seed:
            seed["key"] = f"anchor{i:02d}"
            seeds.append(seed)
    return seeds


def _load(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return json.loads(text) if text[0] == "[" else [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build 'more like this' seeds from judged-strong anchors (no LLM).")
    ap.add_argument("--anchors", required=True, help="judged-strong file (e.g. shortlist/ground_truth_ranked.json) — NEVER the eval GT")
    ap.add_argument("--plan", required=True, help="Approved plan.json; supplies role, core traits, and location")
    ap.add_argument("--top-k", type=int, default=3, help="How many top anchors to expand from")
    ap.add_argument("--out", required=True, help="Where to write seeds.json")
    args = ap.parse_args()

    records = _load(Path(args.anchors))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    seeds = build_seeds(records, args.top_k, plan)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(seeds, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "expand_from_anchor", "status": "completed", "anchors": len(seeds), "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
