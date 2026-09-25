"""Shared LLM configuration and spend-estimate regressions."""

from __future__ import annotations

import unittest
from unittest import mock

from packs.indexing.lib.llm_config import DEFAULT_SYNTHESIS_MODEL, is_reasoning_model
from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import build_parser


class LlmPricingTest(unittest.TestCase):
    def test_luna_is_priced_for_default_and_flex_spend_gates(self) -> None:
        with mock.patch.dict(
            "os.environ", {"POWERPACKS_OPENAI_SERVICE_TIER": "default"}, clear=False,
        ):
            self.assertEqual(estimate_cost_usd(1_000_000, 1_000_000, "gpt-5.6-luna"), 7.0)
        with mock.patch.dict(
            "os.environ", {"POWERPACKS_OPENAI_SERVICE_TIER": "flex"}, clear=False,
        ):
            self.assertEqual(estimate_cost_usd(1_000_000, 1_000_000, "gpt-5.6-luna"), 3.5)

    def test_gpt_6_luna_is_priced_and_is_the_synthesis_default(self) -> None:
        with mock.patch.dict(
            "os.environ", {"POWERPACKS_OPENAI_SERVICE_TIER": "default"}, clear=False,
        ):
            self.assertEqual(estimate_cost_usd(1_000_000, 1_000_000, "gpt-6-luna"), 0.6)
        self.assertTrue(is_reasoning_model("gpt-6-luna"))
        self.assertEqual(DEFAULT_SYNTHESIS_MODEL, "gpt-6-luna")
        self.assertEqual(build_parser().parse_args([]).model, "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
