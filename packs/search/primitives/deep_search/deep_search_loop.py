"""The `$search` deep-mode convergence loop: source -> judge -> expand-from-anchor -> ... until converged.

expand-from-anchor is NOT a cleanup afterthought — it is the core Phase-2 hill-climb. The JD is a
lossy proxy for "what good looks like"; once the judge confirms strong candidates, THEIR profiles
are the highest-signal query for "find more like this", reaching the adjacent region the JD wording
never names (the barely-reachable stragglers that more JD-decompose rounds can't surface).

  Review (before retrieval):
    build_eval_inputs(--plan-only) -> plan_critic -> human approval
  epoch 0  (Phase 1, seed from the approved recruiter plan):
    robust_source(JD, plan) -> build_eval_inputs(plan reuse) -> judge -> consensus  => strong set S0
  epoch k>=1 (Phase 2, expand from our OWN judged-strong):
    pick DIVERSE anchors from S(k-1) (dedup by company so we don't echo-chamber one archetype)
    expand_from_anchor -> run_wide_search -> build_eval_inputs(--plan reuse) -> judge ONLY new pids
    consensus over everything judged so far => S(k)
  stop when a Phase-2 epoch adds NO new strong (converged) or --max-epochs hit (default 3).

Self-limiting give-up: if the judge returns ~0 strong there are no anchors, so Phase 2 no-ops and
the loop ends with an (almost) empty shortlist — correct behavior when the set has nobody.

Judging is INCREMENTAL (only candidates not yet judged) so the free `codex_judge` stays tractable
across epochs. Everything chains the existing deep-search primitives as subprocesses. See SKILL.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any

try:  # direct script execution
    import recruiter_policy
    from location_scope import required_location_from_plan
    from subprocess_utils import CommandError, run_checked
except ImportError:  # module execution: python -m packs.search.primitives.deep_search.deep_search_loop
    from .location_scope import required_location_from_plan
    from .subprocess_utils import CommandError, run_checked
    from . import recruiter_policy

ROOT = Path(__file__).resolve().parents[4]
P = ROOT / "packs/search/primitives/deep_search"
FETCH_JD = P / "fetch_jd.py"
ROBUST = P / "robust_source.py"
BUILD = P / "build_eval_inputs.py"
EXPAND = P / "expand_from_anchor.py"
WIDE_SEARCH = P / "run_wide_search.py"
CODEX_JUDGE = P / "codex_judge.py"
GPT_JUDGE = ROOT / "packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py"
TRIAGE = P / "triage_candidates.py"
CONSENSUS = P / "judge_consensus.py"
CRITIC = P / "plan_critic.py"
MICROSORT = P / "micro_sort_shortlist.py"
VALIDATE = ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py"

# CLI agent judges (codex/claude) are phase-2 only: they never bulk-filter. With --no-triage,
# a frontier with more unjudged candidates than this requires the API judge (--judge gpt).
MAX_CLI_JUDGE_FRONTIER = 300

# A fetched JD below this many chars is almost certainly a JS-rendered page that yielded no real
# text; decomposing it produces a garbage plan. Mirrors fetch_jd._THIN_CHARS (fetch_jd flags "thin"
# but exits 0, so the loop guards it explicitly before spending on sourcing).
_MIN_JD_CHARS = 400


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def diverse_anchors(strong: list[dict[str, Any]], union: dict[str, dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Top strong picks, one per current_company (spread archetypes, avoid echo-chamber), enriched
    with the union profile (positions/skills) so expand_from_anchor builds rich seeds."""
    ranked = sorted(strong, key=lambda r: -float(r.get("mean_score") or 0))
    out, seen_co = [], set()
    for r in ranked:
        co = (r.get("current_company") or "").strip().lower()
        if co and co in seen_co:
            continue
        seen_co.add(co)
        out.append({**union.get(r["person_id"], {}), **r})  # union profile + consensus fields
        if len(out) >= k:
            break
    return out


def run(cmd: list[object], *, expected_paths: list[Path] | None = None, description: str | None = None) -> None:
    run_checked(cmd, expected_paths=expected_paths, description=description)


def stage_judge_input(edir: Path, candidates: list[dict[str, Any]]) -> Path:
    """Create a new-only judge run dir while leaving canonical frontier files untouched."""
    jdir = edir / "judge_input"
    jdir.mkdir(parents=True, exist_ok=True)
    for name in ("plan.json", "probe_summaries.json"):
        src = edir / name
        if src.exists():
            shutil.copyfile(src, jdir / name)
    with (jdir / "candidate_frontier.jsonl").open("w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    return jdir


def anchor_expansion_command(anchors: Path, plan: Path, out: Path, top_k: int) -> list[object]:
    """Build the bound Phase-2 command so role and location context cannot be omitted."""
    return [
        sys.executable, EXPAND,
        "--anchors", anchors,
        "--plan", plan,
        "--top-k", top_k,
        "--out", out,
    ]


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


def load_advisory_critic(path: Path) -> dict[str, Any]:
    """A missing or corrupt advisory critic must never block the Review checkpoint."""
    if not path.exists():
        return {"verdict": "unavailable"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"verdict": "unavailable", "error": str(exc)[:200]}
    if not isinstance(value, dict):
        return {"verdict": "unavailable", "error": "plan critic output must be a JSON object"}
    return value


def validate_approved_plan(plan_path: Path, *, expected_source_url: str | None = None) -> dict[str, Any]:
    """Enforce cross-field recruiter invariants that JSON Schema cannot express."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    required_location_from_plan(plan)
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

    must = (plan.get("traits") or {}).get("must_have") or []
    core_traits = {
        str(item.get("trait") or "").strip()
        for item in must
        if item.get("tier") == "core" and str(item.get("trait") or "").strip()
    }
    groups = plan.get("core_groups") or []
    if not core_traits:
        raise ValueError("approved plan must contain at least one core must-have trait")
    if not groups:
        raise ValueError("approved plan must contain at least one alternative all-of core group")
    oversized = [str(group.get("name") or "unnamed") for group in groups
                 if len(group.get("all_of") or []) > 3]
    if oversized:
        raise ValueError(f"approved core_groups may contain at most 3 traits: {oversized}")
    grouped_traits = {
        str(trait).strip()
        for group in groups
        for trait in (group.get("all_of") or [])
        if str(trait).strip()
    }
    missing = sorted(core_traits - grouped_traits)
    unknown = sorted(grouped_traits - core_traits)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"core traits absent from core_groups: {missing}")
        if unknown:
            details.append(f"core_groups reference non-core traits: {unknown}")
        raise ValueError("; ".join(details))
    default_groups = [group for group in groups if group.get("source") == "default"]
    if any(len(group.get("all_of") or []) != 1 for group in default_groups):
        raise ValueError("default core_groups must be singleton eligibility groups; mark reviewed paths as user or jd")
    if len(default_groups) == len(groups):
        default_traits = [str(group["all_of"][0]).strip() for group in default_groups]
        if len(default_traits) != len(core_traits) or set(default_traits) != core_traits:
            raise ValueError(
                "default core_groups must contain exactly one singleton for every core trait; "
                "mark deliberate reviewed paths as user or jd"
            )
    return plan


def plan_sha256(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    candidates = [
        run_dir / "master_union.jsonl",
        *sorted(run_dir.glob("epoch*/union.jsonl")),
        *sorted((run_dir / "judges").glob("*.jsonl")),
        *sorted((run_dir / "shortlist").glob("*.json")),
    ]
    return [path for path in candidates if path.exists()]


def bind_approved_plan(
    run_dir: Path,
    plan_path: Path,
    retrieval_identity: dict[str, Any],
    jd_path: Path | None = None,
) -> tuple[Path, str]:
    """Pin all reusable artifacts to one approved plan, JD source, and backend."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    digest = plan_sha256(plan)
    jd_digest = hashlib.sha256(jd_path.read_bytes()).hexdigest() if jd_path else None
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
            "run contains retrieval/judge artifacts without an approved-plan binding "
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
    binding_path.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    return canonical_plan_path, digest


def judge(edir: Path, candidates: list[dict[str, Any]], judge_kind: str, effort: str, concurrency: int) -> None:
    jdir = stage_judge_input(edir, candidates)
    raw = jdir / "candidate_evaluations.raw.jsonl"
    if judge_kind == "gpt":
        # gpt-5.4 rerank on the FLEX tier (~50% cheaper batch tier); flex is slower + can 429, so
        # give it a generous timeout (the judge retries transient errors internally).
        run([sys.executable, GPT_JUDGE, "--run-dir", jdir, "--concurrency", concurrency,
             "--reasoning-effort", effort, "--service-tier", "flex", "--timeout", 600])
    else:
        run([sys.executable, CODEX_JUDGE, "--run-dir", jdir, "--concurrency", concurrency, "--reasoning-effort", effort])
    if not raw.exists():
        raise CommandError(["judge", judge_kind], missing=[raw], description=f"{judge_kind} judge")
    shutil.copyfile(raw, edir / "candidate_evaluations.raw.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description="The $search deep-mode convergence loop (source -> judge -> expand) until converged.")
    ap.add_argument("--jd-file", default=None, help="Path to JD text. Provide this OR --jd-url.")
    ap.add_argument("--jd-url", default=None, help="Job-posting URL; fetched to <run-dir>/jd.txt via fetch_jd before sourcing.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--backend", choices=("powerset", "local"), default=None, help="Sourcing backend. Defaults from <run-dir>/decision.json, else powerset; local = DuckDB")
    ap.add_argument("--decision", default=None, help="decision.json override. If present, surface=people/depth=deep/backend are enforced")
    ap.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb", help="Local DuckDB path (used only with --backend local)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--created-at", required=True, help="ISO timestamp for the plan")
    ap.add_argument("--max-epochs", type=int, default=3, help="Total epochs incl. epoch 0 (converge-capped)")
    ap.add_argument("--score-threshold", type=float, default=0.40, help="Shortlist cutoff on the canonical score")
    ap.add_argument("--sendable-threshold", type=float, default=0.55,
                    help="Sendable shortlist cutoff on the canonical score (provisional default: 0.55)")
    ap.add_argument("--judge", choices=["codex", "gpt"], default=os.environ.get("POWERPACKS_DEEP_JUDGE", "codex"),
                    help="Phase-2 judge engine: codex = free (subscription, slower); gpt = paid gpt-5.4 on the flex tier (fast). Default from POWERPACKS_DEEP_JUDGE env, else codex.")
    ap.add_argument("--triage", action=argparse.BooleanOptionalAction, default=True,
                    help="Phase-1 cheap conservative filter (triage_candidates) over each epoch's frontier before the judge; --no-triage judges the full frontier")
    ap.add_argument("--micro-sort", action=argparse.BooleanOptionalAction, default=False,
                    help="OPT-IN final ordering pass: micro-sort the shortlist's saturated score bands "
                         "(<=10 fast-model calls); judge scores untouched. Non-default per the "
                         "anti-local-maxima rule: measured neutral on the audited 22-person benchmark; "
                         "needs validation on a benchmark that can score top-band ordering")
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--n", type=int, default=16, help="seeds per robust_source round (epoch 0)")
    ap.add_argument("--keep", type=int, default=200)
    ap.add_argument("--anchors", type=int, default=6, help="diverse anchors expanded per Phase-2 epoch")
    ap.add_argument("--approved-plan", default=None, help="Reviewed plan.json to use without calling the plan LLM")
    ap.add_argument("--plan-approved", action="store_true", help="Resume with the existing <run-dir>/epoch0/plan.json after human review")
    ap.add_argument(
        "--preferences",
        default=None,
        help="Recruiter-preferences JSON used only when generating the pre-Review plan",
    )
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

    judges_dir = run_dir / "judges"
    judges_dir.mkdir(parents=True, exist_ok=True)
    master_judge = judges_dir / "loop.jsonl"      # accumulated verdicts (one growing judge file)
    master_union_path = run_dir / "master_union.jsonl"

    master_union: dict[str, dict[str, Any]] = {}
    judged_pids: set[str] = set()
    strong_pids: set[str] = set()
    epoch0_dir = run_dir / "epoch0"
    plan_path = Path(args.approved_plan) if args.approved_plan else epoch0_dir / "plan.json"
    if args.approved_plan and not plan_path.exists():
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "approved plan not found", "plan": str(plan_path)}, indent=2))
        raise SystemExit(1)
    if args.plan_approved and args.approved_plan:
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "use only one of --plan-approved or --approved-plan"}, indent=2))
        raise SystemExit(1)
    if args.preferences and (args.plan_approved or args.approved_plan):
        print(json.dumps({
            "primitive": "deep_search_loop",
            "status": "failed",
            "error": "--preferences is only valid while generating a new pre-Review plan",
        }, indent=2))
        raise SystemExit(1)
    if args.plan_approved and not plan_path.exists():
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": "--plan-approved requires existing epoch0/plan.json", "plan": str(plan_path)}, indent=2))
        raise SystemExit(1)
    # Retry/resume safety: if a previous approved run already judged some candidates, do not
    # rejudge them blindly on process restart. First gate resume has no such files, so this is a no-op.
    master_union = {r["person_id"]: r for r in _jsonl(master_union_path) if r.get("person_id")}
    for v in _jsonl(master_judge):
        if v.get("error"):
            continue  # a transient judge failure is not a verdict; leave the pid re-judgeable
        pid = v.get("person_id") or v.get("candidate_id")
        if pid:
            judged_pids.add(pid)
    existing_shortlist = run_dir / "shortlist" / "ground_truth_ranked.json"
    if existing_shortlist.exists():
        try:
            strong_pids = {r["person_id"] for r in json.loads(existing_shortlist.read_text()) if r.get("person_id")}
        except (json.JSONDecodeError, OSError):
            strong_pids = set()
    history: list[dict[str, Any]] = []

    try:
        epoch0_dir.mkdir(parents=True, exist_ok=True)
        if not args.plan_approved and not args.approved_plan:
            # The single human Review happens before all retrieval. This makes user edits to core
            # requirements, ranking preferences, stage, seniority, and location authoritative for
            # epoch-0 sourcers rather than judge-only corrections after the recall pool is fixed.
            existing_plan = plan_path.exists()
            if not existing_plan:
                build_cmd: list[object] = [
                    sys.executable, BUILD, "--run-dir", epoch0_dir, "--jd-file", args.jd_file,
                    "--created-at", args.created_at, "--plan-only",
                ]
                if args.jd_url:
                    build_cmd += ["--source-url", args.jd_url]
                if args.set_id:
                    build_cmd += ["--set-id", args.set_id]
                if args.preferences:
                    build_cmd += ["--preferences", args.preferences]
                run(build_cmd, expected_paths=[plan_path], description="build recruiter plan")
                try:
                    run([sys.executable, VALIDATE, "--schema", "search-network-jd-plan", "--file", plan_path],
                        description="validate recruiter plan")
                except CommandError as exc:
                    raise CommandError(exc.cmd, returncode=exc.returncode, stdout=exc.stdout_tail,
                                       stderr=exc.stderr_tail, missing=exc.missing,
                                       description="generated recruiter plan failed schema validation") from exc
                try:
                    run([sys.executable, CRITIC, "--plan", plan_path, "--jd-file", args.jd_file,
                         "--backend", args.backend],
                        description="plan critic")
                except CommandError:
                    # The critic is advisory; schema validation above is the hard contract check.
                    pass
            critic_path = epoch0_dir / "plan_critic.json"
            critic = load_advisory_critic(critic_path)
            history.append({
                "epoch": 0,
                "status": "awaiting_plan_approval",
                "plan": str(plan_path),
                "plan_critic": critic.get("verdict"),
                "source_started": False,
                "existing_plan": existing_plan,
            })
            (run_dir / "loop.json").write_text(json.dumps(history, indent=2) + "\n")
            print(json.dumps({
                "primitive": "deep_search_loop",
                "status": "awaiting_plan_approval",
                "plan": str(plan_path),
                "plan_critic": critic,
                "backend": args.backend,
                "decision": str(decision_path) if decision_path else None,
                "source_started": False,
                "next": "review/edit the plan, then rerun with --plan-approved",
            }, indent=2))
            return

        # Approval is an execution contract, not merely a flag. Reject malformed edited/external
        # plans before they can shape probes or spend.
        run([sys.executable, VALIDATE, "--schema", "search-network-jd-plan", "--file", plan_path],
            description="validate approved recruiter plan")
        try:
            approved_plan = validate_approved_plan(plan_path, expected_source_url=args.jd_url)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError(
                ["validate-approved-plan", str(plan_path)],
                returncode=2,
                stderr=str(exc),
                description="approved recruiter plan failed policy validation",
            ) from exc
        try:
            retrieval_identity, args.set_id, args.db = resolve_retrieval_identity(
                args.backend,
                approved_plan,
                args.set_id,
                args.db,
            )
            plan_path, approved_plan_sha256 = bind_approved_plan(
                run_dir,
                plan_path,
                retrieval_identity,
                Path(args.jd_file),
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError(
                ["bind-approved-plan", str(plan_path)],
                returncode=2,
                stderr=str(exc),
                description="approved recruiter plan binding failed",
            ) from exc

        for epoch in range(args.max_epochs):
            edir = run_dir / f"epoch{epoch}"
            edir.mkdir(parents=True, exist_ok=True)

            if epoch == 0:
                required = [edir / "union.jsonl", edir / "plan.json", edir / "candidate_frontier.jsonl", edir / "candidate_frontier.json", edir / "probe_summaries.json"]
                if not (edir / "union.jsonl").exists():
                    run([sys.executable, ROBUST, "--jd-file", args.jd_file, "--plan", plan_path,
                         "--run-dir", edir, "--env-file", args.env_file,
                         "--n", args.n, "--keep", args.keep, "--max-rounds", 2]
                        + (["--backend", "local", "--db", args.db] if args.backend == "local" else [])
                        + (["--set-id", args.set_id] if args.set_id else []),
                        expected_paths=[edir / "union.jsonl"], description="epoch0 robust_source")
                frontier_paths = required[2:]
                if not all(p.exists() for p in frontier_paths):
                    build_cmd = [sys.executable, BUILD, "--run-dir", edir, "--created-at", args.created_at,
                                 "--plan", plan_path]
                    if args.set_id:
                        build_cmd += ["--set-id", args.set_id]
                    run(build_cmd, expected_paths=required[1:], description="epoch0 build_eval_inputs")
                plan_path = edir / "plan.json"
            else:
                sr = run_dir / "shortlist" / "shortlist_ranked.json"
                if not sr.exists():  # compatibility with runs created before shortlist/bench split
                    sr = run_dir / "shortlist" / "ground_truth_ranked.json"
                strong = json.loads(sr.read_text()) if sr.exists() else []
                anchors = diverse_anchors(strong, master_union, args.anchors)
                if not anchors:
                    history.append({"epoch": epoch, "stopped": "no_anchors_giveup"})
                    break
                (edir / "anchors.json").write_text(json.dumps(anchors, indent=2))
                run(anchor_expansion_command(
                    edir / "anchors.json", plan_path, edir / "anchor_seeds.json", len(anchors)),
                    expected_paths=[edir / "anchor_seeds.json"], description=f"epoch{epoch} expand_from_anchor")
                run([sys.executable, WIDE_SEARCH, "--seeds", edir / "anchor_seeds.json", "--run-dir", edir, "--env-file", args.env_file,
                     "--limit", args.keep]
                    + (["--backend", "local", "--db", args.db] if args.backend == "local" else [])
                    + (["--set-id", args.set_id] if args.set_id else []),
                    expected_paths=[edir / "union.jsonl"], description=f"epoch{epoch} run_wide_search")
                build_cmd = [sys.executable, BUILD, "--run-dir", edir, "--plan", plan_path, "--created-at", args.created_at]
                if args.set_id:
                    build_cmd += ["--set-id", args.set_id]
                run(build_cmd, expected_paths=[edir / "plan.json", edir / "candidate_frontier.jsonl", edir / "candidate_frontier.json", edir / "probe_summaries.json"],
                    description=f"epoch{epoch} build_eval_inputs")

            # accumulate union; judge ONLY new pids without mutating canonical union artifacts
            for r in _jsonl(edir / "union.jsonl"):
                master_union.setdefault(r["person_id"], r)
            # Phase 1 — cheap conservative triage (keep/maybe pass; only clear misses drop) so
            # the expensive per-candidate judge sees a much smaller frontier. Resume-safe twice
            # over: it only runs when the frontier still has unjudged candidates, and
            # candidate_frontier.full.jsonl is the pre-triage backup/marker so a rerun never
            # re-filters survivors. Bulk filtering is ALWAYS the batched API filter — a CLI
            # agent engine (codex/claude) is a phase-2 judge only, never the thousands->hundreds
            # cut, and there is no fallback that hands it one: triage failure fails the run loud,
            # and --no-triage over a large frontier requires the API judge.
            triage_pool = None
            frontier = _jsonl(edir / "candidate_frontier.jsonl")
            pending = [c for c in frontier if (c.get("person_id") or c.get("candidate_id")) not in judged_pids]
            if args.triage:
                if pending and not (edir / "candidate_frontier.full.jsonl").exists():
                    run([sys.executable, TRIAGE, "--run-dir", edir, "--concurrency", max(args.concurrency, 8)],
                        expected_paths=[edir / "candidate_frontier.jsonl", edir / "candidate_frontier.full.jsonl"],
                        description=f"epoch{epoch} triage")
                    frontier = _jsonl(edir / "candidate_frontier.jsonl")
                if (edir / "candidate_frontier.full.jsonl").exists():
                    triage_pool = len(_jsonl(edir / "candidate_frontier.full.jsonl"))
            elif args.judge != "gpt" and len(pending) > MAX_CLI_JUDGE_FRONTIER:
                print(json.dumps({"primitive": "deep_search_loop", "status": "failed",
                                  "error": f"--no-triage with {len(pending)} unjudged candidates and --judge {args.judge}: "
                                           f"CLI agent engines never do bulk filtering (max {MAX_CLI_JUDGE_FRONTIER} untriaged). "
                                           "Re-enable triage (the default) or use --judge gpt."}, indent=2))
                raise SystemExit(1)
            new = [c for c in frontier if (c.get("person_id") or c.get("candidate_id")) not in judged_pids]
            (edir / "candidate_frontier.to_judge.jsonl").write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in new))
            new_judged = 0
            judge_errors_dropped = 0
            if new:
                judge(edir, new, args.judge, args.reasoning_effort, args.concurrency)
                verds = _jsonl(edir / "candidate_evaluations.raw.jsonl")
                # A transient judge failure (timeout/429/unparsable) writes a synthetic 0.0 "out"
                # verdict with an `error` marker. That must not become a cached rejection: retry
                # the errored candidates once in-epoch, then drop any that still errored — they
                # are never appended to the master judge file and stay re-judgeable.
                errored = {v.get("person_id") or v.get("candidate_id") for v in verds if v.get("error")}
                if errored:
                    retry = [c for c in new if (c.get("person_id") or c.get("candidate_id")) in errored]
                    judge(edir, retry, args.judge, args.reasoning_effort, args.concurrency)
                    retried = _jsonl(edir / "candidate_evaluations.raw.jsonl")
                    verds = [v for v in verds if (v.get("person_id") or v.get("candidate_id")) not in errored] + retried
                    (edir / "candidate_evaluations.raw.jsonl").write_text("".join(json.dumps(v) + "\n" for v in verds))
                ok_verds = [v for v in verds if not v.get("error")]
                judge_errors_dropped = len(verds) - len(ok_verds)
                with master_judge.open("a") as fh:
                    for v in ok_verds:
                        fh.write(json.dumps(v) + "\n")
                judged_pids |= {v.get("person_id") or v.get("candidate_id") for v in ok_verds}
                new_judged = len(ok_verds)
            master_union_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in master_union.values()))

            # consensus over everything judged so far
            run([sys.executable, CONSENSUS, "--judges-dir", judges_dir, "--union", master_union_path,
                 "--out-dir", run_dir / "shortlist", "--min-inband-votes", 1, "--min-notout-votes", 1,
                 "--score-threshold", args.score_threshold,
                 "--sendable-threshold", args.sendable_threshold,
                 "--plan", plan_path],
                expected_paths=[run_dir / "shortlist" / "consensus.json",
                                run_dir / "shortlist" / "shortlist_ranked.json",
                                run_dir / "shortlist" / "sendable_ranked.json",
                                run_dir / "shortlist" / "bench_ranked.json"],
                description=f"epoch{epoch} consensus")  # core-gate the shortlist on the plan's core domain must-haves
            strong_now = json.loads((run_dir / "shortlist" / "shortlist_ranked.json").read_text())
            now_pids = {r["person_id"] for r in strong_now}
            new_strong = now_pids - strong_pids
            history.append({"epoch": epoch, "phase": "jd" if epoch == 0 else "anchor",
                            "plan_sha256": approved_plan_sha256,
                            "triage_pool": triage_pool, "frontier": len(frontier),
                            "new_judged": new_judged, "judged_total": len(judged_pids),
                            "judge_errors_dropped": judge_errors_dropped,
                            "score_threshold": args.score_threshold,
                            "sendable_threshold": args.sendable_threshold,
                            "strong_total": len(now_pids), "new_strong": len(new_strong)})
            print(json.dumps(history[-1]))
            strong_pids = now_pids
            if epoch > 0 and len(new_strong) == 0:
                history[-1]["stopped"] = "converged"
                break
    except CommandError as exc:
        history.append({"status": "failed", "error": str(exc), "details": exc.to_dict()})
        (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
        print(json.dumps({"primitive": "deep_search_loop", "status": "failed", "error": str(exc), "details": exc.to_dict(), "history": history}, indent=2))
        raise SystemExit(1) from exc

    # Final ordering pass: micro-sort (agentic merge sort, ported from network-search-api)
    # reorders the saturated top bands using the judge's own evidence. Judge scores are
    # untouched; a failure keeps the score ordering. Cheap: <=10 fast-model calls.
    ranked_final = None
    shortlist_path = run_dir / "shortlist" / "sendable_ranked.json"
    if args.micro_sort and shortlist_path.exists():
        try:
            run([sys.executable, MICROSORT, "--run-dir", run_dir, "--input", shortlist_path],
                description="micro-sort sendable shortlist")
            ranked_final = str(run_dir / "shortlist" / "ranked_final.json")
        except Exception as exc:
            history.append({"micro_sort": "failed", "error": str(exc)[:200]})

    (run_dir / "loop.json").write_text(json.dumps(history, indent=2))
    print(json.dumps({"primitive": "deep_search_loop", "status": "completed", "epochs": len(history),
                      "strong_total": len(strong_pids), "shortlist": str(shortlist_path),
                      "ranked_final": ranked_final,
                      "history": history}, indent=2))


if __name__ == "__main__":
    main()
