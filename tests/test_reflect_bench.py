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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from test_score_funnel import FunnelFixture  # noqa: E402


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
        self.tmp = Path(tempfile.mkdtemp())
        self.results = self.tmp / "results"
        self.report = self.tmp / "report.json"
        self.suite = self.tmp / "suite"

    def patches(self):
        return [mock.patch.object(bench, "RESULTS_DIR", self.results),
                mock.patch.object(bench, "REPORT_PATH", self.report),
                mock.patch.object(bench, "SUITE_DIR", self.suite),
                mock.patch.object(bench, "REFLECT_STATE", self.tmp),
                mock.patch.object(bench, "GT_DIR", self.tmp / "gt"),
                mock.patch.object(bench, "POWERPACKS_STATE", Path("/tmp"))]


def _score_args(fx: FunnelFixture) -> argparse.Namespace:
    local = bench.GT_DIR / fx.dir.name
    local.mkdir(parents=True, exist_ok=True)
    case = local / "case.json"
    spec = {"profile": "recruiting", "query": "Synthetic systems role"}
    case.write_text(json.dumps({
        "schema_version": "reflect.case.v1", "case_id": fx.dir.name,
        "public_source": {"reference": "https://example.invalid/synthetic-role", "content_hash": "1" * 64},
        "reviewed_search_spec": {"content": spec, "content_hash": bench.canonical_hash(spec)},
    }, indent=2) + "\n")
    person_ids = [f"p{i}" for i in range(1, 12)] + ["p20"]
    evidence = {pid: bench.canonical_hash({"synthetic": pid}) for pid in person_ids}
    snapshot_doc = {
        "schema_version": "reflect.corpus_snapshot.v1", "backend": "synthetic",
        "source": "synthetic_test_fixture", "verification_status": "verified_comparable",
        "set_id": "synthetic-set", "operator_scope_hash": "2" * 64, "membership_hash": "3" * 64,
        "namespace_schema_hashes": {"people": "4" * 64}, "native_content_version": "synthetic-v1",
        "evidence_hashes": evidence,
    }
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
    hard_filter = local / "hard-filter-validation.json"
    hard_filter.write_text(json.dumps({
        "schema_version": "reflect.hard_filter_validation.v1", "case_id": fx.dir.name,
        "case_hash": bench._file_hash(case), "corpus_snapshot_hash": bench.snapshot_identity(snapshot_doc),
        "reviewed_count": len(evidence), "violation_count": 0, "violations": [],
        "producer": "synthetic_contract_fixture", "generated_at": "2026-07-31T00:00:00Z",
    }) + "\n")
    meta = bench.SUITE_DIR / fx.dir.name
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "meta.json").write_text(json.dumps({"case_id": fx.dir.name, "job_family": "synthetic"}) + "\n")
    return argparse.Namespace(run_dir=str(fx.dir), gt=str(gt), case=str(case), slug=None, ks="10,25",
                              score_threshold=0.40, usage_log=None, snapshot=str(snapshot),
                              hard_filter_validation=str(hard_filter), out=None)


class TestBenchScoreAndReport(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = BenchSandbox()
        self.fx = FunnelFixture()
        self.patchers = self.sandbox.patches()
        for p in self.patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

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
        self.assertIsNone(result["cost"])  # no usage.jsonl in the fixture

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
                                  review_template_out=str(self.sandbox.tmp / "review.json"), enforce=True)
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
            "label_hash": "d" * 64, "source_recall": 0.8, "frontier_coverage": 0.8,
            "triage_survival": 0.8, "hard_filter_violations": 0, "unreviewed_candidate_count": 0,
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
    def test_direct_script_help_and_lifecycle_help(self) -> None:
        script = ROOT / "packs/search/reflect/bench.py"
        for args in (["--help"], ["build-review-packet", "--help"], ["resume-labels", "--help"],
                     ["finalize-human-labels", "--help"]):
            result = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifecycle_commands_execute_as_direct_script(self) -> None:
        root = ROOT / ".powerpacks/reflect" / f"test-synthetic-lifecycle-{uuid.uuid4().hex}"
        shutil.rmtree(root, ignore_errors=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        root.mkdir(parents=True)
        script = ROOT / "packs/search/reflect/bench.py"
        spec = {"query": "Synthetic role"}
        case = root / "case.json"
        case.write_text(json.dumps({"schema_version": "reflect.case.v1", "case_id": "synthetic-gtm-senior-ic",
            "public_source": {"reference": "https://example.invalid/role", "content_hash": "1" * 64},
            "reviewed_search_spec": {"content": spec, "content_hash": bench.canonical_hash(spec)}}) + "\n")
        evidence = candidate_evidence()
        evidence_hash = bench.canonical_hash(evidence)
        snapshot_doc = {"schema_version": "reflect.corpus_snapshot.v1", "backend": "synthetic",
            "source": "synthetic_test_fixture", "verification_status": "verified_comparable",
            "set_id": "synthetic-set", "operator_scope_hash": "2" * 64, "membership_hash": "3" * 64,
            "namespace_schema_hashes": {"people": "4" * 64}, "native_content_version": "v1",
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
            result = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        label_doc = json.loads(labels.read_text())
        label_doc["rows"][0]["human"] = {"decision": "eligible_bench", "reason_codes": ["synthetic_fit"],
            "notes": "", "reviewer": "Synthetic Reviewer", "reviewed_at": "2026-07-31T00:00:00Z"}
        labels.write_text(json.dumps(label_doc) + "\n")
        result = subprocess.run([sys.executable, str(script), "finalize-human-labels", "--packet", str(packet),
            "--labels", str(labels), "--snapshot", str(snapshot), "--out", str(gt)], cwd=ROOT,
            text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(gt.read_text())["labels"][0]["decision"], "eligible_bench")

        run = root / "run"
        (run / "epoch0").mkdir(parents=True)
        (run / "judges").mkdir()
        (run / "shortlist").mkdir()
        (run / "master_union.jsonl").write_text(json.dumps({
            "person_id": "synthetic-person", "found_by": ["synthetic-probe"]}) + "\n")
        for name in ("candidate_frontier.full.jsonl", "candidate_frontier.to_judge.jsonl"):
            (run / "epoch0" / name).write_text(json.dumps({"candidate_id": "synthetic-person"}) + "\n")
        (run / "judges" / "loop.jsonl").write_text(json.dumps({"candidate_id": "synthetic-person"}) + "\n")
        consensus = [{"person_id": "synthetic-person", "inband_votes": 1, "notout_votes": 1,
                      "gated_votes": 0, "mean_score": 0.9, "core_met": True}]
        (run / "shortlist" / "consensus.json").write_text(json.dumps(consensus) + "\n")
        (run / "shortlist" / "ranked_final.json").write_text(json.dumps([
            {"person_id": "synthetic-person", "rank": 1}]) + "\n")
        hard_filter = root / "hard-filter-validation.json"
        hard_filter.write_text(json.dumps({
            "schema_version": "reflect.hard_filter_validation.v1",
            "case_id": "synthetic-gtm-senior-ic", "case_hash": bench._file_hash(case),
            "corpus_snapshot_hash": bench.snapshot_identity(snapshot_doc), "reviewed_count": 1,
            "violation_count": 0, "violations": [], "producer": "synthetic_contract_fixture",
            "generated_at": "2026-07-31T00:00:00Z",
        }) + "\n")
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
            result = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr}\n{result.stdout}")

    def test_lifecycle_rejects_output_outside_reflect_root(self) -> None:
        with self.assertRaises(ValueError):
            bench._local_output("/tmp/not-reflect.json", bench.GT_DIR / "x.json")

    def test_direct_subprocess_legacy_diagnostic_score(self) -> None:
        fixture = FunnelFixture()
        root = ROOT / ".powerpacks/reflect/test-legacy-diagnostic"
        shutil.rmtree(root, ignore_errors=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        out = root / "result.json"
        result = subprocess.run([
            sys.executable, str(ROOT / "packs/search/reflect/bench.py"), "score",
            "--run-dir", str(fixture.dir), "--gt", str(fixture.gt_flat()), "--ks", "10,25", "--out", str(out),
        ], cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(out.read_text())
        self.assertIsNone(document["case_hash"])
        self.assertIsNone(document["corpus_hash"])

    def test_strict_score_rejects_run_outside_repo_powerpacks(self) -> None:
        args = _score_args(FunnelFixture())
        meta_dir = bench.SUITE_DIR / Path(args.run_dir).name
        self.addCleanup(lambda: shutil.rmtree(meta_dir, ignore_errors=True))
        with self.assertRaises(ValueError):
            with mock.patch.object(bench, "POWERPACKS_STATE", ROOT / ".powerpacks"):
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
