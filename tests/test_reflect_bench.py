"""Unit tests for the Reflect bench CLI (score/report/gate) and cost_report."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
import subprocess
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from test_score_funnel import FunnelFixture  # noqa: E402
from test_recruiting_pipeline import (  # noqa: E402
    FakeRunner,
    critic_adapter,
    good_judge,
    plan_adapter,
    recruiting_spec,
)
from packs.search.pipeline.artifacts import persist_result
from packs.search.pipeline.recruiting import run_recruiting
from packs.search.reflect.snapshots import canonical_hash as recruiting_hash


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


bench = _load("bench", ROOT / "packs" / "search" / "reflect" / "bench.py")
cr = _load("cost_report", ROOT / "packs" / "search" / "reflect" / "cost_report.py")


class BenchSandbox:
    """Redirect bench's fixed state paths into a tmp dir so tests never touch repo state."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.powerpacks = self.tmp / ".powerpacks"
        self.search_runs = self.powerpacks / "search-runs"
        self.reflect = self.powerpacks / "reflect"
        self.results = self.reflect / "results"
        self.report = self.reflect / "report.json"
        self.suite = self.reflect / "suite"
        self.search_runs.mkdir(parents=True)
        self.reflect.mkdir(parents=True)

    def patches(self):
        return [mock.patch.object(bench, "RESULTS_DIR", self.results),
                mock.patch.object(bench, "REPORT_PATH", self.report),
                mock.patch.object(bench, "SUITE_DIR", self.suite),
                mock.patch.object(bench, "REFLECT_STATE", self.reflect),
                mock.patch.object(bench, "GT_DIR", self.reflect / "gt"),
                mock.patch.object(bench, "POWERPACKS_STATE", self.powerpacks)]


def _score_args(fx: FunnelFixture) -> argparse.Namespace:
    local = bench.GT_DIR / fx.dir.name
    local.mkdir(parents=True, exist_ok=True)
    case = local / "case.json"
    base_spec = recruiting_spec()
    plan = {"schema_version": "synthetic.review-plan.v1", "role": "Synthetic systems role"}
    person_ids = [f"p{i}" for i in range(1, 12)] + ["p20"]
    run_spec = replace(
        base_spec,
        bounds=replace(base_spec.bounds, frontier_limit=2),
        corpus=replace(
            base_spec.corpus,
            content_hash="5" * 64,
            schema_hash=bench.canonical_hash({"people": "4" * 64}),
            membership_hash="3" * 64,
        ),
        recruiting=replace(
            base_spec.recruiting,
            reviewed_plan_hash=bench.canonical_hash(plan),
            review_pool_person_ids=tuple(person_ids),
        ),
    )
    spec = run_spec.to_dict()
    evidence = {pid: bench.canonical_hash({"synthetic": pid}) for pid in person_ids}
    snapshot_doc = {
        "schema_version": "reflect.corpus_snapshot.v2", "backend": "local",
        "source": "local_deterministic_snapshot", "verification_status": "verified_comparable",
        "set_id": "synthetic-set", "operator_scope_hash": "2" * 64, "membership_hash": "3" * 64,
        "namespace_schema_hashes": {"people": "4" * 64}, "scoped_records_hash": "5" * 64,
        "evidence_hashes": evidence,
    }
    run_corpus = dict(snapshot_doc)
    stable_run_corpus = {key: value for key, value in run_corpus.items() if key != "observed_at"}
    review_dir = fx.dir / "review"
    review_dir.mkdir(exist_ok=True)
    (fx.dir / "search_spec.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    (review_dir / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    (review_dir / "corpus.json").write_text(json.dumps(run_corpus, indent=2, sort_keys=True) + "\n")
    (review_dir / "source.json").write_text(json.dumps({
        "normalized_jd": run_spec.recruiting.source,
    }, indent=2, sort_keys=True) + "\n")
    review_evidence = bench.ReviewEvidenceSnapshot.from_hashes(evidence)
    (review_dir / "evidence.json").write_text(
        json.dumps(review_evidence.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    (review_dir / "binding.json").write_text(json.dumps({
        "schema_version": "recruiting.review-binding.v1",
        "plan_sha256": run_spec.recruiting.reviewed_plan_hash,
        "source_sha256": bench.canonical_hash(run_spec.recruiting.source),
        "jd_sha256": bench.canonical_hash(run_spec.recruiting.source),
        "corpus_sha256": bench.canonical_hash(stable_run_corpus),
        "corpus": stable_run_corpus,
        "review_pool_person_ids": person_ids,
        "review_pool_person_ids_sha256": bench.canonical_hash(person_ids),
    }, indent=2, sort_keys=True) + "\n")
    case.write_text(json.dumps({
        "schema_version": "reflect.case.v1", "case_id": fx.dir.name,
        "public_source": {
            "reference": "https://example.invalid/synthetic-role",
            "content_hash": bench.canonical_hash(run_spec.recruiting.source),
        },
        "reviewed_search_spec": {"content": spec, "content_hash": bench.canonical_hash(spec)},
    }, indent=2) + "\n")
    snapshot = local / "snapshot.json"
    snapshot.write_text(json.dumps(snapshot_doc) + "\n")
    labels = [{"person_id": f"p{i}", "evidence_hash": evidence[f"p{i}"], "decision": "eligible_bench",
               "reason_codes": ["synthetic_fit"], "notes": "", "reviewer": "Synthetic Reviewer",
               "reviewed_at": "2026-07-31T00:00:00Z"} for i in range(1, 12)]
    labels.append({"person_id": "p20", "evidence_hash": evidence["p20"], "decision": "ineligible",
                   "reason_codes": ["synthetic_out"], "notes": "", "reviewer": "Synthetic Reviewer",
                   "reviewed_at": "2026-07-31T00:00:00Z"})
    gt_doc = {
        "schema_version": "reflect.ground_truth.v1", "case_id": fx.dir.name,
        "case_hash": bench._file_hash(case), "corpus_snapshot_hash": bench.snapshot_identity(snapshot_doc),
        "review_pool_evidence_hash": bench.canonical_hash(evidence), "review_pool_evidence_hashes": evidence,
        "labels": labels, "finalized_at": "2026-07-31T00:00:00Z",
    }
    gt = local / "ground-truth.json"
    gt.write_text(json.dumps(gt_doc) + "\n")
    hard_filter = fx.dir / "hard-filter-validation.json"
    membership = json.loads((fx.dir / "stage-membership.json").read_text())
    violations = [
        {"person_id": row["person_id"], "reason_code": "synthetic_filter"}
        for row in membership["candidates"] if row["disposition"] == "hard_filter_quarantined"
    ]
    hard_filter.write_text(json.dumps({
        "schema_version": "reflect.hard_filter_validation.v1", "case_id": "production",
        "case_hash": bench.canonical_hash(spec), "corpus_snapshot_hash": bench.canonical_hash(stable_run_corpus),
        "reviewed_count": membership["total_sourced"], "violation_count": len(violations), "violations": violations,
        "producer": "typed_runner", "generated_at": "2026-07-31T00:00:00Z",
    }) + "\n")
    manifest_artifacts = {
        "search_spec_json": "search_spec.json",
        "review_plan_json": "review/plan.json",
        "review_binding_json": "review/binding.json",
        "review_corpus_json": "review/corpus.json",
        "review_source_json": "review/source.json",
        "review_evidence_json": "review/evidence.json",
        "stage-membership.json": "stage-membership.json",
        "candidate-frontier.json": "candidate-frontier.json",
        "hard_filter_validation_json": "hard-filter-validation.json",
    }
    (fx.dir / "manifest.json").write_text(json.dumps({
        "schema_version": "search.manifest.v1",
        "artifacts": {
            key: {"path": relative, "sha256": bench._file_hash(fx.dir / relative)}
            for key, relative in manifest_artifacts.items()
        },
    }, indent=2) + "\n")
    meta = bench.SUITE_DIR / fx.dir.name
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "meta.json").write_text(json.dumps({"case_id": fx.dir.name, "job_family": "synthetic"}) + "\n")
    return argparse.Namespace(run_dir=str(fx.dir), gt=str(gt), case=str(case), slug=None, ks="10,25",
                              score_threshold=0.40, usage_log=None, snapshot=str(snapshot),
                              hard_filter_validation=str(hard_filter), out=None)


def _refresh_manifest_hash(run_dir: Path, key: str) -> None:
    path = run_dir / "manifest.json"
    document = json.loads(path.read_text())
    artifact = run_dir / document["artifacts"][key]["path"]
    document["artifacts"][key]["sha256"] = bench._file_hash(artifact)
    path.write_text(json.dumps(document, indent=2) + "\n")


class TestBenchScoreAndReport(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = BenchSandbox()
        self.fx = FunnelFixture(self.sandbox.search_runs)
        self.patchers = self.sandbox.patches()
        for p in self.patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])
        self.addCleanup(lambda: shutil.rmtree(self.sandbox.tmp, ignore_errors=True))

    def test_score_writes_result_json(self) -> None:
        rc = bench.cmd_score(_score_args(self.fx))
        self.assertEqual(rc, 0)
        result = json.loads((self.sandbox.results / self.fx.dir.name / "result.json").read_text())
        self.assertEqual(result["funnel"]["gt_size"], 11)
        self.assertEqual(result["funnel"]["dispositions"]["shortlisted"], 1)
        self.assertTrue(result["funnel"]["funnel"])
        self.assertTrue(result["funnel"]["probe_attribution"])
        self.assertIsNotNone(result["gaps"])
        self.assertIn("ndcg@10", result["gaps"])
        self.assertEqual(result["gaps"]["overall_recall"], 0.7273)
        self.assertGreater(result["gaps"]["overall_recall"], 2 / 11)  # includes ranked bench candidates
        self.assertIsNone(result["cost"])  # no usage.jsonl in the fixture
        self.assertFalse((self.fx.dir / "convergence.csv").exists())

    def test_typed_recruiting_run_scores_complete_frontier_strictly(self) -> None:
        run = self.sandbox.search_runs / f"reflect-typed-{uuid.uuid4().hex}"
        run.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(run, ignore_errors=True))
        runner = FakeRunner(4)
        person_ids = [f"p{i}" for i in range(4)]
        base_spec = recruiting_spec()
        spec = replace(base_spec, recruiting=replace(
            base_spec.recruiting, review_pool_person_ids=tuple(person_ids)
        ))
        evidence = {person_id: bench.canonical_hash({"person_id": person_id}) for person_id in person_ids}
        run_snapshot = runner.snapshot_corpus("local", tuple(person_ids))
        prepared = run_recruiting(
            spec,
            runner,
            artifact_root=run,
            allowed_artifact_root=self.sandbox.search_runs,
            plan_adapter=plan_adapter,
            critic_adapter=critic_adapter,
            corpus_snapshot=run_snapshot,
        )
        self.assertEqual(prepared.status, "awaiting_review")
        plan = json.loads((run / "review/plan.json").read_text())
        approved = replace(
            spec,
            recruiting=replace(spec.recruiting, reviewed_plan_hash=recruiting_hash(plan)),
        )
        completed = run_recruiting(
            approved, runner, artifact_root=run,
            allowed_artifact_root=self.sandbox.search_runs, judge_adapter=good_judge,
            corpus_snapshot=run_snapshot,
        )
        self.assertIn(completed.status, {"completed_no_anchors", "completed_capped"})
        self.assertTrue((run / "stage-membership.json").exists())
        self.assertTrue((run / "candidate-frontier.json").exists())
        persist_result(run, approved, completed, allowed_root=self.sandbox.powerpacks)
        run_binding = json.loads((run / "review/binding.json").read_text())
        self.assertEqual(run_binding["review_pool_person_ids"], person_ids)
        self.assertEqual(
            set(json.loads((run / "review/corpus.json").read_text())["evidence_hashes"]),
            set(person_ids),
        )
        self.assertEqual(
            set(json.loads((run / "review/evidence.json").read_text())["evidence_hashes"]),
            set(person_ids),
        )
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertTrue({"search_spec_json", "review_binding_json", "review_corpus_json", "review_evidence_json"}.issubset(
            manifest["artifacts"]
        ))

        slug = run.name
        local = self.sandbox.reflect / "gt" / slug
        local.mkdir(parents=True)
        reviewed_spec = approved.to_dict()
        case = local / "case.json"
        case.write_text(json.dumps({
            "schema_version": "reflect.case.v1",
            "case_id": slug,
            "public_source": {
                "reference": "https://example.invalid/synthetic-role",
                "content_hash": run_binding["jd_sha256"],
            },
            "reviewed_search_spec": {"content": reviewed_spec, "content_hash": bench.canonical_hash(reviewed_spec)},
        }, indent=2) + "\n")
        snapshot_doc = json.loads((run / "review/corpus.json").read_text())
        snapshot = local / "snapshot.json"
        snapshot.write_text(json.dumps(snapshot_doc) + "\n")
        decisions = ("eligible_strong", "eligible_bench", "ineligible", "ineligible")
        labels = [
            {
                "person_id": person_id,
                "evidence_hash": evidence[person_id],
                "decision": decision,
                "reason_codes": ["synthetic_fit"],
                "notes": "",
                "reviewer": "Synthetic Reviewer",
                "reviewed_at": "2026-07-31T00:00:00Z",
            }
            for person_id, decision in zip(person_ids, decisions, strict=True)
        ]
        gt = local / "ground-truth.json"
        gt.write_text(json.dumps({
            "schema_version": "reflect.ground_truth.v1",
            "case_id": slug,
            "case_hash": bench._file_hash(case),
            "corpus_snapshot_hash": bench.snapshot_identity(snapshot_doc),
            "review_pool_evidence_hash": bench.canonical_hash(evidence),
            "review_pool_evidence_hashes": evidence,
            "labels": labels,
            "finalized_at": "2026-07-31T00:00:00Z",
        }) + "\n")
        validation = run / "hard-filter-validation.json"
        meta = self.sandbox.suite / slug
        meta.mkdir(parents=True)
        (meta / "meta.json").write_text(json.dumps({"case_id": slug, "job_family": "synthetic"}) + "\n")
        args = argparse.Namespace(
            run_dir=str(run),
            gt=str(gt),
            case=str(case),
            slug=slug,
            ks="10,25",
            usage_log=None,
            snapshot=str(snapshot),
            hard_filter_validation=str(validation),
            out=None,
        )
        bench.cmd_score(args)
        production_validation = local / "production-hard-filter-validation.json"
        production_validation.write_bytes(validation.read_bytes())
        with self.assertRaisesRegex(ValueError, "run-produced hard-filter artifact"):
            bench.cmd_score(argparse.Namespace(
                **{**vars(args), "hard_filter_validation": str(production_validation)}
            ))
        bench.cmd_report(argparse.Namespace(results_dir=None, out=None))
        self.assertEqual(bench.cmd_gate(argparse.Namespace(
            baseline=str(self.sandbox.report),
            current=str(self.sandbox.report),
            min_recall=None,
            max_cost=None,
            comparison_review=None,
            review_template_out=str(self.sandbox.reflect / "comparison-review.json"),
            enforce=True,
        )), 0)
        result = json.loads((self.sandbox.results / slug / "result.json").read_text())
        self.assertEqual(result["source_recall"], 1.0)
        self.assertEqual(result["gaps"]["overall_recall"], 1.0)
        self.assertEqual(result["gaps"]["recall@10"], 1.0)
        self.assertGreater(result["gaps"]["ndcg@10"], 0)
        self.assertFalse((run / "convergence.csv").exists())

    def test_completed_empty_run_scores_zero_recall_and_never_sourced(self) -> None:
        run = self.sandbox.search_runs / f"reflect-empty-{uuid.uuid4().hex}"
        run.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(run, ignore_errors=True))
        runner = FakeRunner(0)
        person_id = "synthetic-gt-miss"
        base_spec = recruiting_spec()
        spec = replace(base_spec, recruiting=replace(
            base_spec.recruiting, review_pool_person_ids=(person_id,)
        ))
        evidence = {person_id: bench.canonical_hash({"person_id": person_id})}
        snapshot_doc = runner.snapshot_corpus("local", (person_id,))
        prepared = run_recruiting(
            spec, runner, artifact_root=run,
            allowed_artifact_root=self.sandbox.search_runs, plan_adapter=plan_adapter,
            critic_adapter=critic_adapter, corpus_snapshot=snapshot_doc,
        )
        plan = json.loads((run / "review/plan.json").read_text())
        approved = replace(
            spec, recruiting=replace(spec.recruiting, reviewed_plan_hash=recruiting_hash(plan)),
        )
        completed = run_recruiting(
            approved, runner, artifact_root=run,
            allowed_artifact_root=self.sandbox.search_runs, judge_adapter=good_judge,
            corpus_snapshot=snapshot_doc,
        )
        self.assertEqual(completed.status, "completed_empty")
        persist_result(run, approved, completed, allowed_root=self.sandbox.powerpacks)

        slug = run.name
        local = self.sandbox.reflect / "gt" / slug
        local.mkdir(parents=True)
        binding = json.loads((run / "review/binding.json").read_text())
        case = local / "case.json"
        case.write_text(json.dumps({
            "schema_version": "reflect.case.v1", "case_id": slug,
            "public_source": {"reference": "https://example.invalid/empty-role",
                              "content_hash": binding["jd_sha256"]},
            "reviewed_search_spec": {"content": approved.to_dict(),
                                     "content_hash": bench.canonical_hash(approved.to_dict())},
        }, indent=2) + "\n")
        snapshot = local / "snapshot.json"
        snapshot.write_text(json.dumps(snapshot_doc) + "\n")
        gt = local / "ground-truth.json"
        gt.write_text(json.dumps({
            "schema_version": "reflect.ground_truth.v1", "case_id": slug,
            "case_hash": bench._file_hash(case),
            "corpus_snapshot_hash": bench.snapshot_identity(snapshot_doc),
            "review_pool_evidence_hash": bench.canonical_hash(evidence),
            "review_pool_evidence_hashes": evidence,
            "labels": [{"person_id": person_id, "evidence_hash": evidence[person_id],
                        "decision": "eligible_strong", "reason_codes": ["synthetic_fit"],
                        "notes": "", "reviewer": "Synthetic Reviewer",
                        "reviewed_at": "2026-07-31T00:00:00Z"}],
            "finalized_at": "2026-07-31T00:00:00Z",
        }) + "\n")
        meta = self.sandbox.suite / slug
        meta.mkdir(parents=True)
        (meta / "meta.json").write_text(json.dumps({"case_id": slug, "job_family": "synthetic"}) + "\n")
        args = argparse.Namespace(
            run_dir=str(run), gt=str(gt), case=str(case), slug=slug, ks="10,25",
            usage_log=None, snapshot=str(snapshot),
            hard_filter_validation=str(run / "hard-filter-validation.json"), out=None,
        )
        bench.cmd_score(args)
        result = json.loads((self.sandbox.results / slug / "result.json").read_text())
        self.assertEqual(result["gaps"]["overall_recall"], 0.0)
        self.assertEqual(result["funnel"]["dispositions"], {"never_sourced": 1})
        self.assertEqual(result["source_recall"], 0.0)

    def test_missing_stage_membership_fails_instead_of_scoring_zero(self) -> None:
        args = _score_args(self.fx)
        (self.fx.dir / "stage-membership.json").unlink()
        with self.assertRaisesRegex(ValueError, "hash does not match artifact: stage-membership.json"):
            bench.cmd_score(args)

    def test_malformed_stage_membership_fails_instead_of_scoring_zero(self) -> None:
        args = _score_args(self.fx)
        path = self.fx.dir / "stage-membership.json"
        document = json.loads(path.read_text())
        document["unexpected"] = True
        path.write_text(json.dumps(document) + "\n")
        _refresh_manifest_hash(self.fx.dir, "stage-membership.json")
        with self.assertRaisesRegex(ValueError, "Additional properties.*unexpected"):
            bench.cmd_score(args)

    def test_malformed_candidate_frontier_fails_instead_of_coercing_values(self) -> None:
        args = _score_args(self.fx)
        path = self.fx.dir / "candidate-frontier.json"
        document = json.loads(path.read_text())
        document["candidates"][0]["person_id"] = 123
        document["candidates"][0]["retrieval_score"] = True
        path.write_text(json.dumps(document) + "\n")
        _refresh_manifest_hash(self.fx.dir, "candidate-frontier.json")
        with self.assertRaisesRegex(ValueError, "person_id.*not of type 'string'|retrieval_score.*not of type 'number'"):
            bench.cmd_score(args)

    def test_strict_score_rejects_case_for_a_different_run_spec(self) -> None:
        args = _score_args(self.fx)
        case = Path(args.case)
        document = json.loads(case.read_text())
        document["reviewed_search_spec"]["content"]["raw_request"] = "different run"
        document["reviewed_search_spec"]["content_hash"] = bench.canonical_hash(
            document["reviewed_search_spec"]["content"]
        )
        case.write_text(json.dumps(document, indent=2) + "\n")
        with self.assertRaisesRegex(ValueError, "does not match the persisted run SearchSpec"):
            bench.cmd_score(args)

    def test_strict_score_rejects_changed_jd_content_at_same_url(self) -> None:
        args = _score_args(self.fx)
        case = Path(args.case)
        document = json.loads(case.read_text())
        reference = document["public_source"]["reference"]
        document["public_source"]["content_hash"] = "0" * 64
        case.write_text(json.dumps(document, indent=2) + "\n")
        self.assertEqual(json.loads(case.read_text())["public_source"]["reference"], reference)
        with self.assertRaisesRegex(ValueError, "normalized run JD"):
            bench.cmd_score(args)

    def test_strict_score_rejects_candidate_only_run_evidence(self) -> None:
        args = _score_args(self.fx)
        evidence_path = self.fx.dir / "review/evidence.json"
        document = json.loads(evidence_path.read_text())
        person_id = next(iter(document["evidence_hashes"]))
        document = bench.ReviewEvidenceSnapshot.from_hashes({
            person_id: document["evidence_hashes"][person_id]
        }).to_dict()
        evidence_path.write_text(json.dumps(document, indent=2) + "\n")
        _refresh_manifest_hash(self.fx.dir, "review_evidence_json")
        with self.assertRaisesRegex(ValueError, "review evidence.*review corpus"):
            bench.cmd_score(args)

    def test_strict_score_rejects_different_supplied_review_pool_evidence(self) -> None:
        args = _score_args(self.fx)
        snapshot_path = Path(args.snapshot)
        snapshot = json.loads(snapshot_path.read_text())
        person_id = next(iter(snapshot["evidence_hashes"]))
        snapshot["evidence_hashes"][person_id] = "f" * 64
        snapshot_path.write_text(json.dumps(snapshot) + "\n")
        gt_path = Path(args.gt)
        gt = json.loads(gt_path.read_text())
        gt["review_pool_evidence_hashes"] = snapshot["evidence_hashes"]
        gt["review_pool_evidence_hash"] = bench.canonical_hash(snapshot["evidence_hashes"])
        gt["corpus_snapshot_hash"] = bench.snapshot_identity(snapshot)
        for label in gt["labels"]:
            label["evidence_hash"] = snapshot["evidence_hashes"][label["person_id"]]
        gt_path.write_text(json.dumps(gt) + "\n")
        with self.assertRaisesRegex(ValueError, "review-pool evidence snapshot"):
            bench.cmd_score(args)

    def test_strict_score_binds_membership_bounds_to_search_spec(self) -> None:
        args = _score_args(self.fx)
        membership = self.fx.dir / "stage-membership.json"
        document = json.loads(membership.read_text())
        document["score_floor"] = 0.41
        membership.write_text(json.dumps(document) + "\n")
        _refresh_manifest_hash(self.fx.dir, "stage-membership.json")
        with self.assertRaisesRegex(ValueError, "scoring bounds"):
            bench.cmd_score(args)

    def test_strict_score_rejects_manifest_hash_and_review_binding_drift(self) -> None:
        args = _score_args(self.fx)
        frontier = self.fx.dir / "candidate-frontier.json"
        frontier.write_text(frontier.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "manifest hash does not match artifact"):
            bench.cmd_score(args)

        args = _score_args(self.fx)
        binding = self.fx.dir / "review/binding.json"
        document = json.loads(binding.read_text())
        document["corpus_sha256"] = "0" * 64
        binding.write_text(json.dumps(document, indent=2) + "\n")
        _refresh_manifest_hash(self.fx.dir, "review_binding_json")
        with self.assertRaisesRegex(ValueError, "review/corpus binding"):
            bench.cmd_score(args)

    def test_report_aggregates_and_lists_pending(self) -> None:
        bench.cmd_score(_score_args(self.fx))
        pending_dir = self.sandbox.suite / "some-unscored-jd"
        pending_dir.mkdir(parents=True)
        (pending_dir / "meta.json").write_text(json.dumps({"job_family": "eng-data", "gt": "pending"}))
        rc = bench.cmd_report(argparse.Namespace())
        self.assertEqual(rc, 0)
        report = json.loads(self.sandbox.report.read_text())
        self.assertEqual(len(report["jds"]), 1)
        self.assertEqual(report["gt_pending"], [{"slug": "some-unscored-jd", "job_family": "eng-data", "gt": "pending"}])
        fam = report["by_job_family"]["synthetic"]
        self.assertEqual(fam["jds"], 1)
        self.assertIsNotNone(fam["mean_recall"])
        self.assertTrue(report["jds"][0]["funnel"])
        self.assertTrue(report["jds"][0]["probe_attribution"])

    def test_committed_suite_metadata_contains_no_candidate_identity_or_contact_fields(self) -> None:
        suite = ROOT / "packs/search/reflect/suite"
        forbidden = ('"person_id"', '"email"', 'linkedin.com/in/', '"phone"')
        for path in suite.glob("*/meta.json"):
            text = path.read_text().lower()
            self.assertFalse(any(token in text for token in forbidden), path)

    def test_public_synthetic_gtm_registry_entries_exist(self) -> None:
        suite = ROOT / "packs/search/reflect/suite"
        for slug in ("synthetic-gtm-senior-ic", "synthetic-gtm-executive-leadership"):
            meta = json.loads((suite / slug / "meta.json").read_text())
            self.assertEqual(meta["case_id"], slug)
            self.assertIn("synthetic", meta["source"].lower())

    def test_hard_filter_validation_binding_and_counts(self) -> None:
        valid = {"case_id": "case", "case_hash": "a" * 64, "corpus_snapshot_hash": "b" * 64,
                 "reviewed_count": 2, "violation_count": 0, "violations": [],
                 "generated_at": "2026-07-31T00:00:00Z"}
        self.assertEqual(bench._validate_hard_filter(valid, case_id="case", case_hash="a" * 64,
                                                     corpus_hash="b" * 64, reviewed_count=2), 0)
        for field, value in (("case_hash", "c" * 64), ("corpus_snapshot_hash", "d" * 64),
                             ("reviewed_count", 3), ("violation_count", 1)):
            forged = dict(valid)
            forged[field] = value
            with self.assertRaises(ValueError, msg=field):
                bench._validate_hard_filter(forged, case_id="case", case_hash="a" * 64,
                                            corpus_hash="b" * 64, reviewed_count=2)

    def test_strict_score_report_self_gate_passes_with_validated_zero_violations(self) -> None:
        bench.cmd_score(_score_args(self.fx))
        bench.cmd_report(argparse.Namespace(results_dir=None, out=None))
        args = argparse.Namespace(baseline=str(self.sandbox.report), current=str(self.sandbox.report),
                                  min_recall=None, max_cost=None, comparison_review=None,
                                  review_template_out=str(self.sandbox.reflect / "review.json"), enforce=True)
        self.assertEqual(bench.cmd_gate(args), 0)

    def test_reflect_schema_discovery_includes_final_contracts(self) -> None:
        validator = ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py"
        result = subprocess.run([sys.executable, str(validator), "--list-schemas"], cwd=ROOT,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        schemas = set(result.stdout.splitlines())
        self.assertTrue({"reflect-case", "reflect-corpus-snapshot", "reflect-review-packet",
                         "reflect-human-labels", "reflect-ground-truth", "reflect-comparison-review",
                         "reflect-hard-filter-validation"}.issubset(schemas))


class TestBenchGate(unittest.TestCase):
    def _report(self, recall: float = 0.8, ndcg10: float = 0.8, ndcg25: float = 0.8,
                *, identity: str = "a" * 64) -> Path:
        d = Path(tempfile.mkdtemp())
        path = d / "report.json"
        path.write_text(json.dumps({"jds": [{
            "slug": "jd-1", "overall_recall": recall, "recall@10": recall, "recall@25": recall,
            "ndcg@10": ndcg10, "ndcg@25": ndcg25, "cost_usd": 1.0,
            "corpus_hash": identity, "case_hash": "b" * 64, "evidence_hash": "c" * 64,
            "label_hash": "d" * 64, "source_recall": 0.8, "hydration_coverage": 0.8,
            "hard_filter_survival": 0.8, "triage_survival": 0.8,
            "hard_filter_violations": 0, "unreviewed_candidate_count": 0,
        }]}) + "\n")
        return path

    def _gate_args(self, baseline: Path, current: Path, **kw) -> argparse.Namespace:
        return argparse.Namespace(baseline=str(baseline), current=str(current),
                                  min_recall=kw.get("min_recall"), max_cost=kw.get("max_cost"), enforce=True,
                                  comparison_review=kw.get("comparison_review"),
                                  review_template_out=str(Path(tempfile.mkdtemp()) / "comparison-review.json"))

    def test_identical_reports_pass(self) -> None:
        base = self._report(0.8)
        self.assertEqual(bench.cmd_gate(self._gate_args(base, base)), 0)

    def test_strict_recall_regression_fails(self) -> None:
        rc = bench.cmd_gate(self._gate_args(self._report(0.8), self._report(0.5)))
        self.assertEqual(rc, 1)

    def test_ndcg_drop_greater_than_point_02_fails(self) -> None:
        self.assertEqual(bench.cmd_gate(self._gate_args(self._report(ndcg10=.8), self._report(ndcg10=.779))), 1)

    def test_ndcg_exact_and_smaller_drops_need_review(self) -> None:
        for candidate in (.78, .79):
            args = self._gate_args(self._report(ndcg10=.8), self._report(ndcg10=candidate))
            self.assertEqual(bench.cmd_gate(args), 1)
            template = json.loads(Path(args.review_template_out).read_text())
            self.assertEqual(template["regressions"][0]["candidate_score"], candidate)

    def test_matching_accepted_review_passes_and_rejected_or_stale_fails(self) -> None:
        baseline, current = self._report(ndcg10=.8), self._report(ndcg10=.79)
        args = self._gate_args(baseline, current)
        bench.cmd_gate(args)
        review_path = Path(args.review_template_out)
        review = json.loads(review_path.read_text())
        review.update({"decision": "accepted", "explanation": "Ordering tradeoff accepted.",
                       "reviewer": "Synthetic Reviewer", "reviewed_at": "2026-07-31T00:00:00Z"})
        review_path.write_text(json.dumps(review) + "\n")
        self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current, comparison_review=str(review_path))), 0)
        review["decision"] = "rejected"
        review_path.write_text(json.dumps(review) + "\n")
        self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current, comparison_review=str(review_path))), 1)
        review["decision"] = "accepted"
        review["candidate_report_hash"] = "0" * 64
        review_path.write_text(json.dumps(review) + "\n")
        self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current, comparison_review=str(review_path))), 1)

    def test_wrong_regression_review_fails(self) -> None:
        baseline, current = self._report(ndcg10=.8), self._report(ndcg10=.79)
        args = self._gate_args(baseline, current)
        bench.cmd_gate(args)
        path = Path(args.review_template_out)
        review = json.loads(path.read_text())
        review.update({"decision": "accepted", "explanation": "Reviewed.", "reviewer": "Synthetic Reviewer",
                       "reviewed_at": "2026-07-31T00:00:00Z"})
        review["regressions"][0]["k"] = 25
        path.write_text(json.dumps(review) + "\n")
        self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current, comparison_review=str(path))), 1)

    def test_changed_or_missing_identity_is_non_comparable(self) -> None:
        self.assertEqual(bench.cmd_gate(self._gate_args(self._report(), self._report(identity="e" * 64))), 1)
        baseline, current = self._report(), self._report()
        doc = json.loads(current.read_text())
        doc["jds"][0]["case_hash"] = None
        current.write_text(json.dumps(doc) + "\n")
        with self.assertRaises(ValueError):
            bench.cmd_gate(self._gate_args(baseline, current))

    def test_floors(self) -> None:
        base = self._report(0.8)
        rc = bench.cmd_gate(self._gate_args(base, base, min_recall=0.9))
        self.assertEqual(rc, 1)
        rc = bench.cmd_gate(self._gate_args(base, base, max_cost=0.5))
        self.assertEqual(rc, 1)

    def test_empty_report_is_rejected(self) -> None:
        base = self._report(0.8)
        empty = Path(tempfile.mkdtemp()) / "report.json"
        empty.write_text(json.dumps({"jds": []}))
        with self.assertRaises(ValueError):
            bench.cmd_gate(self._gate_args(base, empty))

    def test_each_stage_regression_fails_independently(self) -> None:
        for metric in bench.STAGE_METRICS:
            baseline, current = self._report(), self._report()
            doc = json.loads(current.read_text())
            doc["jds"][0][metric] = 0.7
            current.write_text(json.dumps(doc) + "\n")
            self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current)), 1, metric)

    def test_hard_filter_violation_and_missing_evidence_fail(self) -> None:
        for value in (1, None):
            baseline, current = self._report(), self._report()
            doc = json.loads(current.read_text())
            doc["jds"][0]["hard_filter_violations"] = value
            current.write_text(json.dumps(doc) + "\n")
            if value is None:
                with self.assertRaises(ValueError):
                    bench.cmd_gate(self._gate_args(baseline, current))
            else:
                self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current)), 1)

    def test_unreviewed_candidate_is_non_comparable(self) -> None:
        baseline, current = self._report(), self._report()
        doc = json.loads(current.read_text())
        doc["jds"][0]["unreviewed_candidate_count"] = 1
        current.write_text(json.dumps(doc) + "\n")
        self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current)), 1)

    def test_duplicate_report_slugs_are_rejected(self) -> None:
        baseline, current = self._report(), self._report()
        doc = json.loads(current.read_text())
        doc["jds"].append(dict(doc["jds"][0]))
        current.write_text(json.dumps(doc) + "\n")
        with self.assertRaises(ValueError):
            bench.cmd_gate(self._gate_args(baseline, current))

    def test_report_row_type_range_and_finite_bypasses_are_rejected(self) -> None:
        mutations = [
            ("case_hash", "A" * 64), ("source_recall", 1.1), ("recall@10", float("nan")),
            ("ndcg@25", float("inf")), ("triage_survival", "0.8"),
            ("overall_recall", -0.1),
            ("hard_filter_violations", -1), ("unreviewed_candidate_count", 1.5),
        ]
        for field, value in mutations:
            baseline, current = self._report(), self._report()
            document = json.loads(current.read_text())
            document["jds"][0][field] = value
            current.write_text(json.dumps(document) + "\n")
            with self.assertRaises(ValueError, msg=field):
                bench.cmd_gate(self._gate_args(baseline, current))

    def test_comparison_review_requires_stripped_nonempty_text(self) -> None:
        baseline, current = self._report(ndcg10=.8), self._report(ndcg10=.79)
        args = self._gate_args(baseline, current)
        bench.cmd_gate(args)
        review = json.loads(Path(args.review_template_out).read_text())
        review.update({"decision": "accepted", "explanation": "   ", "reviewer": "   ",
                       "reviewed_at": "2026-07-31T00:00:00Z"})
        path = Path(args.review_template_out)
        path.write_text(json.dumps(review) + "\n")
        self.assertEqual(bench.cmd_gate(self._gate_args(baseline, current, comparison_review=str(path))), 1)


class TestBenchCliSubprocess(unittest.TestCase):
    def test_module_help_and_lifecycle_help(self) -> None:
        for args in (["--help"], ["build-review-packet", "--help"], ["resume-labels", "--help"],
                     ["finalize-human-labels", "--help"]):
            result = subprocess.run([sys.executable, "-m", "packs.search.reflect.bench", *args], cwd=ROOT, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifecycle_commands_execute_through_module_cli(self) -> None:
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        powerpacks = sandbox / ".powerpacks"
        reflect = powerpacks / "reflect"
        root = reflect / f"test-synthetic-lifecycle-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        patchers = [
            mock.patch.object(bench, "POWERPACKS_STATE", powerpacks),
            mock.patch.object(bench, "REFLECT_STATE", reflect),
            mock.patch.object(bench, "GT_DIR", reflect / "gt"),
            mock.patch.object(bench, "RESULTS_DIR", reflect / "results"),
            mock.patch.object(bench, "REPORT_PATH", reflect / "report.json"),
            mock.patch.object(bench, "COMPARISON_REVIEW_PATH", reflect / "comparison-review.json"),
        ]
        for patcher in patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        def run_cli(args: list[str]) -> int:
            with mock.patch.object(sys, "argv", ["bench", *args]), mock.patch("builtins.print"):
                with self.assertRaises(SystemExit) as stopped:
                    bench.main()
            return int(stopped.exception.code or 0)

        base_spec = recruiting_spec()
        plan = {"schema_version": "synthetic.review-plan.v1", "role": "Synthetic systems role"}
        typed_spec = replace(
            base_spec,
            bounds=replace(base_spec.bounds, frontier_limit=2),
            corpus=replace(base_spec.corpus, content_hash="5" * 64,
                           schema_hash=bench.canonical_hash({"people": "4" * 64}),
                           membership_hash="3" * 64),
            recruiting=replace(
                base_spec.recruiting,
                reviewed_plan_hash=bench.canonical_hash(plan),
                review_pool_person_ids=("synthetic-person",),
            ),
        )
        spec = typed_spec.to_dict()
        case = root / "case.json"
        case.write_text(json.dumps({"schema_version": "reflect.case.v1", "case_id": "synthetic-gtm-senior-ic",
            "public_source": {"reference": "https://example.invalid/role",
                              "content_hash": bench.canonical_hash(typed_spec.recruiting.source)},
            "reviewed_search_spec": {"content": spec, "content_hash": bench.canonical_hash(spec)}}) + "\n")
        evidence = candidate_evidence()
        evidence_hash = bench.canonical_hash(evidence)
        snapshot_doc = {"schema_version": "reflect.corpus_snapshot.v2", "backend": "local",
            "source": "local_deterministic_snapshot", "verification_status": "verified_comparable",
            "set_id": "synthetic-set", "operator_scope_hash": "2" * 64, "membership_hash": "3" * 64,
            "namespace_schema_hashes": {"people": "4" * 64}, "scoped_records_hash": "5" * 64,
            "evidence_hashes": {"synthetic-person": evidence_hash}}
        snapshot = root / "snapshot.json"
        snapshot.write_text(json.dumps(snapshot_doc) + "\n")
        candidates = root / "candidates.json"
        candidates.write_text(json.dumps([{"person_id": "synthetic-person", "evidence": evidence}]) + "\n")
        packet, labels, gt = root / "packet.json", root / "labels.json", root / "gt.json"

        commands = [
            ["build-review-packet", "--case", str(case), "--snapshot", str(snapshot),
             "--candidates", str(candidates), "--out", str(packet)],
            ["resume-labels", "--packet", str(packet), "--out", str(labels)],
        ]
        for args in commands:
            self.assertEqual(run_cli(args), 0)
        label_doc = json.loads(labels.read_text())
        label_doc["rows"][0]["human"] = {"decision": "eligible_bench", "reason_codes": ["synthetic_fit"],
            "notes": "", "reviewer": "Synthetic Reviewer", "reviewed_at": "2026-07-31T00:00:00Z"}
        labels.write_text(json.dumps(label_doc) + "\n")
        self.assertEqual(run_cli([
            "finalize-human-labels", "--packet", str(packet), "--labels", str(labels),
            "--snapshot", str(snapshot), "--out", str(gt),
        ]), 0)
        self.assertEqual(json.loads(gt.read_text())["labels"][0]["decision"], "eligible_bench")

        run = root / "run"
        run.mkdir()
        fixture = FunnelFixture(powerpacks / "search-runs" / "fixtures")
        membership = json.loads((fixture.dir / "stage-membership.json").read_text())
        membership["candidates"] = [row for row in membership["candidates"] if row["person_id"] == "p1"]
        membership["candidates"][0]["person_id"] = "synthetic-person"
        membership["candidates"][0]["name"] = "Synthetic Person"
        membership["total_sourced"] = 1
        (run / "stage-membership.json").write_text(json.dumps(membership) + "\n")
        frontier = json.loads((fixture.dir / "candidate-frontier.json").read_text())
        frontier["candidates"] = [row for row in frontier["candidates"] if row["person_id"] == "p1"]
        frontier["candidates"][0]["person_id"] = "synthetic-person"
        frontier["candidates"][0]["hydrated_profile"]["name"] = "Synthetic Person"
        frontier["input_count"] = frontier["output_count"] = 1
        (run / "candidate-frontier.json").write_text(json.dumps(frontier) + "\n")
        run_corpus = dict(snapshot_doc)
        stable_run_corpus = {key: value for key, value in run_corpus.items() if key != "observed_at"}
        (run / "search_spec.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
        (run / "review").mkdir()
        (run / "review/plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        (run / "review/corpus.json").write_text(json.dumps(run_corpus, indent=2, sort_keys=True) + "\n")
        (run / "review/source.json").write_text(json.dumps({
            "normalized_jd": typed_spec.recruiting.source,
        }, indent=2, sort_keys=True) + "\n")
        (run / "review/evidence.json").write_text(json.dumps(
            bench.ReviewEvidenceSnapshot.from_hashes(run_corpus["evidence_hashes"]).to_dict(),
            indent=2, sort_keys=True,
        ) + "\n")
        (run / "review/binding.json").write_text(json.dumps({
            "schema_version": "recruiting.review-binding.v1",
            "plan_sha256": typed_spec.recruiting.reviewed_plan_hash,
            "source_sha256": bench.canonical_hash(typed_spec.recruiting.source),
            "jd_sha256": bench.canonical_hash(typed_spec.recruiting.source),
            "corpus_sha256": bench.canonical_hash(stable_run_corpus),
            "corpus": stable_run_corpus,
            "review_pool_person_ids": ["synthetic-person"],
            "review_pool_person_ids_sha256": bench.canonical_hash(["synthetic-person"]),
        }, indent=2, sort_keys=True) + "\n")
        hard_filter = run / "hard-filter-validation.json"
        hard_filter.write_text(json.dumps({
            "schema_version": "reflect.hard_filter_validation.v1",
            "case_id": "production", "case_hash": bench.canonical_hash(spec),
            "corpus_snapshot_hash": bench.canonical_hash(stable_run_corpus), "reviewed_count": 1,
            "violation_count": 0, "violations": [], "producer": "typed_runner",
            "generated_at": "2026-07-31T00:00:00Z",
        }) + "\n")
        manifest_artifacts = {
            "search_spec_json": "search_spec.json",
            "review_plan_json": "review/plan.json",
            "review_binding_json": "review/binding.json",
            "review_corpus_json": "review/corpus.json",
            "review_source_json": "review/source.json",
            "review_evidence_json": "review/evidence.json",
            "stage-membership.json": "stage-membership.json",
            "candidate-frontier.json": "candidate-frontier.json",
            "hard_filter_validation_json": "hard-filter-validation.json",
        }
        (run / "manifest.json").write_text(json.dumps({
            "schema_version": "search.manifest.v1",
            "artifacts": {key: {"path": relative, "sha256": bench._file_hash(run / relative)}
                          for key, relative in manifest_artifacts.items()},
        }, indent=2) + "\n")
        result_path = root / "results/synthetic-gtm-senior-ic/result.json"
        report_path = root / "report.json"
        strict_commands = [
            ["score", "--run-dir", str(run), "--gt", str(gt), "--case", str(case),
             "--snapshot", str(snapshot), "--hard-filter-validation", str(hard_filter),
             "--slug", "synthetic-gtm-senior-ic", "--out", str(result_path)],
            ["report", "--results-dir", str(root / "results"), "--out", str(report_path)],
            ["gate", "--baseline", str(report_path), "--current", str(report_path)],
        ]
        for args in strict_commands:
            self.assertEqual(run_cli(args), 0, args)

    def test_lifecycle_rejects_output_outside_reflect_root(self) -> None:
        with self.assertRaises(ValueError):
            bench._local_output(
                str(Path(tempfile.gettempdir()) / "not-reflect.json"),
                bench.GT_DIR / "x.json",
            )

    def test_module_cli_rejects_incomplete_legacy_score(self) -> None:
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        powerpacks = sandbox / ".powerpacks"
        fixture = FunnelFixture(powerpacks / "search-runs")
        root = powerpacks / "reflect/test-legacy-diagnostic"
        out = root / "result.json"
        with (
            mock.patch.object(bench, "POWERPACKS_STATE", powerpacks),
            mock.patch.object(bench, "REFLECT_STATE", powerpacks / "reflect"),
            mock.patch.object(
                sys,
                "argv",
                ["bench", "score", "--run-dir", str(fixture.dir), "--gt", str(fixture.gt_flat()),
                 "--ks", "10,25", "--out", str(out)],
            ),
        ):
            with self.assertRaisesRegex(ValueError, "strict scoring requires --case"):
                bench.main()

    def test_strict_score_rejects_run_outside_repo_powerpacks(self) -> None:
        sandbox = BenchSandbox()
        patchers = sandbox.patches()
        for patcher in patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])
        self.addCleanup(lambda: shutil.rmtree(sandbox.tmp, ignore_errors=True))
        fixture = FunnelFixture(sandbox.search_runs)
        args = _score_args(fixture)
        outside_root = Path(tempfile.mkdtemp()) / ".powerpacks"
        self.addCleanup(lambda: shutil.rmtree(outside_root.parent, ignore_errors=True))
        with self.assertRaises(ValueError):
            with mock.patch.object(bench, "POWERPACKS_STATE", outside_root):
                bench.cmd_score(args)


def candidate_evidence() -> dict:
    return {"role": [{"title": "Synthetic Engineer", "current": True, "start_date": None, "end_date": None}],
            "company": [{"name": "Example Systems", "relationship": "current"}],
            "location": [{"value": "Example City", "source": "profile"}],
            "matched_positions": [{"position_id": "synthetic-position", "title": "Synthetic Engineer",
                                   "company": "Example Systems", "location": "Example City"}],
            "retrieval_provenance": [{"lane": "synthetic", "probe": "probe", "rank": 1, "score": 0.5}],
            "relevant_profile_evidence": ["Synthetic evidence."]}


class TestCostReport(unittest.TestCase):
    def test_by_stage_and_model_with_unpriced_fallback(self) -> None:
        rows = [
            {"model": "priced-model", "stage": "triage", "prompt_tokens": 1_000_000, "completion_tokens": 0, "reasoning_tokens": 0, "latency_ms": 100},
            {"model": "priced-model", "stage": "judge", "prompt_tokens": 0, "completion_tokens": 500_000, "reasoning_tokens": 0, "latency_ms": 200},
            {"model": "mystery-model", "stage": "judge", "prompt_tokens": 10, "completion_tokens": 10, "reasoning_tokens": 0, "latency_ms": 50},
        ]
        prices = {"priced-model": {"input_per_1m": 1.0, "output_per_1m": 2.0}}
        report = cr.build_report(rows, prices)
        self.assertEqual(report["totals"]["calls"], 3)
        self.assertEqual(report["totals"]["cost_usd"], 2.0)  # $1 prompt + $1 completion
        self.assertFalse(report["totals"]["fully_priced"])   # mystery-model is unpriced
        self.assertEqual(report["by_stage"]["triage"]["cost_usd"], 1.0)
        self.assertTrue(report["by_stage"]["triage"]["fully_priced"])
        self.assertFalse(report["by_stage"]["judge"]["fully_priced"])
        self.assertEqual(report["by_model"]["mystery-model"]["cost_usd"], 0.0)
        self.assertEqual(report["latency_total_ms"], 350)

    def test_committed_price_table_parses_and_nulls_are_unpriced(self) -> None:
        prices = cr.load_prices(cr.DEFAULT_PRICES_PATH)
        self.assertIn("gpt-4.1-mini", prices)
        row = {"model": "gpt-4.1-mini", "prompt_tokens": 1_000_000, "completion_tokens": 0, "reasoning_tokens": 0}
        self.assertAlmostEqual(cr.row_cost_usd(row, prices), 0.4)
        luna_row = {"model": "gpt-5.6-luna", "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "reasoning_tokens": 0}
        self.assertAlmostEqual(cr.row_cost_usd(luna_row, prices), 7.0)
        self.assertIsNone(cr.row_cost_usd({"model": "gpt-5.2", "prompt_tokens": 10}, prices))  # delisted, kept null


if __name__ == "__main__":
    unittest.main()
