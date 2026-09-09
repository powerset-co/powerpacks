"""Fetch a JD, generate one reviewed pond query, then use the ordinary search pipeline.

URL intake writes jd.txt and source.json once. Query approval initializes
results.json with the reviewed JD, queries, and retrieval corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

try:  # direct script execution
    from subprocess_utils import CommandError, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.deep_search.deep_search_loop
    from .subprocess_utils import CommandError, run_checked

ROOT = Path(__file__).resolve().parents[4]
P = ROOT / "packs/search/primitives/deep_search"
FETCH_JD = P / "fetch_jd.py"

# A fetched JD below this many chars is almost certainly a JS-rendered page that yielded no real
# text; decomposing it produces a garbage query. Mirrors fetch_jd._THIN_CHARS (fetch_jd flags "thin"
# but exits 0, so the loop guards it explicitly before spending on sourcing).
_MIN_JD_CHARS = 400


def run(cmd: list[object], *, expected_paths: list[Path] | None = None, description: str | None = None) -> None:
    # Child stages get a usage-stage tag consumed by the shared client's POWERPACKS_USAGE_LOG capture.
    stage = (description or "child").replace(" ", "_")
    prior = os.environ.get("POWERPACKS_USAGE_STAGE")
    os.environ["POWERPACKS_USAGE_STAGE"] = stage
    try:
        run_checked(cmd, expected_paths=expected_paths, description=description)
    finally:
        if prior is None:
            os.environ.pop("POWERPACKS_USAGE_STAGE", None)
        else:
            os.environ["POWERPACKS_USAGE_STAGE"] = prior


def resolve_backend(run_dir: Path, requested: str | None, decision_arg: str | None) -> tuple[str, Path | None]:
    """Bind execution to decision.json when present; explicit CLI and recorded decisions may not drift."""
    decision_path = Path(decision_arg) if decision_arg else run_dir / "decision.json"
    if decision_arg and not decision_path.exists():
        raise ValueError(f"decision file not found: {decision_path}")
    if not decision_path.exists():
        return requested or "powerset", None
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    if decision.get("surface") != "people" or decision.get("depth") != "deep":
        raise ValueError(
            f"decision must be people/deep, got {decision.get('surface')!r}/{decision.get('depth')!r}"
        )
    recorded = decision.get("backend")
    if recorded not in {"powerset", "local"}:
        raise ValueError(f"decision has invalid backend: {recorded!r}")
    if requested and requested != recorded:
        raise ValueError(f"--backend {requested} conflicts with decision backend {recorded}")
    return recorded, decision_path


def normalize_source_url(value: str) -> str:
    """Normalize only transport-irrelevant URL details for resume binding."""
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid JD source URL: {value!r}")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def validate_bound_jd_source(source_path: Path, requested_url: str) -> dict[str, Any]:
    """Fail closed when a resumed URL run does not match its original fetch metadata."""
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot verify existing jd.txt source from {source_path}: {exc}") from exc
    if not isinstance(source, dict):
        raise ValueError(f"cannot verify existing jd.txt source: {source_path} is not a JSON object")
    bound_url = source.get("requested_url") or source.get("source_url")
    if not isinstance(bound_url, str) or not bound_url.strip():
        raise ValueError(f"cannot verify existing jd.txt source: {source_path} has no bound URL")
    if normalize_source_url(bound_url) != normalize_source_url(requested_url):
        raise ValueError(
            f"--jd-url {requested_url!r} conflicts with the URL bound in {source_path}: {bound_url!r}"
        )
    return source


def resolve_retrieval_identity(
    backend: str,
    requested_set_id: str | None,
    requested_db: str,
) -> tuple[dict[str, Any], str | None, str]:
    """Resolve the exact corpus identity that approved artifacts may use."""
    if backend == "powerset":
        set_id = str(requested_set_id or os.environ.get("POWERPACKS_DEFAULT_SET_ID") or "").strip()
        if not set_id:
            raise ValueError("Powerset search requires --set-id or POWERPACKS_DEFAULT_SET_ID")
        return {"backend": "powerset", "set_id": set_id}, set_id, requested_db

    db_path = Path(requested_db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db_path = db_path.resolve()
    try:
        stat = db_path.stat()
    except OSError as exc:
        raise ValueError(f"local DuckDB is not readable: {db_path}: {exc}") from exc
    if not db_path.is_file():
        raise ValueError(f"local DuckDB path is not a file: {db_path}")
    identity = {
        "backend": "local",
        "db_path": str(db_path),
        "db_size": stat.st_size,
        "db_mtime_ns": stat.st_mtime_ns,
    }
    return identity, None, str(db_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="The $search deep orchestrator: one reviewed query, then the pond harness.")
    ap.add_argument("--jd-file", default=None, help="Path to JD text. Provide this OR --jd-url.")
    ap.add_argument("--jd-url", default=None, help="Job-posting URL; fetched to <run-dir>/jd.txt via fetch_jd before sourcing.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--backend", choices=("powerset", "local"), default=None, help="Sourcing backend. Defaults from <run-dir>/decision.json, else powerset; local = DuckDB")
    ap.add_argument("--decision", default=None, help="decision.json override. If present, surface=people/depth=deep/backend are enforced")
    ap.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb", help="Local DuckDB path (used only with --backend local)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--queries-file", default=None,
                    help="Reviewed JSON: one or two objects containing only key/query")
    ap.add_argument("--query-approved", action="store_true", help="Initialize the reviewed query without retrieving candidates")
    ap.add_argument("--query-model", default="gpt-5.6-luna", help="Pond-1 query generator model")
    ap.add_argument("--query-reasoning-effort", default="medium", help="Pond-1 query generator reasoning effort")
    ap.add_argument("--expand-model", default="gpt-5.6-luna", help="Per-pond query expansion model")
    ap.add_argument("--expand-reasoning-effort", default="medium", help="Per-pond expansion reasoning effort")
    ap.add_argument("--filter-model", default="gpt-5.6-luna", help="Per-pond filter model")
    ap.add_argument("--filter-reasoning-effort", default="none", help="Per-pond filter reasoning effort")
    ap.add_argument("--rerank-model", default="gpt-5.6-luna", help="Per-pond rerank model")
    ap.add_argument("--rerank-reasoning-effort", default="medium", help="Per-pond rerank reasoning effort")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    try:
        args.backend, decision_path = resolve_backend(run_dir, args.backend, args.decision)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": str(exc)}, indent=2))
        raise SystemExit(2) from exc

    # JD input: exactly one of --jd-file / --jd-url. A URL is fetched to <run-dir>/jd.txt first
    # (the URL intake via fetch_jd), then treated as an ordinary --jd-file from here on.
    if bool(args.jd_file) == bool(args.jd_url):
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "provide exactly one of --jd-file or --jd-url"}, indent=2))
        raise SystemExit(2)
    if args.jd_url:
        run_dir.mkdir(parents=True, exist_ok=True)
        jd_txt = run_dir / "jd.txt"
        source_json = run_dir / "source.json"
        if jd_txt.exists():
            # The first fetch IS the contract: re-fetching would overwrite the JD the query
            # (and its hash binding) came from — rotating page tokens or a taken-down posting
            # would silently corrupt or brick the run. Reuse the bound file.
            note = "using existing jd.txt (URL binding verified); not re-fetching --jd-url"
        else:
            run([sys.executable, FETCH_JD, "--url", args.jd_url, "--out", jd_txt],
                expected_paths=[jd_txt, source_json], description="fetch_jd URL->JD")
            note = "fetched jd.txt and bound its source URL"
        try:
            validate_bound_jd_source(source_json, args.jd_url)
        except ValueError as exc:
            print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": str(exc)}, indent=2))
            raise SystemExit(2) from exc
        print(json.dumps({"primitive": "deep_search_loop", "note": note}))
        jd_text = jd_txt.read_text(encoding="utf-8").strip()
        if len(jd_text) < _MIN_JD_CHARS:
            print(json.dumps({"primitive": "deep_search_loop", "status": "failed",
                              "error": "fetched JD is too thin (likely a JS-rendered page); paste the JD text and rerun with --jd-file",
                              "jd_url": args.jd_url, "jd_chars": len(jd_text)}, indent=2))
            raise SystemExit(1)
        args.jd_file = str(jd_txt)

    os.environ.setdefault("POWERPACKS_USAGE_LOG", str(run_dir / "usage.jsonl"))
    try:
        try:
            from search_harness import run_search_harness
        except ImportError:  # pragma: no cover - package execution
            from .search_harness import run_search_harness
        result = run_search_harness(args, run_dir, decision_path)

    except (CommandError, OSError, json.JSONDecodeError, ValueError) as exc:
        details = exc.to_dict() if isinstance(exc, CommandError) else None
        print(json.dumps({
            "primitive": "deep_search_loop",
            "status": "failed",
            "error": str(exc),
            "details": details,
        }, indent=2))
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
