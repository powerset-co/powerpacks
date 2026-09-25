from __future__ import annotations

import os
import unittest
from unittest import mock

from packs.search.primitives.llm_rerank_candidates import capability_contract
from packs.search.primitives.llm_rerank_candidates.jev.questions import build_request


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
        with mock.patch.object(capability_contract, "REQUEST_VERSION", "revised-jev-request"):
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

    def test_prompt_spec_carries_the_exact_jev_questions_and_dated_terra_rubric(self) -> None:
        spec = capability_contract.prompt_spec(judge="jev", as_of="2026-09-19")
        self.assertNotIn("rating_rubric", spec)
        self.assertEqual(spec["questions"], build_request(
            jd="Build systems.", profile={}, as_of="2026-09-19")["questions"])
        self.assertEqual(len(spec["questions"]), 7)
        self.assertEqual(spec["request_version"], "jev-capability-request-v3-20260925")
        self.assertEqual(len(spec["model_asset_sha256"]), 64)

        terra_spec = capability_contract.prompt_spec(judge="terra", as_of="2026-09-20")
        self.assertIn("2026-09-20", terra_spec["rating_rubric"])
        self.assertNotIn("questions", terra_spec)


if __name__ == "__main__":
    unittest.main()
