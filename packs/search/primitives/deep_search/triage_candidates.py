"""Cheap-model triage over a deep-search candidate frontier (tier-1 filter).

The canonical judge (`evaluate_profile_candidates`, gpt-5.4) is expensive, so we DON'T run it on
the full wide-search union. The existing `llm_filter_candidates` only operates on a search_network
run-state (merge step + hydration coverage), which the deep-search artifact model deliberately
avoids. This is the portable equivalent for the deep-search `candidate_frontier.jsonl`:

  - load each candidate's hydrated profile (same probe_summaries -> profiles.jsonl.gz path the
    judge uses), build a compact profile card,
  - batch them to a CHEAP model with the plan's must/nice traits,
  - keep verdicts in {keep, maybe} (conservative — borderline survives to the real judge),
  - rewrite candidate_frontier.jsonl to survivors (originals saved to candidate_frontier.full.jsonl).

IMPORTANT (recall guardrail): triage is conservative on purpose. Probe-count is a BAD precision
signal — measured: single-probe hits include true top candidates — so triage looks at the PROFILE,
not how many probes found them, and only drops obvious non-matches. See packs/search/skills/search/SKILL.md.
"""
from __future__ import annotations

import argparse
import gzip
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

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = os.environ.get("RECRUIT_TRIAGE_MODEL", "gpt-4.1-mini")
KEEP = {"keep", "maybe"}

SYSTEM = (
    "You are a fast first-pass recruiting screener. You are NOT the final judge — be CONSERVATIVE: "
    "only 'drop' candidates who are CLEARLY irrelevant to the role (wrong field entirely, or "
    "obviously far too junior/senior with no relevant work). When unsure, 'maybe'. Reserve 'keep' "
    "for clearly on-target profiles. Missing data is NOT grounds to drop.\n"
    'Return strict JSON: {"verdicts":[{"id":"<id>","v":"keep|maybe|drop"}, ...]} for EVERY id given.'
)


def compact_card(pid: str, front: dict[str, Any], prof: dict[str, Any] | None) -> dict[str, Any]:
    prof = prof or {}
    positions = [
        " ".join(x for x in ((p.get("title") or "").strip(), ("@ " + (p.get("company_name") or "").strip()) if p.get("company_name") else "") if x)
        for p in (prof.get("positions") or [])[:4]
    ]
    edu = [
        " ".join(x for x in ((e.get("school_name") or "").strip(), (e.get("degree") or "").strip(), (e.get("field_of_study") or "").strip()) if x)
        for e in (prof.get("education") or [])[:2]
    ]
    return {
        "id": pid,
        "name": prof.get("name") or front.get("name"),
        "headline": prof.get("headline"),
        "current": " ".join(x for x in ((front.get("current_title") or ""), ("@ " + (front.get("current_company") or "")) if front.get("current_company") else "") if x) or None,
        "positions": [p for p in positions if p],
        "education": [e for e in edu if e],
        "skills": [s for s in (prof.get("tech_skills") or []) if isinstance(s, str)][:10],
    }


def build_batch_messages(traits: dict[str, Any], cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    must = [t.get("trait") for t in (traits.get("must_have") or []) if t.get("trait")]
    nice = [t.get("trait") for t in (traits.get("nice_to_have") or []) if t.get("trait")]
    head = (
        "Role must-have traits:\n" + "\n".join(f"- {t}" for t in must) +
        "\nNice-to-have:\n" + "\n".join(f"- {t}" for t in nice) +
        "\n\nClassify each candidate. Candidates (JSON):\n" + json.dumps(cards, ensure_ascii=False)
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": head}]


def load_profiles(probe_summaries: Path, wanted: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not probe_summaries.exists():
        return out
    for entry in json.loads(probe_summaries.read_text()):
        d = entry.get("artifact_dir") if isinstance(entry, dict) else None
        if not d:
            continue
        p = Path(d)
        gz = (p if p.is_absolute() else ROOT / p) / "hydrate_people" / "profiles.jsonl.gz"
        if not gz.exists():
            continue
        try:
            with gzip.open(gz, "rt") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pid = obj.get("person_id") or obj.get("id")
                    if pid in wanted and pid not in out:
                        out[pid] = obj
        except OSError:
            continue
        if len(out) == len(wanted):
            break
    return out


def parse_verdicts(content: str) -> dict[str, str]:
    try:
        obj = json.loads(content or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(v.get("id")): str(v.get("v", "")).lower() for v in (obj.get("verdicts") or []) if v.get("id")}


def main() -> None:
    ap = argparse.ArgumentParser(description="Cheap conservative triage over candidate_frontier.jsonl before the canonical judge.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--dry-run", action="store_true", help="Cards + batch count only; no LLM, no spend")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    frontier = [json.loads(l) for l in (run_dir / "candidate_frontier.jsonl").read_text().splitlines() if l.strip()]
    plan = json.loads((run_dir / "plan.json").read_text())
    traits = plan.get("traits", {}) or {}

    wanted = {c["person_id"] for c in frontier}
    profiles = load_profiles(run_dir / "probe_summaries.json", wanted)
    cards = [compact_card(c["person_id"], c, profiles.get(c["person_id"])) for c in frontier]
    batches = [cards[i:i + args.batch_size] for i in range(0, len(cards), args.batch_size)]

    if args.dry_run:
        print(json.dumps({"primitive": "triage_candidates", "status": "dry_run",
                          "candidates": len(cards), "with_profile": len(profiles),
                          "batches": len(batches), "model": args.model}, indent=2))
        return

    key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        print(json.dumps({"primitive": "triage_candidates", "status": "failed", "error": "OPENAI_API_KEY not set"}))
        raise SystemExit(1)

    client = make_openai_client(key)

    def run_batch(batch: list[dict[str, Any]]) -> dict[str, str]:
        try:
            resp = client.chat.completions.create(
                model=args.model, messages=build_batch_messages(traits, batch),
                response_format={"type": "json_object"},
            )
            return parse_verdicts(resp.choices[0].message.content or "{}")
        except Exception:  # noqa: BLE001 - on any batch failure, keep all (conservative)
            return {c["id"]: "maybe" for c in batch}

    verdicts: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for res in ex.map(run_batch, batches):
            verdicts.update(res)

    # Default unseen ids to keep (conservative: never silently drop on a parse miss).
    survivors = [c for c in frontier if verdicts.get(c["person_id"], "maybe") in KEEP]
    counts = {v: sum(1 for c in frontier if verdicts.get(c["person_id"], "maybe") == v) for v in ("keep", "maybe", "drop")}

    full = run_dir / "candidate_frontier.full.jsonl"
    if not full.exists():
        full.write_text("\n".join(json.dumps(c) for c in frontier) + "\n", encoding="utf-8")
    with (run_dir / "candidate_frontier.jsonl").open("w", encoding="utf-8") as fh:
        for c in survivors:
            fh.write(json.dumps(c) + "\n")
    (run_dir / "triage.json").write_text(json.dumps(
        {"input": len(frontier), "survivors": len(survivors), "verdicts": counts, "model": args.model}, indent=2), encoding="utf-8")

    print(json.dumps({"primitive": "triage_candidates", "status": "completed",
                      "input": len(frontier), "survivors": len(survivors),
                      "dropped": len(frontier) - len(survivors), "verdicts": counts}, indent=2))


if __name__ == "__main__":
    main()
