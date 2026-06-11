"""Validate every skill that has a cases.json:

  - the cases file parses and has the required keys
  - the skill referenced exists
  - `scripts/run-skill-eval --mode dry-run` succeeds end-to-end (prompt
    builds, validation harness runs, report writes)

This is the CI safety net for the generic skill-eval harness. It does NOT
invoke the host agent (no model spend, no network).
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = ROOT / "packs"
RUN_SKILL_EVAL = ROOT / "scripts" / "run-skill-eval"


def discover_skill_eval_cases() -> list[tuple[str, Path]]:
    """Return [(skill_name, cases_path)] for every skill with eval cases."""
    out: list[tuple[str, Path]] = []
    # Two conventions: packs/<pack>/evals/<skill>/cases.json
    for cases in PACKS_DIR.glob("*/evals/*/cases.json"):
        skill_name = cases.parent.name
        skill_md = next(
            (p for p in PACKS_DIR.glob(f"*/skills/{skill_name}/SKILL.md")),
            None,
        )
        if skill_md is None:
            continue
        out.append((skill_name, cases))
    # And packs/<pack>/skills/<skill>/evals/cases.json
    for cases in PACKS_DIR.glob("*/skills/*/evals/cases.json"):
        skill_name = cases.parent.parent.name
        out.append((skill_name, cases))
    return out


class SkillEvalDiscoveryTests(unittest.TestCase):
    def test_at_least_one_skill_has_eval_cases(self) -> None:
        cases = discover_skill_eval_cases()
        self.assertGreater(len(cases), 0, "expected at least one skill to have eval cases")

    def test_runner_script_is_executable(self) -> None:
        self.assertTrue(RUN_SKILL_EVAL.exists(), f"{RUN_SKILL_EVAL} missing")
        self.assertTrue(
            RUN_SKILL_EVAL.stat().st_mode & 0o111,
            f"{RUN_SKILL_EVAL} not executable",
        )


class SkillEvalCaseShapeTests(unittest.TestCase):
    """Validate every cases.json has a sane shape."""

    def test_each_case_file_loads_and_has_required_keys(self) -> None:
        for skill_name, cases_path in discover_skill_eval_cases():
            with self.subTest(skill=skill_name, file=str(cases_path)):
                raw = json.loads(cases_path.read_text())
                self.assertIsInstance(raw, list, f"{cases_path}: expected a JSON array")
                self.assertGreater(len(raw), 0, f"{cases_path}: empty case file")
                for i, entry in enumerate(raw):
                    self.assertIsInstance(entry, dict, f"{cases_path}[{i}]")
                    self.assertIn("id", entry, f"{cases_path}[{i}]: missing 'id'")
                    self.assertIn("query", entry, f"{cases_path}[{i}]: missing 'query'")
                    # The LLM judge grades against this rubric; keyword lists
                    # (must_include / must_not_include) are intentionally not
                    # supported — they grade vocabulary, not behavior.
                    self.assertIsInstance(
                        entry.get("expected_behavior"), str,
                        f"{cases_path}[{i}]: missing 'expected_behavior' rubric",
                    )
                    self.assertGreater(
                        len(entry["expected_behavior"].strip()), 40,
                        f"{cases_path}[{i}]: expected_behavior rubric is too thin to judge against",
                    )
                    self.assertNotIn("must_include", entry, f"{cases_path}[{i}]: keyword lists are not supported; use expected_behavior")
                    self.assertNotIn("must_not_include", entry, f"{cases_path}[{i}]: keyword lists are not supported; use expected_behavior")
                    if "expected_tools" in entry:
                        self.assertIsInstance(
                            entry["expected_tools"], list, f"{cases_path}[{i}].expected_tools must be a list"
                        )

    def test_case_ids_are_unique_within_a_skill(self) -> None:
        for skill_name, cases_path in discover_skill_eval_cases():
            with self.subTest(skill=skill_name):
                raw = json.loads(cases_path.read_text())
                ids = [c["id"] for c in raw]
                self.assertEqual(len(ids), len(set(ids)), f"{cases_path}: duplicate case ids")


class SkillEvalDryRunTests(unittest.TestCase):
    """Run `scripts/run-skill-eval --mode dry-run --json` for every skill."""

    def test_dry_run_succeeds_for_every_skill(self) -> None:
        for skill_name, _ in discover_skill_eval_cases():
            with self.subTest(skill=skill_name):
                proc = subprocess.run(
                    [
                        str(RUN_SKILL_EVAL),
                        "--skill",
                        skill_name,
                        "--mode",
                        "dry-run",
                        "--json",
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"dry-run failed for {skill_name}\nstdout:{proc.stdout}\nstderr:{proc.stderr}",
                )
                summary = json.loads(proc.stdout)
                self.assertEqual(summary["skill"], skill_name)
                self.assertEqual(summary["mode"], "dry-run")
                self.assertGreater(summary["cases_total"], 0)
                self.assertEqual(
                    summary["cases_total"],
                    summary["cases_passed"],
                    f"{skill_name}: not all cases passed dry-run",
                )
                # Report file should exist.
                report_path = Path(summary["report"])
                self.assertTrue(report_path.exists(), f"missing report: {report_path}")


if __name__ == "__main__":
    unittest.main()
