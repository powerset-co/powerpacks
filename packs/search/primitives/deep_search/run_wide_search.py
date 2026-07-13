"""Run a wide search of probes from a seeds file and emit a deduped candidate union.

One callable command that chains the EXISTING primitives so any harness reproduces sourcing
identically (no inline scripting, no shared-ledger footgun):

  for each seed: search_network_pipeline prepare --preserve-query-semantic   (raw query as vector + bm25 + filters)
  diversify_probe_bm25 across all probe payloads                              (drop shared lead terms)
  for each seed: search_network_pipeline run --search-only (unique --ledger)  (read-only hybrid retrieval + Postgres hydrate)
  -> union.jsonl (person_id + fields + found_by + attached profile)

Input seeds.json = [{"key","query", ...}] from decompose_jd.py or expand_from_anchor.py.
Retrieval is read-only (TurboPuffer) + Postgres hydrate -> no OpenAI here (the LLM cost was the
one prepare() expansion call per seed). See packs/search/skills/search/SKILL.md.
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
    from location_scope import enforce_payload_location, location_scope_from_plan
    from subprocess_utils import CommandError, require_paths, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.deep_search.run_wide_search
    from .location_scope import enforce_payload_location, location_scope_from_plan
    from .subprocess_utils import CommandError, require_paths, run_checked

ROOT = Path(__file__).resolve().parents[4]
SNP = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
DIVERSIFY = ROOT / "packs/search/primitives/deep_search/diversify_probe_bm25.py"


def _load_seeds(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("seeds", [])


def _backend_args(backend: str, db: str | None) -> list[str]:
    return ["--backend", "local", "--db", str(db)] if backend == "local" else []


def _seed_location_filters(seed: dict[str, Any]) -> dict[str, list[str]]:
    if "required_location" not in seed or "location_filters" not in seed:
        raise ValueError("seed is missing reviewed location metadata")
    display = seed["required_location"]
    if not isinstance(display, str):
        raise ValueError("seed required_location must be a string")
    _, filters = location_scope_from_plan({
        "search_scope": {
            "location": display.strip() or None,
            "filters": seed["location_filters"],
        }
    })
    return filters


def _prepare(seed: dict[str, Any], probe_dir: Path, env_file: str, preserve: bool, backend: str, db: str | None) -> Path | None:
    """Prepare one probe payload. Returns None (never raises) when this single probe fails so one
    flaky expansion call cannot abort the whole wide search: main drops None via ok_seeds and only fails
    if NO probe survives. Each prepare makes an LLM expansion call, so transient 429/500 is expected."""
    try:
        location_filters = _seed_location_filters(seed)
    except ValueError as exc:
        print(json.dumps({"primitive": "run_wide_search", "probe": seed.get("key"), "stage": "prepare",
                          "status": "skipped", "error": str(exc)}), file=sys.stderr)
        return None
    probe_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SNP), "prepare", "--query", seed["query"],
           "--env-file", env_file, "--output-dir", str(probe_dir / "prep"), *_backend_args(backend, db)]
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
        payload = json.loads(dest.read_text(encoding="utf-8"))
        enforce_payload_location(payload, location_filters)
        dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return dest
    except (CommandError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"primitive": "run_wide_search", "probe": seed.get("key"), "stage": "prepare",
                          "status": "skipped", "error": str(exc)}), file=sys.stderr)
        return None


def _run(seed: dict[str, Any], probe_dir: Path, set_id: str | None, env_file: str, limit: int, top_k: int, backend: str, db: str | None) -> bool:
    """Run one probe. Returns False (never raises) when this single probe fails so one flaky
    retrieval cannot abort the wide search: build_union skips probes without a ledger and main fails
    only if the union ends up empty."""
    payload = probe_dir / "payload.json"
    ledger = probe_dir / "ledger.json"
    try:
        location_filters = _seed_location_filters(seed)
        p = json.loads(payload.read_text(encoding="utf-8"))
        enforce_payload_location(p, location_filters)
        f = p.get("role_search_filters") if isinstance(p.get("role_search_filters"), dict) else p
        if set_id and backend != "local":  # local scope is the reviewed DuckDB file
            f["set_id"] = set_id
        payload.write_text(json.dumps(p, indent=2) + "\n", encoding="utf-8")
        run_checked([sys.executable, str(SNP), "run", "--query", seed["key"],
                     "--payload-json", str(payload), "--ledger", str(ledger),
                     "--env-file", env_file, "--search-only", "--limit", str(limit), "--top-k", str(top_k),
                     *_backend_args(backend, db)],
                    expected_paths=[ledger], description=f"run probe {seed.get('key')}")
        return True
    except (CommandError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"primitive": "run_wide_search", "probe": seed.get("key"), "stage": "run",
                          "status": "skipped", "error": str(exc)}), file=sys.stderr)
        return False


def build_union(run_dir: Path, seeds: list[dict[str, Any]], keep: int) -> list[dict[str, Any]]:
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
            with open(lp, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    prof.setdefault(r["person_id"], r)
        csvp = arts.get("csv")
        if not csvp or not os.path.exists(csvp):
            continue
        with open(csvp, newline="", encoding="utf-8") as handle:
            rows = sorted(csv.DictReader(handle), key=lambda r: int(r["rank"]))[:keep]
        for r in rows:
            pid = r["person_id"]
            u = union.setdefault(pid, {"person_id": pid, "name": r.get("name"), "linkedin_url": r.get("linkedin_url"),
                                       "current_title": r.get("current_titles"), "current_company": r.get("current_companies"),
                                       "location": r.get("location"), "found_by": []})
            u["found_by"].append(seed["key"])
    for pid, u in union.items():
        r = prof.get(pid, {})
        u["found_by"] = sorted(set(u["found_by"]))
        u["headline"] = r.get("headline")
        u["positions"] = [
            {
                **({"position_title": x.get("position_title") or x.get("title")}
                   if x.get("position_title") or x.get("title") else {}),
                **{k: x.get(k) for k in ("company_name", "company_description", "start_date", "end_date")
                   if x.get(k)},
            }
            for x in (r.get("positions") or [])[:5]
        ]
        u["education"] = [{k: e.get(k) for k in ("school_name", "degree", "field_of_study") if e.get(k)} for e in (r.get("education") or [])[:2]]
        u["tech_skills"] = [s for s in (r.get("tech_skills") or []) if isinstance(s, str)][:12]
    return sorted(union.values(), key=lambda r: (-len(r["found_by"]), r.get("name") or ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a wide search of seeds -> deduped candidate union (chains existing primitives).")
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--run-dir", required=True, help="Output dir: probes/<key>/ + union.jsonl")
    ap.add_argument("--set-id", default=os.environ.get("POWERPACKS_DEFAULT_SET_ID"))
    ap.add_argument("--backend", choices=("powerset", "local"), default="powerset", help="powerset = TurboPuffer/Postgres (default); local = the local DuckDB index (set-id scoping is skipped; no seniority bands are pinned)")
    ap.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb", help="Local DuckDB path (used only with --backend local)")
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
            payloads = list(ex.map(lambda s: _prepare(s, run_dir / "probes" / s["key"], args.env_file, preserve, args.backend, args.db), seeds))
        ok_seeds = [s for s, p in zip(seeds, payloads) if p is not None]

        if not ok_seeds:  # every probe's prepare failed — nothing to search
            raise CommandError(["run_wide_search"], description="prepare probes (all probes failed)", missing=[run_dir / "probes"])

        if not args.no_diversify:
            files = [str(run_dir / "probes" / s["key"] / "payload.json") for s in ok_seeds]
            run_checked([sys.executable, str(DIVERSIFY), "--payloads", *files], description="diversify probe payloads")

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            ran = list(ex.map(lambda s: _run(s, run_dir / "probes" / s["key"], args.set_id, args.env_file, args.limit, args.top_k, args.backend, args.db), ok_seeds))
        run_ok = sum(1 for r in ran if r)
        if not run_ok:  # every surviving probe's retrieval failed
            raise CommandError(["run_wide_search"], description="run probes (all probes failed)", missing=[run_dir / "probes"])
    except CommandError as exc:
        print(json.dumps({"primitive": "run_wide_search", "status": "failed", "error": str(exc), "details": exc.to_dict()}, indent=2))
        raise SystemExit(1) from exc

    union = build_union(run_dir, ok_seeds, keep)
    if not union:
        print(json.dumps({"primitive": "run_wide_search", "status": "failed", "error": "empty union after successful probe runs"}, indent=2))
        raise SystemExit(1)
    out = run_dir / "union.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in union) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": "run_wide_search", "status": "completed", "seeds": len(seeds),
                      "probes_prepared": len(ok_seeds), "probes_run_ok": run_ok, "union": len(union), "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
