import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

search = load("search_network_pipeline", "packs/search/primitives/search_network_pipeline/search_network_pipeline.py")
sales = load("sales_nav_pipeline", "packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py")

class SearchNetworkPipelineTests(unittest.TestCase):
    def test_search_approval_id_stable(self):
        p = {"state": "s.json", "model": "x", "mode": "filter_rerank"}
        self.assertEqual(search.approval_id("llm", p), search.approval_id("llm", dict(reversed(list(p.items())))))

    def test_parse_multiple_jsons(self):
        self.assertEqual(search.parse_jsons('{"a":1}\n{"b":2}')[-1]["b"], 2)

class SalesNavPipelineTests(unittest.TestCase):
    def test_sales_block_tool_call_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "pipeline.json"
            ledger = sales.load(lp)
            rc = sales.block_tool_call(
                lp,
                ledger,
                "sales_nav_search",
                {"set_id": "set_123"},
                str(Path(tmp) / "response.json"),
                "python continue",
                "Call tool",
            )
            saved = sales.read_json(lp)
        self.assertEqual(rc, 30)
        block = saved["current_block"]
        self.assertEqual(block["status"], "blocked_tool_call")
        self.assertEqual(block["tool_server"], "powerset-search")
        self.assertEqual(block["tool_name"], "sales_nav_search")
        self.assertEqual(block["tool_args"]["set_id"], "set_123")
        self.assertIn("save_response_to", block)

    def test_sales_ledger_path_uses_state_when_present(self):
        args = SimpleNamespace(ledger=None, state="/tmp/run/state.json", run_id=None, query=None)
        self.assertEqual(str(sales.ledger_path(args)), "/tmp/run/state.json.pipeline.json")

    def test_sales_approve_current_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "pipeline.json"
            sales.write_json(lp, {"current_block": {"approval_id": "llm_abc", "payload": {}}, "approvals": {}, "steps": {}})
            args = SimpleNamespace(ledger=str(lp), state=None, run_id=None, query=None, kind="llm", approval_id=None, confirm=True)
            rc = sales.cmd_approve(args)
            saved = sales.read_json(lp)
        self.assertEqual(rc, 0)
        self.assertTrue(saved["approvals"]["llm_abc"]["confirmed"])

if __name__ == "__main__":
    unittest.main()
