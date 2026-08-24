"""Offline contract tests for typed recall parity compilation and scoring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.search.evals.search_spec_factory import UnsupportedCaseError, build_search_spec, load_case, run_case
from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord, StageResult


class RecallSpecFactoryTests(unittest.TestCase):
    def case(self, text: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "founders_case.yaml"
        path.write_text(text)
        return load_case(path, root)

    def test_remote_spec_requires_explicit_set_and_operator_scope(self) -> None:
        meta = self.case("query: founders in Argentina\nexpected_count: 1\n")
        with self.assertRaisesRegex(ValueError, "explicit set_id and operator_ids"):
            build_search_spec(meta, backend="powerset")
        spec = build_search_spec(meta, backend="powerset", set_id="set-1", operator_ids=("operator-2", "operator-1"))
        self.assertEqual(spec.corpus.set_id, "set-1")
        self.assertEqual(spec.corpus.operator_ids, ("operator-2", "operator-1"))
        self.assertEqual(spec.role.role_ids, ("founder",))
        self.assertEqual(spec.person_filters.countries, ("Argentina",))

    def test_unrepresentable_legacy_filters_fail_closed(self) -> None:
        meta = self.case("query: founders in Europe with 100k LinkedIn followers\nexpected_count: 1\n")
        with self.assertRaises(UnsupportedCaseError) as raised:
            build_search_spec(meta, backend="powerset", set_id="set-1", operator_ids=("operator-1",))
        self.assertEqual(set(raised.exception.fields), {"li_followers_min", "macro_regions"})

    def test_unknown_explicit_filter_fails_closed(self) -> None:
        meta = self.case("query: founders\nexpected_count: 1\nrole_search_filters:\n  imaginary_filter: required\n")
        with self.assertRaises(UnsupportedCaseError) as raised:
            build_search_spec(meta, backend="powerset", set_id="set-1", operator_ids=("operator-1",))
        self.assertEqual(raised.exception.fields, ("imaginary_filter",))

    def test_structured_seniority_expansion_remains_deterministic(self) -> None:
        meta = self.case("query: experienced people\nexpected_count: 1\nuse_expand_seniority: true\n")
        spec = build_search_spec(
            meta,
            backend="powerset",
            set_id="set-1",
            operator_ids=("operator-1",),
        )
        self.assertIn("c-suite", spec.person_filters.seniority_bands)

    def test_run_case_scores_canonical_frontier_counts_and_artifacts(self) -> None:
        person_id = "11111111-1111-5111-8111-111111111111"
        meta = self.case(f"query: founders in Argentina\nexpected_person_ids:\n  - {person_id}\nmin_recall: 1\n")
        captured = {}

        def fake_run_search(spec, *, output_dir):
            captured["output_dir"] = output_dir
            candidate = CandidateRecord(person_id, hydration_disposition="hydrated")
            return StageResult(
                "gtm",
                "completed",
                CandidateFrontier.merge((candidate,)),
                counts={"eligible_pool": 7, "retrieved": 1, "hydrated": 1},
                artifact_paths={"result_json": str(output_dir / "result.json")},
            )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_case(
                meta,
                output_root=Path(tmp) / ".powerpacks" / "search-runs" / "eval",
                backend="powerset",
                set_id="set-1",
                operator_ids=("operator-1",),
                run_search_fn=fake_run_search,
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["eligible_pool"], 7)
        self.assertEqual(result["returned_people"], 1)
        self.assertEqual(result["hydrated"], 1)
        self.assertEqual(result["recall"], 1.0)
        self.assertIn("search-runs", str(captured["output_dir"]))

    def test_engine_unsupported_capability_is_preserved(self) -> None:
        meta = self.case("query: founders with Python\nexpected_count: 1\n")

        def fake_run_search(spec, *, output_dir):
            return StageResult(
                "capabilities",
                "unsupported_capability",
                CandidateFrontier.merge(()),
                errors=("unsupported required hard filters: tech_skills",),
            )

        result = run_case(
            meta,
            output_root=Path(".powerpacks/search-runs/eval"),
            backend="powerset",
            set_id="set-1",
            operator_ids=("operator-1",),
            run_search_fn=fake_run_search,
        )
        self.assertEqual(result["status"], "unsupported_capability")
        self.assertIn("tech_skills", result["reason"])


if __name__ == "__main__":
    unittest.main()
