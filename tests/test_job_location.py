"""Keep both Listen Labs Product locations through planning, queries, and review."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import build_eval_inputs, decompose_jd, search_harness
from tests import test_fetch_job_posting as fetch_tests
from tests.test_search_harness import _payload, _start

METROS = ["San Francisco Bay Area", "New York Metropolitan Area"]


def _plan():
    return build_eval_inputs.plan_from_obj(
        {"job_title": "Member of Technical Staff, Product", "location": "SF or NYC",
         "location_filters": {"cities": ["San Francisco", "New York"], "countries": ["United States"]}},
        {"traits": []}, set_name="Synthetic team", set_id="set-1",
        source_url=None, created_at="2026-09-08T00:00:00Z",
    )


class JobLocationTests(unittest.TestCase):
    def test_posting_locations_reach_planner_and_review(self):
        job = json.loads(fetch_tests.FIXTURE.read_text())
        jd, source = fetch_tests.TestAshbyJobPosting()._fetch(job)
        messages = build_eval_inputs.build_plan_messages(jd, source_metadata=source)
        self.assertIn("Preserve every explicitly allowed office as an OR alternative", messages[0]["content"])
        for location in ("San Francisco, CA", "New York, NY"):
            self.assertIn(location, messages[1]["content"])
        plan = _plan()
        del plan["filters"]  # Optional in the published plan schema.
        self.assertEqual(plan["search_scope"]["filters"], {"metro_areas": METROS})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            plan_path = run_dir / "epoch0/plan.json"
            plan_path.parent.mkdir()
            plan_path.write_text(json.dumps(plan))
            queries = run_dir / "queries.json"
            queries.write_text(json.dumps([{"key": "literal_search", "query": "Software Engineer"}]))
            (run_dir / "network_floors.json").write_text('{"floors": []}')
            result = search_harness.prepare_review(
                SimpleNamespace(), run_dir, plan_path, queries,
                resolve_identity=mock.Mock(), probe_floors=mock.Mock())
        self.assertEqual(result["search_scope"]["location"], " or ".join(METROS))
        self.assertEqual(result["filters"], [])

    def test_query_and_brief_keep_both_allowed_metros(self):
        plan = _plan()
        client = mock.Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"seeds":["Software Engineer with AI experience"]}'))])
        queries = decompose_jd.generate_queries(
            jd="Build software", plan=plan, model="test", client=client, use_precedents=False)
        self.assertEqual(queries[0]["query"], "Software Engineer with AI experience in " + " or ".join(METROS))
        self.assertEqual(search_harness.build_initial_results(plan, queries)["brief"]["geography"],
                         " or ".join(METROS))

    def test_compiler_keeps_reviewed_locations_when_model_narrows_or_drops_them(self):
        for compiled in ({"metro_areas": METROS[:1]}, {}):
            with self.subTest(compiled=compiled), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                _start(run_dir)
                (run_dir / "epoch0/plan.json").write_text(json.dumps(_plan()))
                search_harness.update_pending_query(run_dir=run_dir, query="Software Engineer worldwide")
                payload = _payload()
                payload["role_search_filters"].update(compiled)
                expanded = run_dir / "expanded.json"
                expanded.write_text(json.dumps(payload))
                env = run_dir / "test.env"
                env.write_text("")
                with (mock.patch.object(search_harness, "_run_command", return_value={"payload_json": str(expanded)}),
                      mock.patch.object(search_harness, "_ensure_hiring_company_context"),
                      mock.patch.object(search_harness, "_llm_pattern_defaults",
                                        side_effect=lambda **kw: (kw["payload"], []))):
                    search_harness.compile_pond(run_dir=run_dir, env_file=str(env))
                saved = json.loads((run_dir / "results.json").read_text())
                self.assertEqual(saved["pending_payload"]["payload"]["role_search_filters"]["metro_areas"], METROS)


if __name__ == "__main__":
    unittest.main()
