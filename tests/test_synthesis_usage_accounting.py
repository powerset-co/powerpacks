from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    OpenAIResponsesConfig,
)
from packs.ingestion.primitives.deep_context.synthesis import normalization, runner
from packs.ingestion.primitives.deep_context.synthesis.models import JevUsage, SynthesisPlan, SynthesisTally
from packs.ingestion.primitives.deep_context.synthesis import synthesize_person_context as synthesis


class SynthesisUsageAccountingTest(unittest.TestCase):
    def test_report_bills_raw_output_once_and_retains_reasoning_detail(self) -> None:
        for reasoning_tokens in (0, 1024):
            with self.subTest(reasoning_tokens=reasoning_tokens), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = Db(root / "deep-context.sqlite")
                response = SimpleNamespace(usage=SimpleNamespace(
                    input_tokens=75,
                    output_tokens=1186,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
                ))
                usage = OpenAIResponsesCaller._usage(response).as_dict()
                config = OpenAIResponsesConfig("gpt-6-luna", "medium", 1, 120, 0)
                with mock.patch.object(OpenAIResponsesConfig, "resolve", return_value=config):
                    node = synthesis.SynthesizePersonContext(db=database, out_dir=root / "facts")
                with (
                    mock.patch.object(node, "_migrate_parent_cache", return_value=SynthesisPlan("", ())),
                    mock.patch.object(runner, "run_paid", return_value=SynthesisTally(tokens=usage)),
                    mock.patch.object(runner, "tag_saved_facts", return_value=JevUsage(cost_usd=0.007)),
                    mock.patch.object(normalization, "normalize_parent_cache"),
                    mock.patch.object(synthesis, "estimate_cost_usd", return_value=0.25) as cost,
                ):
                    manifest = node.execute()

                cost.assert_called_once_with(75, 1186, "gpt-6-luna")
                self.assertEqual(manifest.tokens, usage)
                self.assertEqual(manifest.estimated_cost_usd, 0.257)


if __name__ == "__main__":
    unittest.main()
