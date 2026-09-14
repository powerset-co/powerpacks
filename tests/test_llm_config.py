"""Shared LLM configuration and spend-estimate regressions."""

from __future__ import annotations

import unittest
from unittest import mock

from packs.indexing.lib.openai_responses import estimate_cost_usd


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


if __name__ == "__main__":
    unittest.main()
