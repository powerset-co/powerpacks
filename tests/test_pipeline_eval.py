"""Validate pipeline eval harness without running agent or primitives.

Tests:
- Case loading from network-search-api recall YAMLs
- Bucket filtering (founders, date_range, education)
- Dry-run query listing
- Skip-LLM env var handling
- Dry-run mode
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH_EVALS = ROOT / "packs" / "search" / "evals"
RECALL_DIR = Path("/Users/arthur/workspace/network-search-api/tests/recall")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pipeline_eval = load_module("pipeline_eval", SEARCH_EVALS / "run_pipeline_eval.py")


@unittest.skipUnless(RECALL_DIR.exists(), "network-search-api recall dir not present")
class PipelineEvalCaseLoadTests(unittest.TestCase):
    def test_loads_founder_cases(self) -> None:
        cases = pipeline_eval.select_cases(RECALL_DIR, "founders", None, False)
        self.assertGreaterEqual(len(cases), 5, "expected at least 5 founder cases")
        for c in cases:
            self.assertEqual(c.bucket, "founders")
            self.assertTrue(c.query, f"case {c.relpath} has empty query")

    def test_loads_date_range_cases(self) -> None:
        cases = pipeline_eval.select_cases(RECALL_DIR, "date_range", None, False)
        self.assertGreaterEqual(len(cases), 5, "expected at least 5 date_range cases")

    def test_loads_education_cases(self) -> None:
        cases = pipeline_eval.select_cases(RECALL_DIR, "education", None, False)
        self.assertGreaterEqual(len(cases), 4, "expected at least 4 education cases")

    def test_case_glob_filters(self) -> None:
        all_cases = pipeline_eval.select_cases(RECALL_DIR, None, None, False)
        filtered = pipeline_eval.select_cases(RECALL_DIR, None, "founders_backed", False)
        self.assertGreater(len(all_cases), len(filtered))
        for c in filtered:
            self.assertIn("founders_backed", c.relpath)

    def test_staging_excluded_by_default(self) -> None:
        cases = pipeline_eval.select_cases(RECALL_DIR, None, None, False)
        self.assertTrue(all(c.bucket != "staging" for c in cases))


@unittest.skipUnless(RECALL_DIR.exists(), "network-search-api recall dir not present")
class PipelineEvalDryRunShapeTests(unittest.TestCase):
    def test_case_metadata_contains_query(self) -> None:
        cases = pipeline_eval.select_cases(RECALL_DIR, "founders", None, False)
        meta = cases[0]
        self.assertTrue(meta.query)
        self.assertEqual(pipeline_eval.case_id(meta), meta.relpath.removesuffix(".yaml").replace("/", "__"))


class PipelineEvalSkipLlmTests(unittest.TestCase):
    def test_default_is_skip(self) -> None:
        self.assertTrue(pipeline_eval.skip_llm({}))

    def test_env_false_disables_skip(self) -> None:
        self.assertFalse(pipeline_eval.skip_llm({"POWERPACKS_PIPELINE_SKIP_LLM": "false"}))

    def test_env_true_enables_skip(self) -> None:
        self.assertTrue(pipeline_eval.skip_llm({"POWERPACKS_PIPELINE_SKIP_LLM": "true"}))

    def test_env_zero_disables_skip(self) -> None:
        self.assertFalse(pipeline_eval.skip_llm({"POWERPACKS_PIPELINE_SKIP_LLM": "0"}))


@unittest.skipUnless(RECALL_DIR.exists(), "network-search-api recall dir not present")
class PipelineEvalDryRunTests(unittest.TestCase):
    def test_dry_run_founders(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SEARCH_EVALS / "run_pipeline_eval.py"),
                "--recall-dir", str(RECALL_DIR),
                "--bucket", "founders",
                "--max-cases", "2",
                "--dry-run",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"dry-run failed:\n{proc.stderr}")
        out = json.loads(proc.stdout)
        self.assertEqual(out["mode"], "dry-run")
        self.assertTrue(out["skip_llm"])
        self.assertGreaterEqual(len(out["queries"]), 1)
        self.assertIn("query", out["queries"][0])

    def test_list_founders(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SEARCH_EVALS / "run_pipeline_eval.py"),
                "--recall-dir", str(RECALL_DIR),
                "--bucket", "founders",
                "--list",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"list failed:\n{proc.stderr}")
        cases = json.loads(proc.stdout)
        self.assertGreaterEqual(len(cases), 5)
        for c in cases:
            self.assertIn("founders", c["id"])


if __name__ == "__main__":
    unittest.main()
