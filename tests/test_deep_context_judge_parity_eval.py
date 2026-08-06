from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.ingestion.primitives.deep_context import identity_evidence
from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.tools import (
    judge_parity_data,
    judge_parity_eval,
    judge_parity_replay,
)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("public_identifier", "person_id", "action", "approved"),
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _profile(identifier: str) -> dict[str, Any]:
    return {
        "has_profile": True,
        "public_identifier": identifier,
        "linkedin_url": f"https://www.linkedin.com/in/{identifier}",
        "full_name": identifier.title(),
        "headline": "Engineer",
        "location": "Springfield",
        "experiences": ["Engineer at Example"],
        "education": ["Example University"],
    }


def _install(tmp_path: Path) -> Path:
    deep_context = tmp_path / ".powerpacks" / "deep-context"
    _write_csv(
        tmp_path / ".powerpacks" / "network-import" / "overrides" / "review.csv",
        [
            {
                "public_identifier": "jordan-bravo",
                "person_id": "person-a",
                "action": "verify",
                "approved": "yes",
            },
            {
                "public_identifier": "casey-delta",
                "person_id": "person-b",
                "action": "detach",
                "approved": "yes",
            },
            {
                "public_identifier": "missing-echo",
                "person_id": "person-c",
                "action": "retarget",
                "approved": "yes",
            },
        ],
    )
    _write_jsonl(
        deep_context / "reconcile" / "verdicts.jsonl",
        [
            {
                "candidate_key": "jordan-bravo",
                "name": "Jordan Bravo",
                "person_ids": ["person-a"],
                "linkedin": _profile("jordan-bravo"),
                "verdict": {"verdict": "confirmed"},
            },
            {
                "candidate_key": "casey-delta",
                "name": "Casey Delta",
                "person_ids": ["person-b"],
                "linkedin": _profile("casey-delta"),
                "research_proposal": True,
                "research_confidence": 0.42,
                "research_unverified": True,
                "verdict": {"verdict": "needs_review"},
            },
            {
                "candidate_key": "taylor-foxtrot",
                "name": "Taylor Foxtrot",
                "person_ids": ["person-d"],
                "linkedin": {},
                "verdict": {"verdict": "wrong_person"},
            },
        ],
    )
    for person_id, name in (
        ("person-a", "Jordan Bravo"),
        ("person-b", "Casey Delta"),
    ):
        _write_jsonl(
            deep_context / "facts" / f"{person_id}.jsonl",
            [
                {
                    "facts": {
                        "canonical_name": name,
                        "title": "Engineer",
                        "employers": [
                            {"name": "Example", "role": "Engineer", "status": "current"}
                        ],
                    }
                }
            ],
        )
    (deep_context / "owner.json").write_text(
        json.dumps({"name": "Owner Example"}),
        encoding="utf-8",
    )
    return deep_context


def _json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class JudgeParityEvalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.deep_context = _install(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_free_baseline_counts_agreement_abstention_and_missing(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = judge_parity_eval.main(
                ["--deep-context", str(self.deep_context)]
            )

        self.assertEqual(code, 0)
        payload = _json_lines(output.getvalue())[0]
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["mode"], "historical_baseline")
        self.assertEqual(
            payload["installs"][0],
            {
                "install": self.root.name,
                "human_decided": 3,
                "human_no_comparator": 0,
                "historical_agree": 1,
                "historical_disagree": 0,
                "historical_abstain": 1,
                "historical_missing": 1,
                "historical_ambiguous": 0,
            },
        )

    def test_replay_without_approval_estimates_and_never_calls_judge(self) -> None:
        def unexpected_judge(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("paid judge called before approval")

        output = io.StringIO()
        with patch.object(judge_parity_replay, "judge_batch", unexpected_judge):
            with redirect_stdout(output):
                code = judge_parity_eval.main(
                    ["--deep-context", str(self.deep_context), "--replay"]
                )

        self.assertEqual(code, EXIT_NEEDS_APPROVAL)
        estimate, gate = _json_lines(output.getvalue())
        self.assertEqual(estimate["status"], "dry_run")
        self.assertEqual(estimate["replayable"], 2)
        self.assertGreater(estimate["estimated_input_tokens"], 0)
        self.assertEqual(estimate["estimated_output_tokens"], 1500)
        self.assertEqual(gate["status"], "needs_approval")

    def test_estimate_uses_production_research_packet(self) -> None:
        seen = []
        production_packet = identity_evidence.IdentityTask.packet

        def packet(task: dict[str, Any]):
            parsed = production_packet(task)
            seen.append(parsed)
            return parsed

        with tempfile.TemporaryDirectory() as temporary:
            install = judge_parity_data.load_install(
                self.deep_context,
                Path(temporary),
                "fixture",
            )
            with patch.object(
                judge_parity_replay.IdentityTask,
                "packet",
                side_effect=packet,
            ):
                estimate = judge_parity_replay.estimate(
                    [install], "gpt-5-mini", "medium"
                )

        self.assertEqual(estimate["replayable"], 2)
        research_packets = [item for item in seen if item[2] == IdentityOrigin.RESEARCH]
        self.assertEqual(len(research_packets), 1)
        profile = research_packets[0][1]
        self.assertEqual(profile["_research_confidence"], 0.42)
        self.assertTrue(profile["_research_unverified"])

    def test_replay_uses_production_packet_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = judge_parity_data.load_install(
                self.deep_context,
                Path(temporary),
                "fixture",
            )
        case = next(
            row for row in install.replay_cases if row.identifier == "casey-delta"
        )
        self.assertEqual(case.task["research_confidence"], 0.42)
        self.assertTrue(case.task["research_unverified"])
        evidence, profile, origin = identity_evidence.IdentityTask.packet(case.task)
        self.assertEqual(origin, IdentityOrigin.RESEARCH)
        expected = identity_evidence.judgment_fingerprint(
            evidence,
            profile,
            origin,
            install.owner_block,
        )
        self.assertEqual(
            identity_evidence.task_fingerprint(case.task, install.owner_block),
            expected,
        )
        batch_results = []

        def offline_batch(tasks: list[dict[str, Any]], **kwargs: Any):
            results = identity_evidence.judge_batch(
                tasks,
                **{**kwargs, "use_llm": False},
            )
            batch_results.extend(results)
            return results

        with patch.object(judge_parity_replay, "judge_batch", side_effect=offline_batch):
            report = judge_parity_replay.replay(
                [install],
                model="gpt-5-mini",
                effort="medium",
                concurrency=1,
                timeout=1,
                max_retries=0,
            )
        replayed = dict(zip(
            (row.identifier for row in install.replay_cases),
            batch_results,
        ))
        self.assertEqual(replayed["casey-delta"]["fingerprint"], expected)
        self.assertEqual(
            replayed["casey-delta"]["verdict"]["verdict"],
            "wrong_person",
        )
        self.assertEqual(report["replay"][0]["replayed"], 2)

    def test_approved_replay_uses_unified_judge_and_lists_flips(self) -> None:
        calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

        def fake_judge(
            tasks: list[dict[str, Any]],
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            calls.append((tasks, kwargs))
            return [
                {"verdict": {"verdict": "wrong_person"}, "error": ""},
                {"verdict": {"verdict": "wrong_person"}, "error": ""},
            ]

        output = io.StringIO()
        with patch.object(judge_parity_replay, "judge_batch", fake_judge):
            with redirect_stdout(output):
                code = judge_parity_eval.main(
                    [
                        "--deep-context",
                        str(self.deep_context),
                        "--replay",
                        "--approve-spend",
                    ]
                )

        self.assertEqual(code, 0)
        estimate, result = _json_lines(output.getvalue())
        self.assertEqual(estimate["status"], "dry_run")
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            all("evidence" in task and "linkedin" in task for task in calls[0][0])
        )
        self.assertEqual(
            result["replay"],
            [
                {
                    "install": self.root.name,
                    "replayed": 2,
                    "errors": 0,
                    "new_vs_old_agree": 0,
                    "new_vs_old_flip": 2,
                    "new_vs_human_agree": 1,
                    "new_vs_human_disagree": 1,
                    "new_vs_human_abstain": 0,
                    "human_replayed": 2,
                }
            ],
        )
        self.assertEqual(
            result["flips"],
            [
                {
                    "install": self.root.name,
                    "identifier": "jordan-bravo",
                    "historical": "confirmed",
                    "replay": "wrong_person",
                    "human": "confirmed",
                },
                {
                    "install": self.root.name,
                    "identifier": "casey-delta",
                    "historical": "needs_review",
                    "replay": "wrong_person",
                    "human": "wrong_person",
                },
            ],
        )
        self.assertNotIn("Engineer", json.dumps(result["flips"]))

    def test_artifacts_are_parsed_only_after_copying(self) -> None:
        parsed_paths: list[Path] = []
        original_csv_rows = judge_parity_data._csv_rows
        original_json_rows = judge_parity_data._json_rows

        def csv_rows(path: Path) -> list[dict[str, str]]:
            parsed_paths.append(path)
            return original_csv_rows(path)

        def json_rows(path: Path) -> list[dict[str, Any]]:
            parsed_paths.append(path)
            return original_json_rows(path)

        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary)
            with patch.object(judge_parity_data, "_csv_rows", csv_rows):
                with patch.object(judge_parity_data, "_json_rows", json_rows):
                    judge_parity_data.load_install(
                        self.deep_context,
                        staged,
                        "fixture",
                    )
            self.assertTrue(parsed_paths)
            self.assertTrue(all(path.is_relative_to(staged) for path in parsed_paths))


if __name__ == "__main__":
    unittest.main()
