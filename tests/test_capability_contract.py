from __future__ import annotations

import os
import unittest
from unittest import mock

from packs.search.primitives.llm_rerank_candidates import capability_contract


class CapabilityContractTests(unittest.TestCase):
    inputs = {
        "jd": "Build reliable distributed systems.",
        "title": "Backend Engineer",
        "company_name": "Example Systems",
        "evaluation_query": "Must have operated production storage.",
        "judge": "jev",
    }

    def digest(self, **changes: str) -> str:
        return capability_contract.request_sha256(**{**self.inputs, **changes})

    def test_meaningful_inputs_and_frozen_definitions_invalidate(self) -> None:
        original = self.digest()
        for change in (
            {"jd": "Build reliable databases."},
            {"title": "Infrastructure Engineer"},
            {"company_name": "Different Systems"},
            {"evaluation_query": "Must have scaled production storage."},
            {"judge": "terra"},
        ):
            self.assertNotEqual(self.digest(**change), original)

        with mock.patch.object(capability_contract.jev_model, "PROMPT_VERSION", "revised-jev-prompt"):
            self.assertNotEqual(self.digest(), original)
        with mock.patch.object(capability_contract, "EVIDENCE_POLICY", "Revised evidence policy"):
            self.assertNotEqual(self.digest(), original)

    def test_cleaner_whitespace_normalization_and_credentials_are_ignored(self) -> None:
        original = self.digest()
        normalized = self.digest(
            jd="  Build reliable distributed systems.\n",
            title=" Backend   Engineer ",
            company_name=" Example   Systems ",
            evaluation_query="  Must have operated production storage.  ",
            judge=" JEV ",
        )
        self.assertEqual(normalized, original)
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "openai-secret", "TYPESAFE_API_KEY": "typesafe-secret"},
            clear=False,
        ):
            self.assertEqual(self.digest(), original)

    def test_prompt_spec_contains_exact_dated_rubric_and_jev_templates(self) -> None:
        spec = capability_contract.prompt_spec(judge="jev", as_of="2026-09-19")
        self.assertIn("2026-09-19", spec["rating_rubric"])
        self.assertEqual(len(spec["shared_questions"]), 20)
        self.assertEqual(
            set(spec["role_question_template"]),
            {"role_0_function", "role_0_execution", "role_0_quality"},
        )
        self.assertIn("roles[0]", spec["role_question_template"]["role_0_function"]["instructions"])
        self.assertEqual(len(spec["model_asset_sha256"]), 64)

        terra_spec = capability_contract.prompt_spec(judge="terra", as_of="2026-09-20")
        self.assertIn("2026-09-20", terra_spec["rating_rubric"])
        self.assertNotIn("shared_questions", terra_spec)


if __name__ == "__main__":
    unittest.main()
