"""Reflect bench CLI: score a deep-search run against GT, aggregate a suite report, gate changes.

The maintainer-side half of the Reflect family (see README.md in this directory). Three
subcommands, all pure local file math over existing run artifacts — no network, no spend:

  score   one run dir + one GT file -> funnel + gaps(+ndcg) + cost -> \
          .powerpacks/reflect/results/<slug>/result.json (fixed path, overwritten in place)
  report  all results + suite metas -> .powerpacks/reflect/report.json (per-JD rows,
          per-job-family aggregates; suite entries without a result are listed gt-pending)
  gate    current report vs a baseline report -> strict corpus-bound quality verdicts.

Scorer primitives are invoked in-process (argv-driven main()), never as subprocesses.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.search.reflect.review import (  # noqa: E402
    build_review_packet, finalize_human_labels, merge_human_labels, parse_timestamp,
    validate_ground_truth_semantics,
)
from packs.search.reflect.snapshots import (  # noqa: E402
    canonical_hash, snapshot_identity, validate_complete_evidence, validate_snapshot,
)
from packs.search.pipeline.frontier import CANDIDATE_FRONTIER_NAME  # noqa: E402
from packs.search.pipeline.artifacts import REVIEW_EVIDENCE_NAME, ReviewEvidenceSnapshot  # noqa: E402
from packs.search.pipeline.stage_membership import STAGE_MEMBERSHIP_NAME  # noqa: E402
from packs.search.pipeline.models import SearchSpec  # noqa: E402

DEEP_SEARCH_DIR = ROOT / "packs" / "search" / "primitives" / "deep_search"
SUITE_DIR = Path(__file__).resolve().parent / "suite"
REFLECT_STATE = ROOT / ".powerpacks" / "reflect"
POWERPACKS_STATE = ROOT / ".powerpacks"
RESULTS_DIR = REFLECT_STATE / "results"
REPORT_PATH = REFLECT_STATE / "report.json"
COMPARISON_REVIEW_PATH = REFLECT_STATE / "comparison-review.json"
GT_DIR = REFLECT_STATE / "gt"

RECALL_METRICS = ("recall@10", "recall@25")
NDCG_METRICS = ("ndcg@10", "ndcg@25")
IDENTITY_FIELDS = ("corpus_hash", "case_hash", "evidence_hash", "label_hash")
STAGE_METRICS = ("source_recall", "hydration_coverage", "hard_filter_survival", "triage_survival")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_cli_main(mod, argv: list[str]) -> None:
    saved = sys.argv
    sys.argv = argv
    try:
        mod.main()
    finally:
        sys.argv = saved


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_hash(path: Path) -> str:
    """Hash exact bytes, including report whitespace and trailing newline."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validator():
    return _load_module("validate_artifact", ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py")


def _validate(schema: str, path: Path) -> dict[str, Any]:
    return _validator().validate_file(schema, path)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _local_output(path: str | None, default: Path) -> Path:
    resolved = (Path(path) if path else default).resolve()
    try:
        resolved.relative_to(REFLECT_STATE.resolve())
    except ValueError as exc:
        raise ValueError(f"Reflect artifacts must remain under {REFLECT_STATE}") from exc
    return resolved


def _require_local_inputs(*paths: str | Path | None) -> None:
    for raw in paths:
        if raw is None:
            continue
        if not Path(raw).resolve().is_relative_to(REFLECT_STATE.resolve()):
            raise ValueError(f"Reflect lifecycle artifacts must remain under {REFLECT_STATE}")


def _case_document(path: Path) -> tuple[dict[str, Any], str]:
    case = _validate("reflect-case", path)
    if case["reviewed_search_spec"]["content_hash"] != canonical_hash(case["reviewed_search_spec"]["content"]):
        raise ValueError("case reviewed_search_spec content_hash does not match content")
    return case, _file_hash(path)


def _stage_metrics(funnel: dict[str, Any]) -> dict[str, float | None]:
    stages = {row["stage"]: row["gt_survived"] for row in funnel.get("funnel") or []}
    total = stages.get("ground_truth")
    if not total:
        return {metric: None for metric in STAGE_METRICS}
    return {
        "source_recall": round(stages["sourced"] / total, 4) if "sourced" in stages else None,
        "hydration_coverage": round(stages["hydrated"] / total, 4) if "hydrated" in stages else None,
        "hard_filter_survival": round(stages["hard_filter_survived"] / total, 4)
        if "hard_filter_survived" in stages else None,
        "triage_survival": round(stages["triage_survived"] / total, 4) if "triage_survived" in stages else None,
    }


def _run_artifacts(
    run_dir: Path,
) -> tuple[dict[str, Any], SearchSpec, dict[str, Any], dict[str, Any], ReviewEvidenceSnapshot]:
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != "search.manifest.v1" or not isinstance(manifest.get("artifacts"), dict):
        raise ValueError("strict scoring requires a canonical search.manifest.v1")
    required = {
        "search_spec_json": "search_spec.json",
        "review_plan_json": "review/plan.json",
        "review_binding_json": "review/binding.json",
        "review_corpus_json": "review/corpus.json",
        "review_source_json": "review/source.json",
        "review_evidence_json": REVIEW_EVIDENCE_NAME,
        STAGE_MEMBERSHIP_NAME: STAGE_MEMBERSHIP_NAME,
        CANDIDATE_FRONTIER_NAME: CANDIDATE_FRONTIER_NAME,
        "hard_filter_validation_json": "hard-filter-validation.json",
    }
    for key, relative in required.items():
        entry = manifest["artifacts"].get(key)
        if not isinstance(entry, dict) or entry.get("path") != relative:
            raise ValueError(f"run manifest is missing canonical artifact: {key}")
        artifact = run_dir / relative
        if not artifact.is_file() or entry.get("sha256") != _file_hash(artifact):
            raise ValueError(f"run manifest hash does not match artifact: {relative}")
    raw_spec = _validate("search-spec", run_dir / "search_spec.json")
    spec = SearchSpec.from_dict(raw_spec)
    binding = _read_json(run_dir / "review/binding.json")
    plan = _read_json(run_dir / "review/plan.json")
    corpus = _read_json(run_dir / "review/corpus.json")
    source = _read_json(run_dir / "review/source.json")
    evidence = ReviewEvidenceSnapshot.from_dict(_read_json(run_dir / REVIEW_EVIDENCE_NAME))
    stable_corpus = {key: value for key, value in corpus.items() if key != "observed_at"}
    if binding.get("schema_version") != "recruiting.review-binding.v1":
        raise ValueError("run review binding has an unsupported schema_version")
    if binding.get("corpus") != stable_corpus or binding.get("corpus_sha256") != canonical_hash(stable_corpus):
        raise ValueError("run review/corpus binding does not match persisted corpus")
    if evidence.evidence_hashes != corpus.get("evidence_hashes"):
        raise ValueError("run review evidence does not match the persisted review corpus")
    if (
        spec.recruiting is None
        or spec.recruiting.reviewed_plan_hash != binding.get("plan_sha256")
        or binding.get("plan_sha256") != canonical_hash(plan)
    ):
        raise ValueError("persisted SearchSpec does not bind the reviewed run plan")
    requested_pool = list(spec.recruiting.review_pool_person_ids)
    if (
        binding.get("review_pool_person_ids") != requested_pool
        or binding.get("review_pool_person_ids_sha256") != canonical_hash(requested_pool)
        or set(requested_pool) != set(evidence.evidence_hashes)
    ):
        raise ValueError("persisted SearchSpec does not bind the reviewed evidence pool")
    if (
        binding.get("source_sha256") != canonical_hash(spec.recruiting.source)
        or binding.get("jd_sha256") != canonical_hash(source.get("normalized_jd"))
    ):
        raise ValueError("persisted SearchSpec does not bind the reviewed run source")
    return manifest, spec, binding, corpus, evidence


def _strict_snapshot(path: str | Path) -> dict[str, Any]:
    """Load a corpus snapshot that is strictly comparable, refusing anything weaker.

    `_validate` is a JSON-schema SHAPE check and deliberately accepts the cheap
    `tagged_metadata_non_comparable` snapshot, which `cmd_score` later refuses. Human
    labelling is the slowest and most expensive step in Reflect, so the whole GT
    lifecycle gates here too: a reviewer must never spend a review pass building
    ground truth that cannot be scored.
    """
    snapshot = _validate("reflect-corpus-snapshot", Path(path))
    errors = validate_snapshot(snapshot)
    if errors:
        raise ValueError("; ".join(errors))
    return snapshot


def _corpus_contract(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"schema_version", "observed_at", "evidence_hashes"}
    }


def _validate_hard_filter(validation: dict[str, Any], *, case_id: str, case_hash: str,
                          corpus_hash: str, reviewed_count: int) -> int:
    parse_timestamp(validation["generated_at"], "hard-filter validation generated_at")
    if validation["case_id"] != case_id or validation["case_hash"] != case_hash:
        raise ValueError("hard-filter validation does not match case binding")
    if validation["corpus_snapshot_hash"] != corpus_hash:
        raise ValueError("hard-filter validation does not match corpus snapshot")
    if validation["reviewed_count"] != reviewed_count:
        raise ValueError("hard-filter validation reviewed_count does not match finalized review pool")
    if validation["violation_count"] != len(validation["violations"]):
        raise ValueError("hard-filter validation violation_count does not match violations")
    if validation["violation_count"] > validation["reviewed_count"]:
        raise ValueError("hard-filter validation violations exceed reviewed_count")
    violation_ids = [row["person_id"] for row in validation["violations"]]
    if len(violation_ids) != len(set(violation_ids)):
        raise ValueError("hard-filter validation contains duplicate violation person IDs")
    return validation["violation_count"]


def _strict_report_rows(report: dict[str, Any], name: str) -> list[dict[str, Any]]:
    rows = report.get("jds")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{name} report must contain at least one JD row")
    slugs = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{name} report row {index} must be an object")
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError(f"{name} report row {index} has missing/empty slug")
        slugs.append(slug)
        for field in IDENTITY_FIELDS:
            if not isinstance(row.get(field), str) or not HASH_RE.fullmatch(row[field]):
                raise ValueError(f"{name} report {slug} has invalid {field}")
        metrics = (*STAGE_METRICS, "overall_recall", *RECALL_METRICS, *NDCG_METRICS,
                   *(key for key in row if key.startswith("precision@")))
        for metric in metrics:
            value = row.get(metric)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} report {slug} has invalid {metric}")
        for field in ("hard_filter_violations", "unreviewed_candidate_count"):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} report {slug} has invalid {field}")
    if len(slugs) != len(set(slugs)):
        raise ValueError(f"{name} report contains duplicate slugs")
    return rows


def cmd_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    slug = args.slug or run_dir.name
    reflect_dir = run_dir / "reflect"
    evidence_args = (getattr(args, "case", None), getattr(args, "snapshot", None),
                     getattr(args, "hard_filter_validation", None))
    if not all(evidence_args):
        raise ValueError("strict scoring requires --case, --snapshot, and --hard-filter-validation")
    strict = True

    raw_ground_truth = _read_json(Path(args.gt))
    if not isinstance(raw_ground_truth, dict) or raw_ground_truth.get("schema_version") != "reflect.ground_truth.v1":
        raise ValueError("ground truth must use reflect.ground_truth.v1")

    case = snapshot = ground_truth = None
    case_hash = corpus_hash = evidence_hash_value = label_hash = None
    hard_filter_violations = None
    meta_path = SUITE_DIR / slug / "meta.json"
    meta = _read_json(meta_path) if meta_path.exists() else None
    if strict:
        if not run_dir.is_relative_to(POWERPACKS_STATE.resolve()):
            raise ValueError("strict scoring requires run_dir under repository .powerpacks/")
        _require_local_inputs(args.case, args.snapshot, args.gt)
        manifest, run_spec, run_binding, run_corpus, run_evidence = _run_artifacts(run_dir)
        case_path = Path(args.case)
        case, case_hash = _case_document(case_path)
        if case["reviewed_search_spec"]["content"] != run_spec.to_dict():
            raise ValueError("case reviewed_search_spec does not match the persisted run SearchSpec")
        if case["public_source"]["content_hash"] != run_binding["jd_sha256"]:
            raise ValueError("case public_source content_hash does not match the normalized run JD")
        ground_truth = _validate("reflect-ground-truth", Path(args.gt))
        validate_ground_truth_semantics(ground_truth)
        snapshot = _validate("reflect-corpus-snapshot", Path(args.snapshot))
        corpus_hash = snapshot_identity(snapshot)
        errors = validate_snapshot(snapshot, ground_truth["review_pool_evidence_hashes"])
        expected_rows = [{"person_id": person_id, "evidence_hash": evidence}
                         for person_id, evidence in ground_truth["review_pool_evidence_hashes"].items()]
        errors += validate_complete_evidence(expected_rows, snapshot)
        if ground_truth["corpus_snapshot_hash"] != corpus_hash:
            errors.append("ground truth corpus_snapshot_hash does not match snapshot")
        if _corpus_contract(snapshot) != _corpus_contract(run_corpus):
            errors.append("reviewed corpus snapshot does not match the run review/corpus binding")
        if snapshot["evidence_hashes"] != run_evidence.evidence_hashes:
            errors.append("review-pool evidence snapshot does not match the reviewed run")
        if ground_truth["review_pool_evidence_hash"] != run_evidence.evidence_hash:
            errors.append("ground-truth review-pool evidence hash does not match the reviewed run")
        if not meta_path.exists():
            errors.append(f"suite metadata is required for case: {slug}")
        expected_case_id = (meta or {}).get("case_id")
        if case["case_id"] != expected_case_id or ground_truth["case_id"] != case["case_id"]:
            errors.append("case_id does not match suite metadata and ground truth")
        if ground_truth["case_hash"] != case_hash:
            errors.append("ground truth case_hash does not match exact case bytes")
        if errors:
            raise ValueError("; ".join(errors))
        validation_path = Path(args.hard_filter_validation).resolve()
        if validation_path != (run_dir / "hard-filter-validation.json").resolve():
            raise ValueError("strict scoring requires the run-produced hard-filter artifact")
        validation = _validate("reflect-hard-filter-validation", validation_path)
        membership = _validate("stage-membership", run_dir / STAGE_MEMBERSHIP_NAME)
        if (
            membership["score_floor"] != run_spec.bounds.score_floor
            or membership["sendable_score"] != run_spec.bounds.sendable_score
            or membership["frontier_limit"] != run_spec.bounds.frontier_limit
        ):
            raise ValueError("stage membership scoring bounds do not match the persisted SearchSpec")
        hard_filter_violations = _validate_hard_filter(
            validation,
            case_id="production",
            case_hash=canonical_hash(run_spec.to_dict()),
            corpus_hash=run_binding["corpus_sha256"],
            reviewed_count=membership["total_sourced"],
        )
        expected_violations = {
            row["person_id"] for row in membership["candidates"]
            if row["disposition"] == "hard_filter_quarantined"
        }
        if {row["person_id"] for row in validation["violations"]} != expected_violations:
            raise ValueError("run hard-filter artifact does not match stage membership dispositions")
        evidence_hash_value = ground_truth["review_pool_evidence_hash"]
        label_hash = canonical_hash(ground_truth["labels"])

    sf = _load_module("score_funnel", DEEP_SEARCH_DIR / "score_funnel.py")
    stage_membership = run_dir / STAGE_MEMBERSHIP_NAME
    candidate_frontier = run_dir / CANDIDATE_FRONTIER_NAME
    _run_cli_main(sf, [
        "score_funnel",
        "--stage-membership", str(stage_membership),
        "--candidate-frontier", str(candidate_frontier),
        "--ground-truth", args.gt,
    ])
    funnel = _read_json(reflect_dir / "funnel.json")

    sg = _load_module("score_ground_truth_gaps", DEEP_SEARCH_DIR / "score_ground_truth_gaps.py")
    argv = ["score_ground_truth_gaps", "--ground-truth", args.gt,
            "--candidate-frontier", str(candidate_frontier),
            "--out", str(reflect_dir / "gaps.json"), "--ks", args.ks]
    usage_log = Path(args.usage_log) if args.usage_log else run_dir / "usage.jsonl"
    if usage_log.exists():
        argv += ["--usage-log", str(usage_log)]
    _run_cli_main(sg, argv)
    gaps = _read_json(reflect_dir / "gaps.json")

    cost = None
    usage_log = Path(args.usage_log) if args.usage_log else run_dir / "usage.jsonl"
    if usage_log.exists():
        cr = _load_module("cost_report", Path(__file__).resolve().parent / "cost_report.py")
        cost = cr.build_report(cr.load_rows(usage_log), cr.load_prices(cr.DEFAULT_PRICES_PATH))

    result = {
        "slug": slug,
        "meta": meta,
        "corpus_hash": corpus_hash, "case_hash": case_hash,
        "evidence_hash": evidence_hash_value, "label_hash": label_hash,
        "funnel": {k: funnel[k] for k in ("gt_size", "funnel_line", "dispositions", "thresholds",
                                                     "shortlist_source", "funnel", "probe_attribution")},
        **_stage_metrics(funnel),
        "hard_filter_violations": hard_filter_violations,
        "gaps": gaps and {k: v for k, v in gaps.items() if k != "missed"},
        "missed_count": gaps and gaps.get("missed_count"),
        "unreviewed_candidate_count": gaps and gaps.get("unreviewed_candidate_count"),
        "cost": cost and cost["totals"],
        "generated_at": _now(),
    }
    out = _local_output(getattr(args, "out", None), RESULTS_DIR / slug / "result.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"score[{slug}]: {funnel['funnel_line']}", file=sys.stderr)
    print(json.dumps({"slug": slug, "result_json": str(out), "funnel_line": funnel["funnel_line"],
                      "dispositions": funnel["dispositions"],
                      "overall_recall": (gaps or {}).get("overall_recall"),
                      "cost_usd": (cost or {}).get("totals", {}).get("cost_usd")}, indent=2))
    return 0


def _metric_row(result: dict[str, Any]) -> dict[str, Any]:
    gaps = result.get("gaps") or {}
    meta = result.get("meta") or {}
    return {
        "slug": result["slug"],
        "job_family": meta.get("job_family", "unknown"),
        "expected_size_class": meta.get("expected_size_class"),
        "gt_size": (result.get("funnel") or {}).get("gt_size"),
        "funnel_line": (result.get("funnel") or {}).get("funnel_line"),
        "dispositions": (result.get("funnel") or {}).get("dispositions"),
        "funnel": (result.get("funnel") or {}).get("funnel"),
        "probe_attribution": (result.get("funnel") or {}).get("probe_attribution"),
        **{metric: result.get(metric) for metric in STAGE_METRICS},
        "hard_filter_violations": result.get("hard_filter_violations"),
        "unreviewed_candidate_count": result.get("unreviewed_candidate_count"),
        "overall_recall": gaps.get("overall_recall"),
        **{k: v for k, v in gaps.items() if k.startswith(("recall@", "precision@", "ndcg@"))},
        "cost_usd": (result.get("cost") or {}).get("cost_usd"),
        "generated_at": result.get("generated_at"),
        **{field: result.get(field) for field in IDENTITY_FIELDS},
    }


def cmd_report(args: argparse.Namespace) -> int:
    rows = []
    results_dir = Path(getattr(args, "results_dir", None) or RESULTS_DIR)
    if not results_dir.resolve().is_relative_to(REFLECT_STATE.resolve()):
        raise ValueError("Reflect results must remain under .powerpacks/reflect")
    for result_path in sorted(results_dir.glob("*/result.json")):
        rows.append(_metric_row(_read_json(result_path)))
    slugs = [row["slug"] for row in rows]
    if len(slugs) != len(set(slugs)):
        raise ValueError("duplicate report slugs")
    scored = {r["slug"] for r in rows}
    pending = []
    if SUITE_DIR.exists():
        for meta_path in sorted(SUITE_DIR.glob("*/meta.json")):
            slug = meta_path.parent.name
            if slug not in scored:
                meta = _read_json(meta_path)
                pending.append({"slug": slug, "job_family": meta.get("job_family", "unknown"),
                                "gt": meta.get("gt", "pending")})

    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        fam = families.setdefault(row["job_family"], {"jds": 0, "recalls": [], "costs": []})
        fam["jds"] += 1
        if row.get("overall_recall") is not None:
            fam["recalls"].append(row["overall_recall"])
        if row.get("cost_usd") is not None:
            fam["costs"].append(row["cost_usd"])
    aggregates = {
        fam: {
            "jds": data["jds"],
            "mean_recall": round(sum(data["recalls"]) / len(data["recalls"]), 4) if data["recalls"] else None,
            "min_recall": min(data["recalls"]) if data["recalls"] else None,
            "total_cost_usd": round(sum(data["costs"]), 4) if data["costs"] else None,
        }
        for fam, data in sorted(families.items())
    }

    report = {"generated_at": _now(), "jds": rows, "gt_pending": pending, "by_job_family": aggregates}
    report_path = _local_output(getattr(args, "out", None), REPORT_PATH)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for row in rows:
        print(f"report: {row['slug']} [{row['job_family']}] recall={row.get('overall_recall')} "
              f"cost=${row.get('cost_usd') or 0}", file=sys.stderr)
    print(json.dumps({"report_json": str(report_path), "jds_scored": len(rows),
                      "gt_pending": len(pending), "by_job_family": aggregates}, indent=2))
    return 0


def cmd_build_review(args: argparse.Namespace) -> int:
    _require_local_inputs(args.case, args.snapshot, args.candidates)
    case, case_hash = _case_document(Path(args.case))
    snapshot = _strict_snapshot(args.snapshot)
    candidates = _read_json(Path(args.candidates))
    if not isinstance(candidates, list):
        raise ValueError("review candidates must be a JSON list")
    packet = build_review_packet(case_id=case["case_id"], case_hash=case_hash,
                                 corpus_snapshot_hash=snapshot_identity(snapshot), candidates=candidates)
    out = _local_output(args.out, GT_DIR / case["case_id"] / "review-packet.json")
    _write_json(out, packet)
    _validate("reflect-review-packet", out)
    print(json.dumps({"review_packet": str(out)}, indent=2))
    return 0


def cmd_resume_labels(args: argparse.Namespace) -> int:
    _require_local_inputs(args.packet, args.previous)
    packet = _validate("reflect-review-packet", Path(args.packet))
    previous = _validate("reflect-human-labels", Path(args.previous)) if args.previous else None
    labels = merge_human_labels(packet, previous)
    out = _local_output(args.out, GT_DIR / packet["case_id"] / "human-labels.json")
    _write_json(out, labels)
    _validate("reflect-human-labels", out)
    print(json.dumps({"human_labels": str(out)}, indent=2))
    return 0


def cmd_finalize_labels(args: argparse.Namespace) -> int:
    _require_local_inputs(args.packet, args.labels, args.snapshot)
    packet = _validate("reflect-review-packet", Path(args.packet))
    labels = _validate("reflect-human-labels", Path(args.labels))
    snapshot = _strict_snapshot(args.snapshot)
    gt = finalize_human_labels(packet, labels, snapshot)
    out = _local_output(args.out, GT_DIR / packet["case_id"] / "ground-truth.json")
    _write_json(out, gt)
    _validate("reflect-ground-truth", out)
    print(json.dumps({"ground_truth": str(out)}, indent=2))
    return 0


def _review_regressions(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [regression for verdict in verdicts for regression in verdict.get("review_regressions", [])]


def _review_template(baseline_path: Path, current_path: Path, regressions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "reflect.comparison_review.v1",
        "baseline_report_hash": _file_hash(baseline_path),
        "candidate_report_hash": _file_hash(current_path),
        "decision": "rejected",
        "explanation": "Replace with the joint comparison review rationale.",
        "reviewer": "REQUIRED",
        "reviewed_at": "1970-01-01T00:00:00Z",
        "regressions": regressions,
    }


def _review_matches(review: dict[str, Any], template: dict[str, Any]) -> bool:
    try:
        parse_timestamp(review.get("reviewed_at"), "comparison review reviewed_at")
    except ValueError:
        return False
    return (
        review.get("decision") == "accepted"
        and isinstance(review.get("reviewer"), str) and bool(review["reviewer"].strip())
        and isinstance(review.get("explanation"), str) and bool(review["explanation"].strip())
        and review.get("reviewer") != "REQUIRED"
        and review.get("explanation") != template["explanation"]
        and review.get("reviewed_at") != template["reviewed_at"]
        and review.get("baseline_report_hash") == template["baseline_report_hash"]
        and review.get("candidate_report_hash") == template["candidate_report_hash"]
        and review.get("regressions") == template["regressions"]
    )


def cmd_gate(args: argparse.Namespace) -> int:
    baseline_path, current_path = Path(args.baseline), Path(args.current)
    baseline_rows = _strict_report_rows(_read_json(baseline_path), "baseline")
    current_rows = _strict_report_rows(_read_json(current_path), "current")
    baseline = {r["slug"]: r for r in baseline_rows}
    current = {r["slug"]: r for r in current_rows}

    verdicts = []
    for slug, base_row in sorted(baseline.items()):
        cur_row = current.get(slug)
        if cur_row is None:
            verdicts.append({"slug": slug, "verdict": "fail", "reason": "JD present in baseline but not in current report"})
            continue
        non_comparable = []
        for field in IDENTITY_FIELDS:
            if base_row[field] != cur_row[field]:
                non_comparable.append(f"changed {field}")
        if base_row["unreviewed_candidate_count"] > 0 or cur_row["unreviewed_candidate_count"] > 0:
            non_comparable.append("candidate output contains IDs outside the finalized review pool")
        if non_comparable:
            verdicts.append({"slug": slug, "verdict": "non_comparable", "reasons": non_comparable})
            continue
        reasons = []
        review_regressions = []
        for metric in STAGE_METRICS:
            base_v, cur_v = base_row.get(metric), cur_row.get(metric)
            if cur_v < base_v:
                reasons.append(f"{metric} regressed {base_v} -> {cur_v}")
        if cur_row["hard_filter_violations"] > 0:
            reasons.append(f"hard_filter_violations is {cur_row['hard_filter_violations']}; required 0")
        for metric in RECALL_METRICS:
            base_v, cur_v = base_row.get(metric), cur_row.get(metric)
            if cur_v < base_v:
                reasons.append(f"{metric} regressed {base_v} -> {cur_v}")
        for metric in NDCG_METRICS:
            base_v, cur_v = base_row.get(metric), cur_row.get(metric)
            drop = base_v - cur_v
            if drop > 0.02 + 1e-12:
                reasons.append(f"{metric} regressed {base_v} -> {cur_v} (drop {drop:.4f} > 0.02)")
            elif drop > 0:
                review_regressions.append({
                    "case_id": slug, "corpus_hash": base_row["corpus_hash"], "metric": metric,
                    "k": int(metric.split("@")[1]), "baseline_score": base_v,
                    "candidate_score": cur_v, "delta": round(cur_v - base_v, 10),
                })
        if args.min_recall is not None and (cur_row.get("overall_recall") or 0) < args.min_recall:
            reasons.append(f"overall_recall {cur_row.get('overall_recall')} below floor {args.min_recall}")
        if args.max_cost is not None and (cur_row.get("cost_usd") or 0) > args.max_cost:
            reasons.append(f"cost_usd {cur_row.get('cost_usd')} above ceiling {args.max_cost}")
        verdict = "fail" if reasons else ("needs_review" if review_regressions else "pass")
        verdicts.append({"slug": slug, "verdict": verdict, "reasons": reasons,
                         "review_regressions": review_regressions})

    regressions = _review_regressions(verdicts)
    template = _review_template(baseline_path, current_path, regressions) if regressions else None
    if regressions and args.comparison_review:
        validator = _load_module("validate_artifact", ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py")
        try:
            review = validator.validate_file("reflect-comparison-review", Path(args.comparison_review))
        except ValueError as exc:
            for verdict in verdicts:
                if verdict["verdict"] == "needs_review":
                    verdict["verdict"], verdict["reasons"] = "fail", [f"invalid comparison review: {exc}"]
        else:
            if _review_matches(review, template):
                for verdict in verdicts:
                    if verdict["verdict"] == "needs_review":
                        verdict["verdict"] = "pass"
                        verdict["comparison_review"] = "accepted"
            else:
                for verdict in verdicts:
                    if verdict["verdict"] == "needs_review":
                        verdict["verdict"], verdict["reasons"] = "fail", ["comparison review is rejected, stale, or mismatched"]
    remaining_review = [v for v in verdicts if v["verdict"] == "needs_review"]
    if remaining_review:
        out = Path(args.review_template_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    failures = [v for v in verdicts if v["verdict"] in {"fail", "non_comparable"}]
    payload = {"mode": "strict", "jds_checked": len(verdicts), "failures": len(failures),
               "needs_review": len(remaining_review), "verdicts": verdicts}
    if remaining_review:
        payload["review_template"] = str(Path(args.review_template_out))
    for v in failures:
        print(f"gate[strict]: {v['verdict'].upper()} {v['slug']}: {'; '.join(v.get('reasons') or [v.get('reason', '')])}", file=sys.stderr)
    print(json.dumps(payload, indent=2))
    return 1 if failures or remaining_review else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Reflect bench: score, report, and gate deep-search quality.")
    sub = ap.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="Score one run dir against a GT file")
    score.add_argument("--run-dir", required=True)
    score.add_argument("--gt", required=True)
    score.add_argument("--case", default=None)
    score.add_argument("--slug", default=None)
    score.add_argument("--ks", default="10,25")
    score.add_argument("--usage-log", default=None)
    score.add_argument("--snapshot", default=None)
    score.add_argument("--hard-filter-validation", default=None)
    score.add_argument("--out", default=None)

    report = sub.add_parser("report", help="Aggregate all results into report.json")
    report.add_argument("--results-dir", default=None)
    report.add_argument("--out", default=None)

    build = sub.add_parser("build-review-packet", help="Build a local structured evidence review packet")
    build.add_argument("--case", required=True)
    build.add_argument("--snapshot", required=True)
    build.add_argument("--candidates", required=True)
    build.add_argument("--out", default=None)

    resume = sub.add_parser("resume-labels", help="Create or resume local human labels")
    resume.add_argument("--packet", required=True)
    resume.add_argument("--previous", default=None)
    resume.add_argument("--out", default=None)

    finalize = sub.add_parser("finalize-human-labels", help="Finalize authoritative local human ground truth")
    finalize.add_argument("--packet", required=True)
    finalize.add_argument("--labels", required=True)
    finalize.add_argument("--snapshot", required=True)
    finalize.add_argument("--out", default=None)

    gate = sub.add_parser("gate", help="Compare a current report against a baseline")
    gate.add_argument("--baseline", required=True)
    gate.add_argument("--current", default=str(REPORT_PATH))
    gate.add_argument("--min-recall", type=float, default=None)
    gate.add_argument("--max-cost", type=float, default=None)
    gate.add_argument("--comparison-review", default=None)
    gate.add_argument("--review-template-out", default=str(COMPARISON_REVIEW_PATH))
    gate.add_argument("--enforce", action="store_true", help=argparse.SUPPRESS)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "score":
        raise SystemExit(cmd_score(args))
    if args.command == "report":
        raise SystemExit(cmd_report(args))
    if args.command == "build-review-packet":
        raise SystemExit(cmd_build_review(args))
    if args.command == "resume-labels":
        raise SystemExit(cmd_resume_labels(args))
    if args.command == "finalize-human-labels":
        raise SystemExit(cmd_finalize_labels(args))
    if args.command == "gate":
        raise SystemExit(cmd_gate(args))
    raise AssertionError(args.command)


if __name__ == "__main__":
    main()
