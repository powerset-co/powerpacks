"""The `$search` deep orchestrator: one reviewed plan, then the pond harness.

The first run generates one reviewed plan plus one initial query and stops
once for approval. The approved run binds the plan, JD, and corpus, initializes
the fixed search-harness artifact, and hands off to `search_harness.py`, whose
commands compile, review, and execute one pond at a time, annotate the top rows
with the company-fit panel, and propose one next move, capped at four ponds.

  Review (before retrieval):
    fetch_jd (URL intake) -> build_eval_inputs (plan) -> network_floors
    -> decompose_jd (Pond-1 query) -> human approval
  --plan-approved:
    validate + bind plan/JD/corpus -> results.json + manifest.json
    -> search_harness compile-pond / review-payload / run-pond / decide ...

Changelog:
  2026-09-02  the exhaustive robust-source/triage/judge/anchor engine is
              deleted; the pond harness is the only engine and `--mode` is gone.
  2026-08-18  simple mode dynamically emits one or two candidate-population
              queries instead of forcing a fixed strategy roster.
  2026-08-17  simple five-query ordinary-pipeline mode is the default; the prior
              robust-source/judge/anchor loop is explicit --mode exhaustive.
  2026-07-30  observability: per-run usage capture default (POWERPACKS_USAGE_LOG ->
              <run-dir>/usage.jsonl, per-child stage tags).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

try:  # direct script execution
    import recruiter_policy
    from location_scope import required_location_from_plan
    from network_floors import probe_populations
    from plan_filters import validate_plan_filter_contract
    from subprocess_utils import CommandError, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.deep_search.deep_search_loop
    from .location_scope import required_location_from_plan
    from .network_floors import probe_populations
    from .plan_filters import validate_plan_filter_contract
    from .subprocess_utils import CommandError, run_checked
    from . import recruiter_policy

ROOT = Path(__file__).resolve().parents[4]
P = ROOT / "packs/search/primitives/deep_search"
FETCH_JD = P / "fetch_jd.py"

# A fetched JD below this many chars is almost certainly a JS-rendered page that yielded no real
# text; decomposing it produces a garbage plan. Mirrors fetch_jd._THIN_CHARS (fetch_jd flags "thin"
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


def validate_approved_plan(plan_path: Path, *, expected_source_url: str | None = None) -> dict[str, Any]:
    """Enforce the published schema (3-6 traits of a known kind, each with a quote) plus
    cross-field recruiter invariants."""
    validator_dir = ROOT / "packs/search/primitives/validate_artifact"
    if str(validator_dir) not in sys.path:
        sys.path.insert(0, str(validator_dir))
    from validate_artifact import validate_file  # type: ignore

    plan = validate_file("search-network-jd-plan", plan_path)
    required_location_from_plan(plan)
    validate_plan_filter_contract(plan)
    resolved = recruiter_policy.validate_resolved_recruiter_preferences(plan.get("recruiter_policy"))
    stage = plan.get("hire_stage")
    policy_stage = resolved["preferences"]["hire_stage"]
    if stage != policy_stage:
        raise ValueError(
            f"plan hire_stage {stage!r} conflicts with recruiter policy hire_stage {policy_stage!r}"
        )
    if expected_source_url:
        source_url = plan.get("source_url")
        if not isinstance(source_url, str) or not source_url.strip():
            raise ValueError("approved URL-sourced plan must contain source_url")
        if normalize_source_url(source_url) != normalize_source_url(expected_source_url):
            raise ValueError(
                f"approved plan source_url {source_url!r} conflicts with requested URL {expected_source_url!r}"
            )
    return plan


def plan_sha256(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path | None) -> str | None:
    """Hash an optional reviewed input exactly as executed."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path is not None else None


def resolve_retrieval_identity(
    backend: str,
    plan: dict[str, Any],
    requested_set_id: str | None,
    requested_db: str,
) -> tuple[dict[str, Any], str | None, str]:
    """Resolve the exact corpus identity that approved artifacts may use."""
    if backend == "powerset":
        planned_set_id = str((plan.get("set_scope") or {}).get("set_id") or "").strip()
        requested = str(requested_set_id or "").strip()
        if not planned_set_id:
            raise ValueError("approved Powerset plan must contain set_scope.set_id")
        if requested and requested != planned_set_id:
            raise ValueError(
                f"--set-id {requested!r} conflicts with approved plan set_id {planned_set_id!r}"
            )
        return {"backend": "powerset", "set_id": planned_set_id}, planned_set_id, requested_db

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


def _derived_execution_artifacts(run_dir: Path) -> list[Path]:
    candidates = [run_dir / "results.json", run_dir / "manifest.json", run_dir / "ponds"]
    return [path for path in candidates if path.exists()]


def bind_approved_plan(
    run_dir: Path,
    plan_path: Path,
    retrieval_identity: dict[str, Any],
    jd_path: Path | None = None,
    reviewed_queries_path: Path | None = None,
) -> tuple[Path, str]:
    """Pin reusable artifacts to the plan, JD, backend, and reviewed query input."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    digest = plan_sha256(plan)
    jd_digest = hashlib.sha256(jd_path.read_bytes()).hexdigest() if jd_path else None
    queries_digest = file_sha256(reviewed_queries_path)
    binding_path = run_dir / "plan_binding.json"
    canonical_plan_path = run_dir / "epoch0" / "plan.json"

    if binding_path.exists():
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        if binding.get("plan_sha256") != digest:
            raise ValueError(
                "approved plan differs from the contract already bound to this run; use a new run directory"
            )
        if binding.get("retrieval") != retrieval_identity:
            raise ValueError(
                "retrieval corpus differs from the corpus bound to this run; use a new run directory"
            )
        if binding.get("jd_sha256") != jd_digest:
            raise ValueError("JD source differs from the source bound to this run; use a new run directory")
        if reviewed_queries_path is not None and binding.get("queries_sha256") != queries_digest:
            raise ValueError("reviewed queries differ from the queries bound to this run; use a new run directory")
        if not canonical_plan_path.exists():
            raise ValueError("bound run is missing epoch0/plan.json")
        canonical = json.loads(canonical_plan_path.read_text(encoding="utf-8"))
        if plan_sha256(canonical) != digest:
            raise ValueError("epoch0/plan.json differs from plan_binding.json; use a new run directory")
        return canonical_plan_path, digest

    derived = _derived_execution_artifacts(run_dir)
    if derived:
        sample = ", ".join(str(path.relative_to(run_dir)) for path in derived[:4])
        raise ValueError(
            "run contains search artifacts without an approved-plan binding "
            f"({sample}); start a new run instead of reusing stale artifacts"
        )

    canonical_plan_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    binding = {
        "plan_sha256": digest,
        "jd_sha256": jd_digest,
        "retrieval": retrieval_identity,
        "policy_id": (plan.get("recruiter_policy") or {}).get("policy_id"),
        "policy_version": (plan.get("recruiter_policy") or {}).get("policy_version"),
    }
    if reviewed_queries_path is not None:
        binding["queries_sha256"] = queries_digest
    binding_path.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    return canonical_plan_path, digest


def main() -> None:
    ap = argparse.ArgumentParser(description="The $search deep orchestrator: one reviewed plan, then the pond harness.")
    ap.add_argument("--jd-file", default=None, help="Path to JD text. Provide this OR --jd-url.")
    ap.add_argument("--jd-url", default=None, help="Job-posting URL; fetched to <run-dir>/jd.txt via fetch_jd before sourcing.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--backend", choices=("powerset", "local"), default=None, help="Sourcing backend. Defaults from <run-dir>/decision.json, else powerset; local = DuckDB")
    ap.add_argument("--decision", default=None, help="decision.json override. If present, surface=people/depth=deep/backend are enforced")
    ap.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb", help="Local DuckDB path (used only with --backend local)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--created-at", required=True, help="ISO timestamp for the plan")
    ap.add_argument("--approved-plan", default=None, help="Reviewed plan.json to use without calling the plan LLM")
    ap.add_argument("--queries-file", default=None,
                    help="Reviewed JSON: one or two objects containing only key/query")
    ap.add_argument("--plan-approved", action="store_true", help="Resume with the existing <run-dir>/epoch0/plan.json after human review")
    ap.add_argument(
        "--preferences",
        default=None,
        help="Recruiter-preferences JSON used only when generating the pre-Review plan",
    )
    ap.add_argument("--plan-model", default="gpt-5.6-luna", help="Review plan model")
    ap.add_argument("--plan-reasoning-effort", default="medium", help="Review plan reasoning effort")
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
            # The first fetch IS the contract: re-fetching would overwrite the JD the plan
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
        result = run_search_harness(
            args,
            run_dir,
            decision_path,
            validate_plan=validate_approved_plan,
            resolve_identity=resolve_retrieval_identity,
            bind_plan=bind_approved_plan,
            probe_floors=probe_populations,
        )
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
