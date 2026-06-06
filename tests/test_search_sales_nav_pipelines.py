import importlib.util
import inspect
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

    def test_query_payload_starts_fresh_state_even_when_ledger_has_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lp = root / "pipeline.json"
            old_state = root / "old-state.json"
            new_state = root / "new-state.json"
            payload_path = root / "expand_search_request.json"
            search.write_json(payload_path, {"role_search_filters": {"title_keywords": ["engineer"]}})
            ledger = search.load_ledger(lp)
            ledger["state"] = str(old_state)
            search.save(lp, ledger)
            args = SimpleNamespace(
                state=None,
                query="software engineers",
                payload_json=str(payload_path),
                env_file=".env",
                timeout=30,
            )

            def fake_run(cmd, **kwargs):
                if "init" in cmd:
                    return {"returncode": 0, "json": {"state": str(new_state)}, "stdout": "{}", "stderr": ""}
                if "record-step" in cmd:
                    return {"returncode": 0, "json": {"status": "ok"}, "stdout": "{}", "stderr": ""}
                raise AssertionError(f"unexpected command: {cmd}")

            with mock.patch.object(search, "run", side_effect=fake_run):
                state = search.init_state(args, lp, search.load_ledger(lp))

            saved = search.read_json(lp)
            self.assertEqual(state, new_state)
            self.assertEqual(saved["state"], str(new_state))
            self.assertNotEqual(saved["state"], str(old_state))

    def test_rerank_concurrency_default_comes_from_module_default(self):
        parser = search.argparse.ArgumentParser()
        search.add_run(parser)
        args = parser.parse_args([])

        self.assertEqual(search.DEFAULT_RERANK_CONCURRENCY, args.rerank_concurrency)

    def test_filter_parallel_defaults_come_from_module_defaults(self):
        parser = search.argparse.ArgumentParser()
        search.add_run(parser)
        args = parser.parse_args([])

        self.assertEqual(search.DEFAULT_FILTER_BATCH_SIZE, args.filter_batch_size)
        self.assertEqual(search.DEFAULT_FILTER_CONCURRENCY, args.filter_concurrency)

    def test_llm_approval_message_sets_time_expectation(self):
        src = inspect.getsource(search.run_pipeline)

        self.assertIn("usually takes 2-3 minutes", src)

    def test_orchestrator_passes_model_to_reranker(self):
        src = inspect.getsource(search.run_pipeline)

        self.assertIn('"--model",args.model', src)
        self.assertIn('"--concurrency",str(args.filter_concurrency)', src)
        self.assertLess(src.index('"llm_filter_candidates"'), src.index('"llm_rerank_candidates"'))

    def test_prepare_helpers_strip_expand_metadata_and_build_execute_preview(self):
        expand = {
            "primitive": "expand_search_request",
            "status": "completed",
            "normalized_query": "software engineers in sf",
            "intent_type": "role_search",
            "role_search_filters": {
                "semantic_query": "Experienced software engineers who build production systems, own backend or full-stack implementation, and show evidence of technical execution in product or infrastructure teams.",
                "bm25_queries": ["software engineer"],
                "metro_areas": ["San Francisco Bay Area"],
            },
        }

        payload = search.payload_from_expand_output(expand)
        self.assertNotIn("primitive", payload)
        self.assertNotIn("status", payload)
        self.assertEqual(search.payload_quality_issues(payload), [])

        preview = search.compact_preview(payload, Path("payload.json"), [])
        self.assertEqual(preview["payload_json"], "payload.json")
        self.assertIn("semantic_query", preview["role_title_intent"])
        self.assertEqual(preview["filters"]["metro_areas"], ["San Francisco Bay Area"])

    def test_prepare_quality_gate_rejects_short_title_semantic_query(self):
        payload = {
            "role_search_filters": {
                "semantic_query": "software engineer",
                "bm25_queries": ["software engineer"],
            }
        }

        issues = search.payload_quality_issues(payload)

        self.assertTrue(issues)
        self.assertIn("semantic_query", issues[0])

    def test_prepare_quality_gate_allows_filter_only_search(self):
        payload = {
            "role_search_filters": {
                "company_names": ["Meta"],
                "position_after_date": "2020-01-01",
            }
        }

        self.assertEqual(search.payload_quality_issues(payload), [])

    def test_company_directory_fast_path_detects_company_only_payload(self):
        payload = {
            "role_search_filters": {
                "company_names": ["OpenAI"],
                "is_current_company": True,
            }
        }

        self.assertEqual(
            search.company_directory_tool_args(payload),
            {"company_name": "OpenAI", "page": 0, "page_size": 50, "company_limit": 5},
        )

    def test_company_directory_fast_path_ignores_role_constrained_payload(self):
        payload = {
            "role_search_filters": {
                "company_names": ["OpenAI"],
                "bm25_queries": ["software engineer"],
            }
        }

        self.assertIsNone(search.company_directory_tool_args(payload))

    def test_cmd_prepare_invokes_expand_and_emits_execute_command_without_openai(self):
        expand = {
            "primitive": "expand_search_request",
            "status": "completed",
            "normalized_query": "software engineers in sf",
            "intent_type": "role_search",
            "source_type": "query",
            "vertical": "people_by_role",
            "role_search_filters": {
                "semantic_query": "Experienced software engineers who build production systems, own backend or full-stack implementation, and show evidence of technical execution in product or infrastructure teams.",
                "bm25_queries": ["software engineer"],
                "metro_areas": ["San Francisco Bay Area"],
            },
            "notes": [],
        }
        emitted = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(search, "run", return_value={"returncode": 0, "json": expand}) as run_mock, \
             mock.patch.object(search, "emit", side_effect=emitted.append):
            args = SimpleNamespace(
                query="software engineers in sf",
                output_dir=tmp,
                env_file=".env",
                timeout=60,
                model=None,
            )

            rc = search.cmd_prepare(args)

            self.assertEqual(rc, 0)
            run_mock.assert_called_once()
            self.assertTrue((Path(tmp) / "expand_search_request.json").exists())
            self.assertTrue((Path(tmp) / "expand_search_request.full.json").exists())

        self.assertEqual(len(emitted), 1)
        out = emitted[0]
        self.assertEqual(out["status"], "preview_ready")
        self.assertEqual(out["quality_issues"], [])
        self.assertIn("execute_command", out)
        self.assertIn("--execute-approved", out["execute_command"])
        self.assertIn("pipeline.ledger.json", out["execute_command"])

    def test_cmd_prepare_emits_company_directory_fast_path_without_execute_command(self):
        expand = {
            "primitive": "expand_search_request",
            "status": "completed",
            "normalized_query": "people who work at OpenAI",
            "intent_type": "role_search",
            "source_type": "query",
            "vertical": "people_by_role",
            "role_search_filters": {
                "company_names": ["OpenAI"],
                "is_current_company": True,
            },
            "notes": [],
        }
        emitted = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(search, "run", return_value={"returncode": 0, "json": expand}), \
             mock.patch.object(search, "emit", side_effect=emitted.append):
            args = SimpleNamespace(
                query="people who work at OpenAI",
                output_dir=tmp,
                env_file=".env",
                timeout=60,
                model=None,
            )

            rc = search.cmd_prepare(args)

        self.assertEqual(rc, 0)
        out = emitted[0]
        self.assertEqual(out["status"], "company_directory_fast_path")
        self.assertEqual(out["tool"], "list_company_people")
        self.assertEqual(out["tool_args"]["company_name"], "OpenAI")
        self.assertNotIn("execute_command", out)

    def test_cli_parser_exposes_prepare_and_existing_commands(self):
        parser = search.build_parser()

        prepare = parser.parse_args(["prepare", "--query", "software engineers in sf"])
        run = parser.parse_args(["run"])
        status = parser.parse_args(["status", "--ledger", "x.json"])
        approve = parser.parse_args(["approve", "llm", "--confirm"])

        self.assertIs(prepare.func, search.cmd_prepare)
        self.assertIs(run.func, search.cmd_run)
        self.assertIs(status.func, search.cmd_status)
        self.assertIs(approve.func, search.cmd_approve)

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
            "min_leads": 10,
            "stop_after_min_leads": True,
            "queries": [
                {"id": "finance", "args": {"company_ids": [123], "company_names": {"123": "Acme"}, "function_ids": ["10"]}},
                {"id": "past_company", "label": "past company", "required": True, "past_company_ids": [123], "past_company_names": {"123": "Acme"}},
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
        self.assertTrue(plan[1]["required"])

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

    def _base_sales_args(self, state, plan):
        return SimpleNamespace(
            ledger=None,
            state=str(state),
            query=None,
            set_id=None,
            conversation_id=None,
            run_id=None,
            search_args_json=None,
            search_plan_json=str(plan),
            response=None,
            prefer_content=False,
            enriched=False,
            require_enriched=False,
            skip_enrich=False,
            enrich_limit=100,
            artifact_limit=1000,
            skip_mutual_url_resolution=True,
            mutual_url_limit=100,
            resolve_mutuals_external=False,
            discover_mutuals=False,
            discover_stagger=None,
            discover_max_leads=None,
            count=25,
            criteria=None,
            threshold=0.7,
            confirm_llm=False,
            force=False,
            timeout=60,
        )

    def test_sales_pipeline_downloads_artifact_without_get_artifact_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            pages = root / "pages"
            pages.mkdir()
            leads = root / "leads.jsonl"
            sales.write_json(state, {"set_id": "set-123", "conversation_id": "conv-123", "files": {"leads_jsonl": str(leads)}})
            plan = root / "plan.json"
            sales.write_json(plan, {"queries": [{"args": {"title": "CFO"}}]})
            lp = sales.ledger_path(self._base_sales_args(state, plan))
            ledger = sales.load(lp)
            ledger["artifacts"].update({"state": str(state), "run_dir": str(root), "conversation_id": "conv-123", "set_id": "set-123"})
            ledger["steps"]["search_000"] = {"status": "completed", "summary": {"artifact_id": "art-1", "results_returned": 1}}
            sales.save(lp, ledger)

            calls = []
            def fake_run(cmd, timeout=300):
                calls.append(cmd)
                if "download-artifact" in cmd:
                    out = Path(cmd[cmd.index("--out") + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps({"content": {"extended_results": {"leads": [{"member_id": "1"}]}}}))
                    return {"returncode": 0, "json": {"response": "downloaded", "lead_count": 1}, "stdout": "{}", "stderr": ""}
                if "ingest-page" in cmd:
                    leads.write_text(json.dumps({"member_id": "1", "artifact_id": "art-1", "enriched": True}) + "\n")
                    return {"returncode": 0, "json": {"lead_count": 1, "new_leads_ingested": 1, "new_member_ids": ["1"]}, "stdout": "{}", "stderr": ""}
                raise AssertionError(f"unexpected command {cmd}")

            args = self._base_sales_args(state, plan)
            with mock.patch.object(sales, "run", side_effect=fake_run):
                rc = sales.advance_searches(args, lp, sales.load(lp), state)

            self.assertIsNone(rc)
            self.assertTrue(any("download-artifact" in cmd for cmd in calls))
            self.assertFalse(any("get_artifact" in cmd for cmd in calls))
            saved = sales.read_json(lp)
            self.assertEqual(saved["steps"]["get_artifact_000"]["status"], "completed")
            self.assertIn("artifact_download_seconds", saved["steps"]["get_artifact_000"]["durations"])

    def test_sales_pipeline_zero_result_skips_artifact_and_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            sales.write_json(state, {"set_id": "set-123", "conversation_id": "conv-123"})
            plan = root / "plan.json"
            sales.write_json(plan, {"queries": [{"args": {"title": "CFO"}}]})
            args = self._base_sales_args(state, plan)
            lp = sales.ledger_path(args)
            ledger = sales.load(lp)
            ledger["artifacts"].update({"state": str(state), "run_dir": str(root), "conversation_id": "conv-123", "set_id": "set-123"})
            ledger["steps"]["search_000"] = {"status": "completed", "summary": {"artifact_id": "art-1", "results_returned": 0}}
            sales.save(lp, ledger)

            rc = sales.advance_searches(args, lp, sales.load(lp), state)

            self.assertIsNone(rc)
            saved = sales.read_json(lp)
            self.assertEqual(saved["steps"]["get_artifact_000"]["summary"]["reason"], "zero_results_no_new_leads")
            self.assertEqual(saved["steps"]["enrich_profiles_000"]["summary"]["reason"], "zero_results_no_new_leads")

    def test_sales_pipeline_zero_new_leads_skips_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            leads = root / "leads.jsonl"
            sales.write_json(state, {"set_id": "set-123", "conversation_id": "conv-123", "files": {"leads_jsonl": str(leads)}})
            plan = root / "plan.json"
            sales.write_json(plan, {"queries": [{"args": {"title": "CFO"}}]})
            args = self._base_sales_args(state, plan)
            lp = sales.ledger_path(args)
            ledger = sales.load(lp)
            ledger["artifacts"].update({"state": str(state), "run_dir": str(root), "conversation_id": "conv-123", "set_id": "set-123"})
            ledger["steps"]["search_000"] = {"status": "completed", "summary": {"artifact_id": "art-1", "results_returned": 1}}
            sales.save(lp, ledger)

            def fake_run(cmd, timeout=300):
                if "download-artifact" in cmd:
                    return {"returncode": 0, "json": {"response": "downloaded"}, "stdout": "{}", "stderr": ""}
                if "ingest-page" in cmd:
                    return {"returncode": 0, "json": {"lead_count": 1, "new_leads_ingested": 0, "new_member_ids": []}, "stdout": "{}", "stderr": ""}
                raise AssertionError(f"unexpected command {cmd}")

            with mock.patch.object(sales, "run", side_effect=fake_run):
                rc = sales.advance_searches(args, lp, sales.load(lp), state)

            self.assertIsNone(rc)
            saved = sales.read_json(lp)
            self.assertEqual(saved["steps"]["enrich_profiles_000"]["summary"]["reason"], "zero_new_leads_after_ingest")

    def test_sales_pipeline_threshold_skip_respects_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            leads = root / "leads.jsonl"
            leads.write_text(json.dumps({"member_id": "1"}) + "\n")
            sales.write_json(state, {"set_id": "set-123", "conversation_id": "conv-123", "files": {"leads_jsonl": str(leads)}})
            plan = root / "plan.json"
            sales.write_json(plan, {"min_leads": 1, "queries": [{"args": {"title": "CFO"}}, {"args": {"title": "VP"}}, {"required": True, "args": {"title": "CEO"}}]})
            args = self._base_sales_args(state, plan)
            lp = sales.ledger_path(args)
            ledger = sales.load(lp)
            ledger["artifacts"].update({"state": str(state), "run_dir": str(root), "conversation_id": "conv-123", "set_id": "set-123"})
            ledger["artifacts"]["search_plan"] = sales.normalize_search_plan(sales.read_json(plan), set_id="set-123", conversation_id="conv-123", default_count=25)[0]
            ledger["artifacts"]["min_leads"] = 1
            ledger["artifacts"]["stop_after_min_leads"] = True
            for step in ("search_000", "get_artifact_000", "enrich_profiles_000", "get_artifact_after_enrich_000"):
                ledger["steps"][step] = {"status": "completed", "summary": {}}
            sales.save(lp, ledger)

            rc = sales.advance_searches(args, lp, sales.load(lp), state)

            self.assertEqual(rc, 30)
            saved = sales.read_json(lp)
            self.assertEqual(saved["steps"]["search_001"]["summary"]["reason"], "minimum_lead_threshold_met")
            self.assertEqual(saved["current_block"]["step"], "search_002")

    def test_sales_pipeline_reingests_after_enrichment_without_get_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            leads = root / "leads.jsonl"
            sales.write_json(state, {"set_id": "set-123", "conversation_id": "conv-123", "files": {"leads_jsonl": str(leads)}})
            plan = root / "plan.json"
            sales.write_json(plan, {"queries": [{"args": {"title": "CFO"}}]})
            args = self._base_sales_args(state, plan)
            lp = sales.ledger_path(args)
            ledger = sales.load(lp)
            ledger["artifacts"].update({"state": str(state), "run_dir": str(root), "conversation_id": "conv-123", "set_id": "set-123"})
            for step in ("search_000", "get_artifact_000", "enrich_profiles_000"):
                ledger["steps"][step] = {"status": "completed", "summary": {"artifact_id": "art-1", "updated_leads": 1}}
            sales.save(lp, ledger)

            calls = []
            def fake_run(cmd, timeout=300):
                calls.append(cmd)
                if "download-artifact" in cmd:
                    return {"returncode": 0, "json": {"response": "downloaded"}, "stdout": "{}", "stderr": ""}
                if "ingest-page" in cmd:
                    return {"returncode": 0, "json": {"lead_count": 1, "new_leads_ingested": 0}, "stdout": "{}", "stderr": ""}
                raise AssertionError(f"unexpected command {cmd}")

            with mock.patch.object(sales, "run", side_effect=fake_run):
                rc = sales.advance_searches(args, lp, sales.load(lp), state)

            self.assertIsNone(rc)
            self.assertTrue(any("download-artifact" in cmd for cmd in calls))
            self.assertFalse(any("get_artifact" in cmd for cmd in calls))

    def test_sales_export_records_timing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            lp = root / "pipeline.json"
            sales.write_json(state, {})
            ledger = sales.load(lp)
            args = self._base_sales_args(state, root / "plan.json")
            args.force = False

            with mock.patch.object(
                sales,
                "run",
                return_value={"returncode": 0, "json": {"leads_csv": "leads.csv", "mutuals_csv": "mutuals.csv"}, "stdout": "{}", "stderr": ""},
            ):
                sales.export_state(args, lp, ledger, state)

            saved = sales.read_json(lp)
            rec = saved["steps"]["export"]
            self.assertEqual(rec["status"], "completed")
            self.assertIn("export_started_at", rec)
            self.assertIn("export_completed_at", rec)
            self.assertIn("export_seconds", rec["durations"])

if __name__ == "__main__":
    unittest.main()
