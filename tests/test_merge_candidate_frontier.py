"""Tests for merge_candidate_frontier primitive."""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path


class TestMergeCandidateFrontier(unittest.TestCase):
    """Test the merge / dedupe logic."""

    def _write_probe_csv(self, tmp: Path, probe_id: str, rows: list[dict]) -> Path:
        csv_path = tmp / f"{probe_id}.csv"
        fields = [
            "rank", "person_id", "result_index", "final_score",
            "trait_scores", "overall_reasoning", "matched_position_indexes",
            "pre_rerank_score", "tags", "vertical_sources", "name",
            "headline", "location", "current_titles", "current_companies",
            "linkedin_url", "hydrated", "source_run", "source_query",
        ]
        with csv_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                row = {f: "" for f in fields}
                row.update(r)
                writer.writerow(row)
        return csv_path

    def _write_probe_summaries(self, tmp: Path, probes: list[dict]) -> Path:
        path = tmp / "probe_summaries.json"
        path.write_text(json.dumps(probes, indent=2))
        return path

    def _write_plan(self, tmp: Path) -> Path:
        plan_path = tmp / "plan.json"
        plan_path.write_text(json.dumps({"job_title": "Test"}, indent=2))
        return plan_path

    def test_dedup_by_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv1 = self._write_probe_csv(tmp, "p1", [
                {"rank": "1", "person_id": "abc", "name": "Alice", "final_score": "0.9",
                 "linkedin_url": "https://linkedin.com/in/alice", "hydrated": "True",
                 "source_run": "run-1", "location": "NYC", "current_titles": "Eng",
                 "current_companies": "Acme"},
            ])
            csv2 = self._write_probe_csv(tmp, "p2", [
                {"rank": "1", "person_id": "abc", "name": "Alice", "final_score": "0.7",
                 "linkedin_url": "https://linkedin.com/in/alice", "hydrated": "True",
                 "source_run": "run-2", "location": "NYC"},
                {"rank": "2", "person_id": "def", "name": "Bob", "final_score": "0.6",
                 "linkedin_url": "https://linkedin.com/in/bob", "hydrated": "True",
                 "source_run": "run-2"},
            ])
            summaries = self._write_probe_summaries(tmp, [
                {"id": "p1", "status": "completed", "csv": str(csv1)},
                {"id": "p2", "status": "completed", "csv": str(csv2)},
            ])
            plan = self._write_plan(tmp)

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
                merge_candidates, read_probe_csv,
            )

            rows = read_probe_csv(csv1, "p1") + read_probe_csv(csv2, "p2")
            candidates = merge_candidates(rows)

            self.assertEqual(len(candidates), 2)
            # Alice appears in both probes
            alice = [c for c in candidates if c["name"] == "Alice"][0]
            self.assertEqual(sorted(alice["matched_probe_ids"]), ["p1", "p2"])
            self.assertEqual(alice["duplicate_signal"]["matched_probe_count"], 2)
            self.assertEqual(len(alice["source_rows"]), 2)

    def test_dedup_by_linkedin_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv1 = self._write_probe_csv(tmp, "p1", [
                {"rank": "1", "person_id": "", "name": "Carol",
                 "linkedin_url": "https://www.linkedin.com/in/Carol123/",
                 "final_score": "0.8"},
            ])
            csv2 = self._write_probe_csv(tmp, "p2", [
                {"rank": "1", "person_id": "", "name": "Carol",
                 "linkedin_url": "https://linkedin.com/in/carol123",
                 "final_score": "0.5"},
            ])

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
                merge_candidates, read_probe_csv,
            )
            rows = read_probe_csv(csv1, "p1") + read_probe_csv(csv2, "p2")
            candidates = merge_candidates(rows)

            self.assertEqual(len(candidates), 1)
            carol = candidates[0]
            self.assertEqual(len(carol["matched_probe_ids"]), 2)

    def test_skipped_probes_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv1 = self._write_probe_csv(tmp, "p1", [
                {"rank": "1", "person_id": "x", "name": "X", "final_score": "0.5"},
            ])
            summaries = self._write_probe_summaries(tmp, [
                {"id": "p1", "status": "completed", "csv": str(csv1)},
                {"id": "p2", "status": "failed", "csv": None},
            ])

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
                merge_candidates, read_probe_csv,
            )
            rows = read_probe_csv(csv1, "p1")
            candidates = merge_candidates(rows)
            self.assertEqual(len(candidates), 1)


class TestMergeRunShapes(unittest.TestCase):
    """run() accepts both the canonical bare list and the legacy wrapper."""

    def _build_run_dir(self, tmp: Path) -> Path:
        run_dir = tmp / "run"
        run_dir.mkdir()
        (run_dir / "plan.json").write_text(json.dumps({"job_title": "Test"}, indent=2))
        csv_path = run_dir / "p1.csv"
        fields = ["rank", "person_id", "final_score", "name", "headline",
                  "location", "current_titles", "current_companies",
                  "linkedin_url", "hydrated", "source_run"]
        with csv_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerow({
                "rank": "1", "person_id": "abc", "final_score": "0.9",
                "name": "Alice", "linkedin_url": "https://linkedin.com/in/alice",
                "hydrated": "True", "source_run": "run-1",
            })
        self.csv_path = csv_path
        return run_dir

    def _run_merge(self, run_dir: Path) -> dict:
        from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import run

        args = argparse.Namespace(
            run_dir=str(run_dir), probe_summaries=None, plan_json=None, out_dir=None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            run(args)
        return json.loads((run_dir / "candidate_frontier.json").read_text())

    def test_run_with_bare_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            run_dir = self._build_run_dir(Path(tmp_str))
            (run_dir / "probe_summaries.json").write_text(json.dumps([
                {"id": "p1", "status": "completed", "csv": str(self.csv_path)},
            ]))
            frontier = self._run_merge(run_dir)
            self.assertEqual(frontier["candidate_count"], 1)
            self.assertEqual(frontier["candidates"][0]["name"], "Alice")

    def test_run_with_legacy_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            run_dir = self._build_run_dir(Path(tmp_str))
            (run_dir / "probe_summaries.json").write_text(json.dumps({
                "probes": [
                    {"id": "p1", "status": "completed", "csv": str(self.csv_path)},
                ],
            }))
            frontier = self._run_merge(run_dir)
            self.assertEqual(frontier["candidate_count"], 1)

    def test_run_with_invalid_shape_exits_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            run_dir = self._build_run_dir(Path(tmp_str))
            (run_dir / "probe_summaries.json").write_text(json.dumps(["p1", "p2"]))
            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import run

            args = argparse.Namespace(
                run_dir=str(run_dir), probe_summaries=None, plan_json=None, out_dir=None,
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                run(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("error:", stderr.getvalue())


class TestCollectProbes(unittest.TestCase):
    """collect-probes generates the canonical bare list from task states."""

    def _write_state(self, tmp: Path, task_id: str, query: str, row_count: int) -> Path:
        artifact_dir = tmp / "artifacts" / task_id
        artifact_dir.mkdir(parents=True)
        csv_path = artifact_dir / f"{task_id}.csv"
        csv_path.write_text("rank,person_id\n" + "\n".join(
            f"{i},person-{i}" for i in range(1, row_count + 1)
        ) + "\n")
        state_path = tmp / f"{task_id}-query.json"
        state_path.write_text(json.dumps({
            "task_id": task_id,
            "task": "search_network",
            "status": "running",
            "query": query,
            "steps": [],
            "artifacts": {
                "task_id": task_id,
                "query": query,
                "state": str(state_path),
                "artifact_dir": str(artifact_dir),
                "csv": str(csv_path),
                "jsonl": str(artifact_dir / f"{task_id}.jsonl"),
                "row_count": row_count,
            },
        }, indent=2))
        return state_path

    def _collect(self, run_dir: Path, states: list[Path], probe_ids: list[str] | None = None,
                 fallback_reasons: list[str] | None = None) -> list[dict]:
        from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
            run_collect_probes,
        )

        args = argparse.Namespace(
            run_dir=str(run_dir),
            state=[str(s) for s in states],
            probe_id=probe_ids,
            fallback_reason=fallback_reasons,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            run_collect_probes(args)
        return json.loads((run_dir / "probe_summaries.json").read_text())

    def test_generates_canonical_bare_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir = tmp / "run"
            run_dir.mkdir()
            state1 = self._write_state(tmp, "task-1", "fintech founders", 2)
            state2 = self._write_state(tmp, "task-2", "ml engineers", 3)

            probes = self._collect(run_dir, [state1, state2], probe_ids=["profile_1", "profile_2"])

            self.assertIsInstance(probes, list)
            self.assertEqual(len(probes), 2)
            self.assertEqual(
                sorted(probes[0].keys()),
                sorted(["id", "status", "query", "artifact_dir", "csv", "state",
                        "found_count", "fallback_reason"]),
            )
            self.assertEqual(probes[0]["id"], "profile_1")
            self.assertEqual(probes[0]["status"], "completed")
            self.assertEqual(probes[0]["query"], "fintech founders")
            self.assertEqual(probes[0]["found_count"], 2)
            self.assertEqual(probes[0]["state"], str(state1))
            self.assertIsNone(probes[0]["fallback_reason"])
            self.assertEqual(probes[1]["id"], "profile_2")
            self.assertEqual(probes[1]["found_count"], 3)
            self.assertTrue(Path(probes[1]["csv"]).exists())

    def test_idempotent_rerun_produces_identical_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir = tmp / "run"
            run_dir.mkdir()
            state1 = self._write_state(tmp, "task-1", "fintech founders", 2)
            state2 = self._write_state(tmp, "task-2", "ml engineers", 3)

            self._collect(run_dir, [state1, state2], probe_ids=["profile_1", "profile_2"])
            first = (run_dir / "probe_summaries.json").read_bytes()
            self._collect(run_dir, [state1, state2], probe_ids=["profile_1", "profile_2"])
            second = (run_dir / "probe_summaries.json").read_bytes()
            self.assertEqual(first, second)

    def test_default_probe_id_is_task_id_and_fallback_reason_applies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir = tmp / "run"
            run_dir.mkdir()
            state1 = self._write_state(tmp, "task-1", "fintech founders", 1)

            probes = self._collect(
                run_dir, [state1],
                fallback_reasons=["task-1=turbopuffer_permit_overflow"],
            )
            self.assertEqual(probes[0]["id"], "task-1")
            self.assertEqual(probes[0]["fallback_reason"], "turbopuffer_permit_overflow")

    def test_state_without_csv_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir = tmp / "run"
            run_dir.mkdir()
            state_path = tmp / "task-3-query.json"
            state_path.write_text(json.dumps({
                "task_id": "task-3",
                "query": "no results run",
                "steps": [],
            }))
            probes = self._collect(run_dir, [state_path], probe_ids=["profile_3"])
            self.assertEqual(probes[0]["status"], "failed")
            self.assertIsNone(probes[0]["csv"])
            self.assertIsNone(probes[0]["found_count"])

    def test_collect_then_merge_round_trip(self) -> None:
        """The generated file feeds run() without modification."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir = tmp / "run"
            run_dir.mkdir()
            (run_dir / "plan.json").write_text(json.dumps({"job_title": "Test"}))
            artifact_dir = tmp / "artifacts" / "task-1"
            artifact_dir.mkdir(parents=True)
            csv_path = artifact_dir / "task-1.csv"
            with csv_path.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["rank", "person_id", "final_score", "name",
                                                        "linkedin_url", "hydrated", "source_run"])
                writer.writeheader()
                writer.writerow({"rank": "1", "person_id": "abc", "final_score": "0.8",
                                 "name": "Alice", "hydrated": "True"})
            state_path = tmp / "task-1-query.json"
            state_path.write_text(json.dumps({
                "task_id": "task-1",
                "query": "fintech founders",
                "artifacts": {
                    "artifact_dir": str(artifact_dir),
                    "csv": str(csv_path),
                    "row_count": 1,
                },
            }))

            self._collect(run_dir, [state_path], probe_ids=["profile_1"])

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import run

            args = argparse.Namespace(
                run_dir=str(run_dir), probe_summaries=None, plan_json=None, out_dir=None,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                run(args)
            frontier = json.loads((run_dir / "candidate_frontier.json").read_text())
            self.assertEqual(frontier["candidate_count"], 1)
            self.assertEqual(frontier["candidates"][0]["matched_probe_ids"], ["profile_1"])


class TestCaptureJdEvaluations(unittest.TestCase):
    """Test evaluation validation logic."""

    def test_validate_evaluation_good(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        ev = {
            "candidate_id": "abc",
            "rank": 1,
            "jd_score": 0.85,
            "verdict": "top_tier",
            "seniority_fit": "ideal",
            "must_have": [{"trait": "Python", "status": "strong", "evidence": "5yr exp"}],
            "nice_to_have": [],
            "duplicate_signal": {
                "matched_probe_count": 2,
                "matched_probe_ids": ["p1", "p2"],
                "interpretation": "Found in both probes",
            },
            "rationale": "Strong match overall",
            "caveats": [],
        }
        errors = validate_evaluation(ev, 0)
        self.assertEqual(errors, [])

    def test_validate_evaluation_bad_verdict(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        ev = {
            "candidate_id": "abc",
            "rank": 1,
            "jd_score": 0.5,
            "verdict": "excellent",
            "seniority_fit": "ideal",
            "must_have": [],
            "nice_to_have": [],
            "duplicate_signal": {
                "matched_probe_count": 1,
                "matched_probe_ids": ["p1"],
                "interpretation": "single",
            },
            "rationale": "ok",
            "caveats": [],
        }
        errors = validate_evaluation(ev, 0)
        self.assertTrue(any("verdict" in e for e in errors))

    def test_validate_evaluation_rejects_strong_or_maybe_out_of_band_seniority(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        base = {
            "candidate_id": "abc",
            "rank": 1,
            "jd_score": 0.5,
            "verdict": "high_potential",
            "seniority_fit": "too_senior",
            "must_have": [],
            "nice_to_have": [],
            "duplicate_signal": {
                "matched_probe_count": 1,
                "matched_probe_ids": ["p1"],
                "interpretation": "single",
            },
            "rationale": "CFO with stale analyst experience",
            "caveats": [],
        }
        errors = validate_evaluation(base, 0)
        self.assertTrue(any("cannot be used with seniority_fit" in e for e in errors))

        out = {**base, "verdict": "out"}
        self.assertEqual(validate_evaluation(out, 0), [])

    def test_validate_evaluation_missing_fields(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        ev: dict = {"candidate_id": "abc"}
        errors = validate_evaluation(ev, 0)
        self.assertTrue(len(errors) > 0)


if __name__ == "__main__":
    unittest.main()
