import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sales = load("sales_nav_pipeline", "packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py")


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
            sales.write_json(
                lp, {"current_block": {"approval_id": "llm_abc", "payload": {}}, "approvals": {}, "steps": {}}
            )
            args = SimpleNamespace(
                ledger=str(lp), state=None, run_id=None, query=None, kind="llm", approval_id=None, confirm=True
            )
            rc = sales.cmd_approve(args)
            saved = sales.read_json(lp)
        self.assertEqual(rc, 0)
        self.assertTrue(saved["approvals"]["llm_abc"]["confirmed"])

    def test_sales_plan_normalization_supports_multi_query_and_strips_metadata(self):
        raw = {
            "score_criteria": "investment team",
            "queries": [
                {
                    "id": "finance",
                    "args": {"company_ids": [123], "company_names": {"123": "Acme"}, "function_ids": ["10"]},
                },
                {
                    "id": "past_company",
                    "label": "past company",
                    "past_company_ids": [123],
                    "past_company_names": {"123": "Acme"},
                },
                {"id": "keyword_last", "label": "keyword", "args": {"keywords": "Acme"}},
            ],
        }
        plan, criteria = sales.normalize_search_plan(
            raw, set_id="set-123", conversation_id="conv-123", default_count=25
        )
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

    def test_sales_mutual_attribution_uses_repo_env_without_cli_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "pipeline.json"
            state = root / "state.json"
            sales.write_json(ledger_path, {})
            sales.write_json(state, {"set_id": "set-123"})
            args = SimpleNamespace(
                force=False,
                discover_mutuals=False,
                discover_stagger=None,
                discover_max_leads=None,
            )
            with mock.patch.object(
                sales,
                "run",
                return_value={"returncode": 0, "json": {"status": "completed"}},
            ) as run_mock:
                sales.enrich_mutual_attribution_step(args, ledger_path, sales.load(ledger_path), state)

        cmd = run_mock.call_args.args[0]
        self.assertIn("--env-file", cmd)
        self.assertEqual(cmd[cmd.index("--env-file") + 1], str(sales.ROOT / ".env"))


if __name__ == "__main__":
    unittest.main()
