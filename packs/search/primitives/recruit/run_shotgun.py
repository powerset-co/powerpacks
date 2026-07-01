"""Run a shotgun of probes from a seeds file and emit a deduped candidate union.

One callable command that chains the EXISTING primitives so any harness reproduces sourcing
identically (no inline scripting, no shared-ledger footgun):

  for each seed: search_network_pipeline prepare --preserve-query-semantic   (raw query as vector + bm25 + filters)
  diversify_probe_bm25 across all probe payloads                              (drop shared lead terms)
  for each seed: search_network_pipeline run --search-only (unique --ledger)  (read-only hybrid retrieval + Postgres hydrate)
  -> union.jsonl (person_id + fields + found_by + attached profile)

Input seeds.json = [{"key","query", ...}] from decompose_jd.py or expand_from_anchor.py.
Retrieval is read-only (TurboPuffer) + Postgres hydrate -> no OpenAI here (the LLM cost was the
one prepare() expansion call per seed). See packs/search/skills/recruit/SKILL.md.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:  # direct script execution
    from subprocess_utils import CommandError, require_paths, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.recruit.run_shotgun
    from .subprocess_utils import CommandError, require_paths, run_checked

ROOT = Path(__file__).resolve().parents[4]
SNP = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
DIVERSIFY = ROOT / "packs/search/primitives/recruit/diversify_probe_bm25.py"


def _load_seeds(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("seeds", [])


def _prepare(seed: dict[str, str], probe_dir: Path, env_file: str, preserve: bool) -> Path | None:
    """Prepare one probe payload. Returns None (never raises) when this single probe fails so one
    flaky expansion call cannot abort the whole shotgun: main drops None via ok_seeds and only fails
    if NO probe survives. Each prepare makes an LLM expansion call, so transient 429/500 is expected."""
    probe_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SNP), "prepare", "--query", seed["query"],
           "--env-file", env_file, "--output-dir", str(probe_dir / "prep")]
    if preserve:
        cmd.append("--preserve-query-semantic")
    try:
        run_checked(cmd, description=f"prepare probe {seed.get('key')}")
        found = glob.glob(str(probe_dir / "prep" / "**" / "expand_search_request.json"), recursive=True)
        if not found:
            raise CommandError(cmd, missing=[probe_dir / "prep" / "**" / "expand_search_request.json"], description=f"prepare probe {seed.get('key')}")
        dest = probe_dir / "payload.json"
        shutil.copy(found[0], dest)
        require_paths([dest], cmd=cmd, description=f"prepare probe {seed.get('key')}")
        return dest
    except CommandError as exc:
        print(json.dumps({"primitive": "run_shotgun", "probe": seed.get("key"), "stage": "prepare",
                          "status": "skipped", "error": str(exc)}), file=sys.stderr)
        return None


def _run(seed: dict[str, str], probe_dir: Path, set_id: str | None, env_file: str, limit: int, top_k: int) -> bool:
    """Run one probe. Returns False (never raises) when this single probe fails so one flaky
    retrieval cannot abort the shotgun: build_union skips probes without a ledger and main fails
    only if the union ends up empty."""
    payload = probe_dir / "payload.json"
    ledger = probe_dir / "ledger.json"
    try:
        if set_id:  # ensure scoping even if the payload lacks it
            p = json.loads(payload.read_text())
            f = p.get("role_search_filters") if isinstance(p.get("role_search_filters"), dict) else p
            f["set_id"] = set_id
            payload.write_text(json.dumps(p, indent=2))
        run_checked([sys.executable, str(SNP), "run", "--query", seed["key"],
                     "--payload-json", str(payload), "--ledger", str(ledger),
                     "--env-file", env_file, "--search-only", "--limit", str(limit), "--top-k", str(top_k)],
                    expected_paths=[ledger], description=f"run probe {seed.get('key')}")
        return True
    except (CommandError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"primitive": "run_shotgun", "probe": seed.get("key"), "stage": "run",
                          "status": "skipped", "error": str(exc)}), file=sys.stderr)
        return False


def build_union(run_dir: Path, seeds: list[dict[str, str]], keep: int) -> list[dict[str, Any]]:
    prof: dict[str, dict[str, Any]] = {}
    union: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        led_path = run_dir / "probes" / seed["key"] / "ledger.json"
        if not led_path.exists():
            continue
        led = json.loads(led_path.read_text())
        arts = led.get("artifacts") or {}
        lp = arts.get("llm_profiles_path")
        if lp and os.path.exists(lp):
            for line in open(lp):
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prof.setdefault(r["person_id"], r)
        csvp = arts.get("csv")
        if not csvp or not os.path.exists(csvp):
            continue
        rows = sorted(csv.DictReader(open(csvp)), key=lambda r: int(r["rank"]))[:keep]
        for r in rows:
            pid = r["person_id"]
            u = union.setdefault(pid, {"person_id": pid, "name": r.get("name"), "linkedin_url": r.get("linkedin_url"),
                                       "current_title": r.get("current_titles"), "current_company": r.get("current_companies"),
                                       "location": r.get("location"), "found_by": []})
            u["found_by"].append(seed["key"])
    for pid, u in union.items():
        r = prof.get(pid, {})
        u["found_by"] = sorted(set(u["found_by"]))
        u["positions"] = [{k: x.get(k) for k in ("title", "company_name", "company_description", "start_date", "end_date") if x.get(k)} for x in (r.get("positions") or [])[:5]]
        u["education"] = [{k: e.get(k) for k in ("school_name", "degree", "field_of_study") if e.get(k)} for e in (r.get("education") or [])[:2]]
        u["tech_skills"] = [s for s in (r.get("tech_skills") or []) if isinstance(s, str)][:12]
    return sorted(union.values(), key=lambda r: (-len(r["found_by"]), r.get("name") or ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a shotgun of seeds -> deduped candidate union (chains existing primitives).")
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--run-dir", required=True, help="Output dir: probes/<key>/ + union.jsonl")
    ap.add_argument("--set-id", default=os.environ.get("POWERPACKS_DEFAULT_SET_ID"))
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--limit", type=int, default=200, help="Kept per probe at retrieval")
    ap.add_argument("--top-k", type=int, default=6000)
    ap.add_argument("--keep", type=int, default=0, help="Top-N per probe folded into the union (0 = --limit)")
    ap.add_argument("--no-preserve-semantic", action="store_true", help="Let expansion rewrite the semantic vector (NOT recommended)")
    ap.add_argument("--no-diversify", action="store_true", help="Skip dropping shared BM25 lead terms")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    seeds = _load_seeds(Path(args.seeds))
    preserve = not args.no_preserve_semantic
    keep = args.keep or args.limit

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            payloads = list(ex.map(lambda s: _prepare(s, run_dir / "probes" / s["key"], args.env_file, preserve), seeds))
        ok_seeds = [s for s, p in zip(seeds, payloads) if p is not None]

        if not ok_seeds:  # every probe's prepare failed — nothing to search
            raise CommandError(["run_shotgun"], description="prepare probes (all probes failed)", missing=[run_dir / "probes"])

        if not args.no_diversify:
            files = [str(run_dir / "probes" / s["key"] / "payload.json") for s in ok_seeds]
            run_checked([sys.executable, str(DIVERSIFY), "--payloads", *files], description="diversify probe payloads")

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            ran = list(ex.map(lambda s: _run(s, run_dir / "probes" / s["key"], args.set_id, args.env_file, args.limit, args.top_k), ok_seeds))
        run_ok = sum(1 for r in ran if r)
        if not run_ok:  # every surviving probe's retrieval failed
            raise CommandError(["run_shotgun"], description="run probes (all probes failed)", missing=[run_dir / "probes"])
    except CommandError as exc:
        print(json.dumps({"primitive": "run_shotgun", "status": "failed", "error": str(exc), "details": exc.to_dict()}, indent=2))
        raise SystemExit(1) from exc

    union = build_union(run_dir, ok_seeds, keep)
    if not union:
        print(json.dumps({"primitive": "run_shotgun", "status": "failed", "error": "empty union after successful probe runs"}, indent=2))
        raise SystemExit(1)
    out = run_dir / "union.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in union) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "run_shotgun", "status": "completed", "seeds": len(seeds),
                      "probes_prepared": len(ok_seeds), "probes_run_ok": run_ok, "union": len(union), "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
