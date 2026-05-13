import importlib.util
import inspect
import json
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

    def test_orchestrator_runs_prefilters_before_role_search(self):
        src = inspect.getsource(search.run_pipeline)
        self.assertIn('"apply_prefilters"', src)
        self.assertLess(src.index('"apply_prefilters"'), src.index('"execute_role_search"'))

    def test_search_block_contract_persists_current_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "pipeline.json"
            ledger = search.load_ledger(lp)
            payload = {"state": "state.json", "model": "x", "mode": "filter_rerank"}
            with self.assertRaises(search.Blocked):
                search.block(lp, ledger, SimpleNamespace(), "llm", "llm_filter_rerank", payload, "Run LLM?")
            saved = search.read_json(lp)

        block = saved["current_block"]
        self.assertEqual(block["status"], "blocked_approval")
        self.assertEqual(block["approval_type"], "llm")
        self.assertIn("approval_id", block)
        self.assertEqual(block["ledger"], str(lp))
        self.assertIn("continue_command", block)

    def test_search_status_reports_current_block_and_step_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "pipeline.json"
            search.write_json(lp, {
                "current_block": {"status": "blocked_approval", "approval_id": "llm_abc"},
                "steps": {
                    "init_state": {"status": "completed"},
                    "llm_filter_rerank": {"status": "blocked_approval"},
                },
            })
            args = SimpleNamespace(ledger=str(lp), state=None)
            rc = search.cmd_status(args)

        self.assertEqual(rc, 0)

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

    def test_sales_plan_normalization_supports_multi_query_and_strips_metadata(self):
        raw = {
            "score_criteria": "investment team",
            "queries": [
                {"id": "finance", "args": {"company_ids": [123], "company_names": {"123": "Acme"}, "function_ids": ["10"]}},
                {"id": "past_company", "label": "past company", "past_company_ids": [123], "past_company_names": {"123": "Acme"}},
                {"id": "keyword_last", "label": "keyword", "args": {"keywords": "Acme"}},
            ],
        }
        plan, criteria = sales.normalize_search_plan(raw, set_id="set-123", conversation_id="conv-123", default_count=25)
        self.assertEqual(criteria, "investment team")
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]["args"]["function_ids"], ["10"])
        self.assertEqual(plan[0]["args"]["company_names"], {"123": "Acme"})
        self.assertEqual(plan[1]["args"]["past_company_ids"], [123])
        self.assertEqual(plan[1]["args"]["past_company_names"], {"123": "Acme"})
        self.assertEqual(plan[2]["args"]["keywords"], "Acme")
        self.assertEqual(plan[0]["args"]["set_id"], "set-123")
        self.assertEqual(plan[0]["args"]["conversation_id"], "conv-123")
        self.assertTrue(plan[0]["args"]["persist_artifact"])
        self.assertNotIn("label", plan[1]["args"])

    def test_sales_member_ids_for_enrichment_filters_current_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leads = root / "leads.jsonl"
            rows = [
                {"member_id": "1", "artifact_id": "art-a", "mutual_count": 1, "enriched": False},
                {"member_id": "2", "artifact_id": "art-a", "mutual_count": 5, "enriched": False},
                {"member_id": "3", "artifact_id": "art-b", "mutual_count": 9, "enriched": False},
                {"member_id": "4", "artifact_id": "art-a", "mutual_count": 10, "enriched": True},
            ]
            leads.write_text("".join(json.dumps(row) + "\n" for row in rows))
            state = root / "state.json"
            sales.write_json(state, {"files": {"leads_jsonl": str(leads)}})
            self.assertEqual(sales.member_ids_for_enrichment(state, artifact_id="art-a", limit=10), [2, 1])

if __name__ == "__main__":
    unittest.main()
