import importlib.util
import inspect
import json
import shlex
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

    def test_search_block_continuation_keeps_evaluation_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lp = root / "pipeline.json"
            filter_prompt = root / "filter.txt"
            rerank_prompt = root / "rerank.txt"
            filter_prompt.write_text("filter prompt", encoding="utf-8")
            rerank_prompt.write_text("rerank prompt", encoding="utf-8")
            args = SimpleNamespace(
                evaluation_query="canonical approved plan",
                evaluation_traits_json=json.dumps([{"value": "distributed systems"}]),
                filter_system_file=str(filter_prompt),
                rerank_system_file=str(rerank_prompt),
            )
            ledger = search.load_ledger(lp)
            payload = {"state": "state.json", "model": "x", "mode": "filter_rerank"}
            with self.assertRaises(search.Blocked):
                search.block(lp, ledger, args, "llm", "llm_filter_rerank", payload, "Run LLM?")
            command = search.read_json(lp)["current_block"]["continue_command"]

        continuation = shlex.split(command.split(" && ", 1)[1])
        self.assertEqual(continuation[continuation.index("--evaluation-query") + 1],
                         "canonical approved plan")
        self.assertEqual(json.loads(continuation[continuation.index("--evaluation-traits-json") + 1]),
                         [{"value": "distributed systems"}])
        self.assertEqual(continuation[continuation.index("--filter-system-file") + 1],
                         str(filter_prompt.resolve()))
        self.assertEqual(continuation[continuation.index("--rerank-system-file") + 1],
                         str(rerank_prompt.resolve()))

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

    def test_query_payload_resumes_existing_ledger_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lp = root / "pipeline.json"
            old_state = root / "old-state.json"
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

            with mock.patch.object(search, "run") as run:
                state = search.init_state(args, lp, search.load_ledger(lp))
                run.assert_not_called()

            saved = search.read_json(lp)
            self.assertEqual(state, old_state)
            self.assertEqual(saved["state"], str(old_state))

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
        self.assertEqual("gpt-5.6-luna", args.filter_model)
        self.assertEqual("none", args.filter_reasoning_effort)
        self.assertEqual("gpt-5.6-luna", args.model)
        self.assertEqual("medium", args.reasoning_effort)

    def test_llm_approval_message_sets_time_expectation(self):
        src = inspect.getsource(search.run_pipeline)

        self.assertIn("usually takes 2-3 minutes", src)

    def test_orchestrator_passes_model_to_reranker(self):
        src = inspect.getsource(search.run_pipeline)

        self.assertIn('"--model",args.model', src)
        self.assertIn('"--model",args.filter_model', src)
        self.assertIn('"--reasoning-effort",args.filter_reasoning_effort', src)
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

    def test_compact_previews_carry_payload_traits(self):
        traits = [
            {"meaning": "role", "temporal": "current", "value": "VP of Finance"},
            {"meaning": "industry", "temporal": "past", "value": "Healthcare"},
        ]
        for payload in [{"traits": traits}, {}, {"traits": []}, {"traits": None}]:
            with self.subTest(payload=payload):
                preview = search.compact_preview(payload, Path("payload.json"), [])
                self.assertEqual(preview["traits"], payload.get("traits") or [])
                with mock.patch.object(search, "local_pool_estimate", return_value={}):
                    local_preview = search.compact_preview_local(
                        payload, Path("payload.json"), Path("db.duckdb"), []
                    )
                self.assertEqual(local_preview["traits"], payload.get("traits") or [])

    def test_compact_previews_hide_current_filters_only_with_traits(self):
        filters = {"is_current_role": True, "is_current_company": False, "countries": ["US"]}
        for traits in [[{"meaning": "role", "temporal": "current", "value": "Engineer"}], [], None]:
            payload = {"role_search_filters": filters, "traits": traits}
            with self.subTest(traits=traits):
                preview = search.compact_preview(payload, Path("payload.json"), [])
                with mock.patch.object(search, "local_pool_estimate", return_value={}):
                    local_preview = search.compact_preview_local(
                        payload, Path("payload.json"), Path("db.duckdb"), []
                    )
                expected = {"countries": ["US"]} if traits else filters
                self.assertEqual(preview["filters"], expected)
                self.assertEqual(local_preview["filters"], expected)

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
                expand_model=search.DEFAULT_EXPAND_MODEL,
                expand_reasoning_effort=search.DEFAULT_EXPAND_REASONING_EFFORT,
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

    def test_cmd_prepare_company_only_emits_preview_and_execute_command(self):
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
                "has_domain_intent": False,
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
                expand_model=search.DEFAULT_EXPAND_MODEL,
                expand_reasoning_effort=search.DEFAULT_EXPAND_REASONING_EFFORT,
            )

            rc = search.cmd_prepare(args)

        self.assertEqual(rc, 0)
        out = emitted[0]
        self.assertEqual(out["status"], "preview_ready")
        self.assertEqual(out["quality_issues"], [])
        self.assertEqual(out["preview"]["filters"]["company_names"], ["OpenAI"])
        self.assertIn("--execute-approved", out["execute_command"])

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
    def test_sales_backend_is_cloud_with_a_set_and_local_without(self):
        self.assertEqual(sales.backend_of({"artifacts": {}}), "local")
        self.assertEqual(sales.backend_of({"artifacts": {"set_id": ""}}), "local")
        self.assertEqual(sales.backend_of({"artifacts": {"set_id": "set-123"}}), "cloud")

    def test_sales_local_search_maps_args_and_answers_in_the_api_shape(self):
        lead = {"first_name": "Ada", "last_name": "L", "title": "CTO", "company_name": "Stripe", "location": "SF",
                "member_id": 74438553, "profile_id": "ACwAAA", "degree": 2, "total_mutual_count": 3,
                "mutual_member_ids": [1, 2, 3], "mutual_intro_text": "You both know Sam", "profile_picture_url": None}
        with mock.patch.object(sales.li, "sales_nav_searches", return_value=[{"total": 430, "leads": [lead], "requests_today": 7, "day_cap": 100}]) as searches, \
             mock.patch.object(sales, "local_title_ids", return_value=[{"id": 8, "text": "Chief Technology Officer"}]):
            out, = sales.local_searches([{"company_ids": [2135371], "company_names": {"2135371": "Stripe"}, "title": "CTO",
                                          "seniority_ids": ["310"], "past_company_ids": [1], "count": 25, "start_offset": 0}])
        namespace = searches.call_args.args[0][0]
        self.assertEqual((namespace.company_id, namespace.company_name, namespace.title_id, namespace.title_name, namespace.seniority),
                         ([2135371], ["Stripe"], [8], ["Chief Technology Officer"], ["310"]))
        row = out["leads"][0]
        self.assertEqual((row["name"], row["company"], row["mutual_count"], row["mutual_member_ids"], row["intro_text"]),
                         ("Ada L", "Stripe", 3, [1, 2, 3], "You both know Sam"))
        self.assertEqual((out["total_count"], out["has_more"], out["next_start_offset"], out["unsupported_filters"], out["backend"]),
                         (430, True, 1, ["past_company_ids"], "local"))

    def test_sales_search_plan_appends_new_ids_to_an_existing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lp = root / "pipeline.json"
            ledger = sales.load(lp)
            ledger["artifacts"].update({"set_id": "set-1", "conversation_id": "conv-1"})
            state = root / "state.json"
            sales.write_json(state, {"counts": {"leads": 0}})
            plan_a = root / "a.json"
            plan_a.write_text(json.dumps({"queries": [{"id": "company_only", "args": {"company_ids": [1]}}]}))
            args = SimpleNamespace(search_plan_json=str(plan_a), search_args_json=None, set_id=None, conversation_id=None, count=25)
            first = sales.search_plan(args, lp, ledger, state)
            plan_b = root / "b.json"
            plan_b.write_text(json.dumps({"queries": [{"id": "company_only", "args": {"company_ids": [1]}},
                                                      {"id": "cxo", "args": {"company_ids": [1], "seniority_ids": ["310", "320"]}}]}))
            args.search_plan_json = str(plan_b)
            second = sales.search_plan(args, lp, ledger, state)
            again = sales.search_plan(SimpleNamespace(search_plan_json=None, search_args_json=None, set_id=None, conversation_id=None, count=25), lp, ledger, state)
        self.assertEqual([item["id"] for item in first], ["company_only"])
        self.assertEqual([item["id"] for item in second], ["company_only", "cxo"])
        self.assertEqual(again, second)
        self.assertEqual(second[1]["args"]["set_id"], "set-1")

    def test_sales_run_searches_calls_the_backend_once_and_records_a_summary_per_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lp = root / "pipeline.json"
            ledger = sales.load(lp)
            ledger["artifacts"].update({"set_id": "set-1", "conversation_id": "conv-1"})
            state = root / "state.json"
            sales.write_json(state, {"counts": {"leads": 0}})
            plan = root / "plan.json"
            plan.write_text(json.dumps({"queries": [
                {"id": "pool", "label": "company only", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}}},
                {"id": "cxo", "label": "executives", "args": {"company_ids": [2135371], "seniority_ids": ["310"]}},
            ]}))
            args = SimpleNamespace(search_plan_json=str(plan), search_args_json=None, set_id=None, conversation_id=None, count=25)
            leads = [{"name": "Ada L", "title": "CTO", "company": "Stripe", "mutual_count": 2, "member_id": 1}]
            answers = [{"leads": leads, "total_count": 180, "max_total_count": 180, "has_more": True, "next_start_offset": 50},
                       {"leads": [], "total_count": 0, "max_total_count": 0, "has_more": False, "next_start_offset": None,
                        "refinement_hint": "try keywords"}]
            with mock.patch.object(sales, "cloud_searches", return_value=answers) as cloud, \
                 mock.patch.object(sales, "ingest_page", side_effect=[{"leads_ingested": 1, "lead_count": 1}, {"leads_ingested": 0, "lead_count": 1}]):
                sales.run_searches(args, lp, ledger, state)
            saved = sales.read_json(lp)
            pages = sorted(path.name for path in (root / "pages").iterdir())
        self.assertEqual(cloud.call_count, 1)
        self.assertEqual([a["set_id"] for a in cloud.call_args.args[0]], ["set-1", "set-1"])
        self.assertEqual(pages, ["sales-nav-search-000.response.json", "sales-nav-search-001.response.json"])
        summaries = sales.search_summaries(saved)
        self.assertEqual([s["label"] for s in summaries], ["company only", "executives"])
        self.assertEqual(summaries[0]["filters_description"], "company=Stripe")
        self.assertEqual((summaries[0]["total_count"], summaries[0]["new_leads"], summaries[0]["lead_summaries"][0]["name"]), (180, 1, "Ada L"))
        self.assertEqual((summaries[1]["filters_description"], summaries[1]["refinement_hint"]), ("company=2135371; seniority=CXO", "try keywords"))

    def test_sales_local_run_skips_enrich_and_member_urls_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lp = root / "pipeline.json"
            ledger = sales.load(lp)
            state = root / "state.json"
            sales.write_json(state, {})
            args = SimpleNamespace(skip_enrich=False, enrich_limit=10, skip_mutual_url_resolution=False, mutual_url_limit=10, resolve_mutuals_external=False)
            with mock.patch.object(sales, "cloud_call") as cloud:
                sales.enrich_profiles(args, lp, ledger, state)
                sales.resolve_mutual_urls(args, lp, ledger, state)
        cloud.assert_not_called()
        self.assertIn("local session", ledger["steps"]["enrich_profiles"]["summary"]["reason"])
        self.assertIn("local session", ledger["steps"]["resolve_mutual_urls"]["summary"]["reason"])

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
        self.assertNotIn("persist_artifact", plan[0]["args"])
        self.assertNotIn("label", plan[1]["args"])

    def test_sales_member_ids_for_enrichment_skips_enriched_and_orders_by_mutuals(self):
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
            self.assertEqual(sales.member_ids_for_enrichment(state, limit=10), [3, 2, 1])
            self.assertEqual(sales.member_ids_for_enrichment(state, limit=2), [3, 2])

    def test_sales_mutual_attribution_uses_repo_env_without_cli_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "pipeline.json"
            state = root / "state.json"
            sales.write_json(ledger_path, {"artifacts": {"set_id": "set-123"}})
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
