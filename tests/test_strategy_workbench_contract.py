"""Contract tests for reviewed strategy-workbench inputs."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETWORK_PIPELINE = (
    ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
)


def load_network_module():
    spec = importlib.util.spec_from_file_location(
        "strategy_workbench_network_pipeline", NETWORK_PIPELINE
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StrategyWorkbenchPipelineContractTests(unittest.TestCase):
    def test_model_defaults_are_luna_filter_none_rerank_and_expand_medium(self) -> None:
        network = load_network_module()

        prepared = network.build_parser().parse_args(["prepare", "--query", "retrieval probe"])
        executed = network.build_parser().parse_args(
            ["run", "--query", "retrieval probe", "--payload-json", "payload.json"]
        )

        self.assertEqual(prepared.expand_model, "gpt-5.6-luna")
        self.assertEqual(prepared.expand_reasoning_effort, "medium")
        self.assertEqual(executed.filter_model, "gpt-5.6-luna")
        self.assertEqual(executed.filter_reasoning_effort, "none")
        self.assertEqual(executed.model, "gpt-5.6-luna")
        self.assertEqual(executed.reasoning_effort, "medium")

    def test_review_contract_keeps_retrieval_and_evaluation_inputs_distinct(self) -> None:
        network = load_network_module()
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            filter_prompt = tmp / "filter.txt"
            rerank_prompt = tmp / "rerank.txt"
            traits = tmp / "traits.json"
            filter_prompt.write_text("filter reviewed prompt", encoding="utf-8")
            rerank_prompt.write_text("rerank reviewed prompt", encoding="utf-8")
            traits.write_text(json.dumps([{"value": "distributed systems"}]), encoding="utf-8")

            args = network.build_parser().parse_args(
                [
                    "prepare",
                    "--query",
                    "wide retrieval probe",
                    "--evaluation-query",
                    "canonical approved plan",
                    "--evaluation-traits-json",
                    f"@{traits}",
                    "--filter-system-file",
                    str(filter_prompt),
                    "--rerank-system-file",
                    str(rerank_prompt),
                ]
            )
            suffix = network.execution_contract_suffix(args)

        self.assertEqual(args.query, "wide retrieval probe")
        self.assertEqual(args.evaluation_query, "canonical approved plan")
        self.assertIn("--evaluation-query", suffix)
        self.assertIn("canonical approved plan", suffix)
        self.assertIn("distributed systems", suffix)
        self.assertIn(str(filter_prompt.resolve()), suffix)
        self.assertIn(str(rerank_prompt.resolve()), suffix)

    def test_payload_is_bound_byte_for_byte_and_cannot_change_on_same_ledger(self) -> None:
        network = load_network_module()
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "edited-payload.json"
            ledger_path = tmp / "pipeline.ledger.json"
            raw = b'{\n  "normalized_query": "edited by operator",\n  "role_search_filters": {}\n}\n'
            source.write_bytes(raw)
            ledger = network.load_ledger(ledger_path)
            args = Namespace(payload_json=str(source))

            network.bind_execution_payload(args, ledger_path, ledger)

            snapshot = Path(args.payload_json)
            self.assertEqual(snapshot.read_bytes(), raw)
            self.assertEqual(ledger["execution_payload"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(ledger["artifacts"]["execution_payload_json"], str(snapshot))

            source.write_text('{"normalized_query":"different"}\n', encoding="utf-8")
            args.payload_json = str(source)
            with self.assertRaises(network.Failed):
                network.bind_execution_payload(args, ledger_path, ledger)


if __name__ == "__main__":
    unittest.main()
