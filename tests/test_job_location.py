"""JD queries own geographic filters through review and execution."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import decompose_jd, search_harness
from packs.search.primitives.expand_search_request import parallel_extractors
from tests.test_search_harness import _payload, _start

METROS = ["San Francisco Bay Area", "New York Metropolitan Area"]


class JobLocationTests(unittest.TestCase):
    def test_query_extractor_locations_survive_compile_review_and_run(self):
        for query, extracted, expected in (
            ("Software engineers in San Francisco or New York",
             {"cities": ["San Francisco", "New York"], "countries": ["United States"]},
             {"metro_areas": METROS}),
            ("Software engineers in New York", {"metro_areas": METROS[1:]}, {"metro_areas": METROS[1:]}),
            ("Software engineers worldwide", {}, {}),
        ):
            with self.subTest(query=query), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                _start(run_dir)
                search_harness.update_pending_query(run_dir=run_dir, query=query)
                payload = _payload()
                filters = parallel_extractors._merge({}, {}, extracted, {}, {}, {}, {}, query)
                payload["role_search_filters"].update(filters)
                expanded = run_dir / "expanded.json"
                expanded.write_text(json.dumps(payload))
                rows = run_dir / "rows.jsonl"
                rows.write_text("")
                env = run_dir / "test.env"
                env.write_text("")
                with (mock.patch.object(search_harness, "_run_command", side_effect=[
                        {"payload_json": str(expanded)}, {"artifacts": {"jsonl": str(rows)}}]) as pipeline,
                      mock.patch.object(search_harness, "_ensure_hiring_company_context"),
                      mock.patch.object(search_harness, "_llm_pattern_defaults",
                                        side_effect=lambda **kw: (kw["payload"], []))):
                    search_harness.compile_pond(run_dir=run_dir, env_file=str(env))
                    search_harness.review_payload(run_dir=run_dir)
                    before = json.loads((run_dir / "results.json").read_text())
                    search_harness.run_pond(run_dir=run_dir, env_file=str(env))
                self.assertIn(query, pipeline.call_args_list[0].args[0])
                self.assertIn(query, pipeline.call_args_list[1].args[0])
                saved = json.loads((run_dir / "results.json").read_text())
                actual = saved["iterations"][0]["arm"]["payload_json"]
                final_filters = json.loads(Path(actual).read_text())["role_search_filters"]
                self.assertEqual({k: v for k, v in final_filters.items()
                                  if k in search_harness.LOCATION_FIELDS}, expected)
                self.assertEqual(final_filters, before["pending_payload"]["payload"]["role_search_filters"])
                self.assertEqual(final_filters["set_id"], "set-1")
                self.assertEqual(saved["brief"]["geography"], " or ".join(expected.get("metro_areas", [])))
                self.assertFalse((run_dir / "epoch0").exists())
                self.assertFalse((run_dir / "plan_binding.json").exists())

    def test_external_reviewed_inputs_initialize_new_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jd = root / "jd.txt"
            jd.write_text("Software engineer in New York")
            queries = root / "queries.json"
            queries.write_text(json.dumps([{"key": "q00", "query": "Software engineer in New York"}]))
            run_dir = root / "new-search"
            path = search_harness.initialize_run(
                run_dir=run_dir, jd_path=jd, queries_path=queries,
                retrieval={"backend": "powerset", "set_id": "synthetic-set"})
            self.assertEqual(path.parent, run_dir)
            self.assertEqual((run_dir / "jd.txt").read_text(), jd.read_text())

    def test_review_generates_only_query_then_initializes_without_model_or_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            jd = run_dir / "jd.txt"
            jd.write_text("Software Engineer\nLocation: San Francisco or New York\nBuild AI products.")
            env = run_dir / "test.env"
            env.write_text("")
            args = SimpleNamespace(jd_file=str(jd), env_file=str(env), queries_file=None,
                                   query_approved=False, query_model="test", query_reasoning_effort="medium",
                                   backend="powerset", set_id="synthetic-set", db="unused")
            query = {"key": "q00", "query": "Software engineers in San Francisco or New York"}
            with (mock.patch.object(decompose_jd, "generate_queries", return_value=[query]) as generate,
                  mock.patch.object(search_harness, "_run_command") as retrieve):
                review = search_harness.run_search_harness(args, run_dir, None)
                self.assertEqual(review["status"], "awaiting_query_review")
                self.assertEqual(review["query_arms"], [query])
                args.query_approved = True
                initialized = search_harness.run_search_harness(args, run_dir, None)
                search_harness.run_search_harness(args, run_dir, None)
                generate.assert_called_once()
                retrieve.assert_not_called()
            self.assertEqual(initialized["status"], "ready_to_compile")
            saved = json.loads((run_dir / "results.json").read_text())
            self.assertEqual(saved["retrieval"], {"backend": "powerset", "set_id": "synthetic-set"})
            self.assertEqual(search_harness._decision_backend(run_dir, None), "powerset")
            self.assertFalse((run_dir / "epoch0").exists())
            self.assertFalse((run_dir / "network_floors.json").exists())
            for change in ("jd", "query", "set"):
                with self.subTest(change=change):
                    previous = jd.read_text()
                    if change == "jd":
                        jd.write_text(previous + " Changed JD.")
                    elif change == "query":
                        (run_dir / "queries.json").write_text(json.dumps([{**query, "query": "Engineer worldwide"}]))
                    else:
                        args.set_id = "different-set"
                    with self.assertRaisesRegex(ValueError, "differs from this run"):
                        search_harness.run_search_harness(args, run_dir, None)
                    jd.write_text(previous)
                    (run_dir / "queries.json").write_text(json.dumps([query]))


if __name__ == "__main__":
    unittest.main()
