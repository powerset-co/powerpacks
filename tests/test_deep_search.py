"""Unit tests for the $search deep-mode primitives: JD intake, plan binding, and Pond-1 query generation."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIM = ROOT / "packs" / "search" / "primitives" / "deep_search"
if str(PRIM) not in sys.path:
    sys.path.insert(0, str(PRIM))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PRIM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


su = _load("subprocess_utils")
dj = _load("decompose_jd")
bei = _load("build_eval_inputs")
rl = _load("deep_search_loop")
fj = _load("fetch_jd")
ls = _load("location_scope")


class TestSubprocessUtils(unittest.TestCase):
    def test_run_checked_raises_on_nonzero(self):
        with self.assertRaises(su.CommandError) as ctx:
            su.run_checked([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"], description="boom")
        self.assertEqual(ctx.exception.returncode, 7)
        self.assertIn("bad", ctx.exception.stderr_tail)

    def test_run_checked_raises_on_missing_expected_path(self):
        missing = Path(tempfile.mkdtemp()) / "missing.txt"
        with self.assertRaises(su.CommandError) as ctx:
            su.run_checked([sys.executable, "-c", "pass"], expected_paths=[missing], description="artifact")
        self.assertEqual(ctx.exception.missing, [missing])


class TestLocalBackendThreading(unittest.TestCase):
    """--backend/--db threading through the deep-search sourcing chain (post search-backend fold)."""

    class _HaltAfterParse(Exception):
        pass

    def test_deep_loop_binds_backend_to_decision_json(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            decision = run_dir / "decision.json"
            decision.write_text(json.dumps({"surface": "people", "backend": "local", "depth": "deep"}))
            backend, used = rl.resolve_backend(run_dir, None, None)
            self.assertEqual((backend, used), ("local", decision))
            with self.assertRaises(ValueError):
                rl.resolve_backend(run_dir, "powerset", None)

    def _parse_with_real_parser(self, mod, argv: list[str]) -> argparse.Namespace:
        """Drive mod.main() only through its real argparse parse, then halt (no execution)."""
        captured: dict[str, argparse.Namespace] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def spy(parser, *args, **kwargs):
            captured["args"] = real_parse_args(parser, *args, **kwargs)
            raise TestLocalBackendThreading._HaltAfterParse()

        old_argv = sys.argv
        sys.argv = argv
        try:
            with mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
                with self.assertRaises(TestLocalBackendThreading._HaltAfterParse):
                    mod.main()
        finally:
            sys.argv = old_argv
        return captured["args"]

    def test_deep_search_loop_parser_accepts_local_backend(self):
        args = self._parse_with_real_parser(
            rl,
            ["loop", "--jd-file", "jd.txt", "--run-dir", "run", "--created-at", "t",
             "--backend", "local", "--db", "x.duckdb"],
        )
        self.assertEqual(args.backend, "local")
        self.assertEqual(args.db, "x.duckdb")

    def test_deep_search_loop_parser_accepts_reviewed_queries_file(self):
        args = self._parse_with_real_parser(
            rl,
            ["loop", "--jd-file", "jd.txt", "--run-dir", "run", "--created-at", "t",
             "--queries-file", "edited-queries.json"],
        )
        self.assertEqual(args.queries_file, "edited-queries.json")


class TestDecomposeJd(unittest.TestCase):
    def test_query_response_is_checkpointed_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = bei.plan_from_obj(
                {"must_have": [{"trait": "Build systems", "tier": "core"}]},
                set_name="team", set_id="set-1", source_url=None, created_at="t",
            )
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            out = root / "queries.json"
            response = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="{malformed"))])
            client = mock.Mock()
            client.chat.completions.create.return_value = response
            argv = sys.argv
            sys.argv = [
                "decompose", "--jd", "Build systems", "--plan", str(plan_path),
                "--api-key", "test", "--out", str(out),
            ]
            try:
                with mock.patch.object(dj, "make_openai_client", return_value=client), \
                     self.assertRaises(json.JSONDecodeError):
                    dj.main()
            finally:
                sys.argv = argv

            self.assertEqual((root / "queries.raw.json").read_text(encoding="utf-8"),
                             "{malformed")

    def test_parse_seeds_strings_and_objects(self):
        self.assertEqual(dj.parse_seeds({"seeds": ["a", "b"]}),
                         [{"key": "q00", "query": "a"}, {"key": "q01", "query": "b"}])
        self.assertEqual(dj.parse_seeds({"seeds": [{"query": "x"}, {"seed": "y"}]}),
                         [{"key": "q00", "query": "x"}, {"key": "q01", "query": "y"}])

    def test_parse_seeds_skips_empty(self):
        out = dj.parse_seeds({"seeds": ["a", "", "b", "c"]})
        self.assertEqual([s["query"] for s in out], ["a", "b", "c"])

    def test_parse_seeds_raises_on_empty(self):
        with self.assertRaises(ValueError):
            dj.parse_seeds({"seeds": []})

    def test_build_messages_includes_jd(self):
        msgs = dj.build_messages("Build RAG systems")
        self.assertIn("Build RAG systems", msgs[-1]["content"])

    def test_build_messages_accepts_reviewed_system_prompt(self):
        msgs = dj.build_messages("Build RAG systems", system_prompt="custom prompt")
        self.assertEqual(msgs[0]["content"], "custom prompt")

    def test_pond1_prompt_generates_one_primary_population(self):
        msgs = dj.build_messages("Build RAG systems", system_prompt=dj.SYSTEM)
        self.assertIn("primary recruiter query", msgs[-1]["content"])
        self.assertIn("source occupation", msgs[0]["content"])
        self.assertIn("one defining experience", msgs[0]["content"])
        self.assertIn("Default to the plain occupation", msgs[0]["content"])
        self.assertIn("common occupation people actually put on LinkedIn", msgs[0]["content"])
        self.assertIn("Software rule", msgs[0]["content"])
        self.assertIn("conventional occupation", msgs[0]["content"])
        self.assertIn("established feeder professions", msgs[0]["content"])
        self.assertIn("approved recruiter-plan location", msgs[0]["content"])
        self.assertIn("job title is only a clue", dj.plan_context({
            "job_title": "Synthetic Role",
            "search_scope": {"location": "Synthetic Metro"},
        }))
        self.assertIn("benchmark", msgs[0]["content"])
        self.assertIn("company-specific rules", msgs[0]["content"])
        self.assertNotIn("for example", msgs[0]["content"].lower())
        self.assertNotIn("Executive Assistant", msgs[0]["content"])

    def test_generate_queries_uses_the_production_request_and_appends_location(self):
        plan = {
            "job_title": "Software Engineer",
            "normalized_archetype": "Software Engineer",
            "pond_prompt_family": "engineering",
            "search_scope": {
                "location": "San Francisco Bay Area",
                "filters": {"metro_areas": ["San Francisco Bay Area"]},
            },
            "traits": {"must_have": []},
        }
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"seeds":["Software Engineer"]}'))])
        client = mock.Mock()
        client.chat.completions.create.return_value = response

        seeds = dj.generate_queries(
            jd="Build production software", plan=plan, model="gpt-5.6-luna",
            reasoning_effort="medium",
            client=client, service_tier="flex", use_precedents=False,
        )

        self.assertEqual(seeds[0]["query"],
                         "Software Engineer in San Francisco Bay Area")
        request = client.chat.completions.create.call_args.kwargs
        self.assertIn("Backend Engineer, Frontend Engineer, or Software Engineer",
                      request["messages"][0]["content"])
        self.assertEqual(request["service_tier"], "flex")

    def test_main_uses_family_prompt_saved_in_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = bei.plan_from_obj(
                {"pond_prompt_family": "design",
                 "must_have": [{"trait": "Design software products", "tier": "core"}]},
                set_name="team", set_id="set-1", source_url=None, created_at="t",
            )
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            out = root / "queries.json"
            response = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"seeds":["Product Designer"]}'))])
            client = mock.Mock()
            client.chat.completions.create.return_value = response
            argv = sys.argv
            sys.argv = [
                "decompose", "--jd", "Design software products", "--plan", str(plan_path),
                "--api-key", "test", "--out", str(out),
            ]
            try:
                with mock.patch.object(dj, "make_openai_client", return_value=client):
                    dj.main()
            finally:
                sys.argv = argv

        system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("this Design JD", system)
        self.assertNotIn("Growth & GTM or Sales", system)

    def test_plan_context_does_not_copy_evaluation_traits_into_queries(self):
        plan = {
            "job_title": "Product Engineer",
            "normalized_archetype": "software engineer",
            "target_level": "mid_ic",
            "search_scope": {"location": "London"},
            "traits": {"must_have": [{"trait": "React", "tier": "core"}]},
            "core_groups": [{"all_of": ["React"]}],
        }
        content = dj.build_messages(
            "Build web products with React",
            plan=plan,
            system_prompt=dj.SYSTEM,
        )[-1]["content"]
        context = content.split("SEARCH PLANNING CONTEXT:", 1)[1]
        self.assertIn('"job_title": "Product Engineer"', context)
        self.assertIn('"location": "London"', context)
        self.assertNotIn('"target_level"', context)
        self.assertNotIn('"normalized_archetype"', context)
        self.assertNotIn('"filters"', context)
        self.assertNotIn('"retrieval_filters"', context)
        self.assertNotIn('"traits"', context)
        self.assertNotIn('"core_groups"', context)
        self.assertIn("Use the full JD to choose recognizable source occupations", context)
        self.assertIn("Level, filters, and JD traits remain downstream", context)

    def test_plan_context_uses_grounded_candidate_populations(self):
        context = dj.plan_context({
            "job_title": "Synthetic Hybrid",
            "search_scope": {"location": "Synthetic Metro"},
            "candidate_populations": [{
                "population": "visual craft practitioner with implementation experience",
                "hint_kind": "dual-craft-sentence",
                "evidence_quote": "Combines visual craft with implementation experience.",
            }, {
                "population": "regulated industry experience",
                "hint_kind": "ranking-boost",
                "evidence_quote": "Experience in a regulated industry.",
            }],
        })
        self.assertIn("visual craft practitioner with implementation experience", context)
        self.assertIn("candidate_populations as the JD-grounded pond menu", context)
        self.assertIn("Ranking-boost hints", context)
        self.assertIn("comp-band-anchor hints never define a query", context)

    def test_messages_include_tiered_recruiter_precedent(self):
        messages = dj.build_messages(
            "Build production web experiences",
            precedent_cards=[{
                "quality": "seed",
                "quality_tier": 2,
                "job": "Synthetic Hybrid",
                "chain": [{"query": "Designer who can code", "action": "stop"}],
            }],
        )

        content = messages[-1]["content"]
        self.assertIn("RETRIEVED RECRUITER PRECEDENTS", content)
        self.assertIn('"quality_tier": 2', content)
        self.assertIn("Designer who can code", content)
        self.assertIn("only when", content)

    def test_generate_queries_records_the_injected_precedent_next_to_the_raw_response(self):
        card = {"quality": "seed", "quality_tier": 2, "job": "Synthetic Hybrid",
                "chain": [{"query": "Designer who can code", "action": "add_adjacent_pond",
                           "next_query": "Frontend Engineer"},
                          {"query": "Frontend Engineer", "action": "stop"}]}
        plan = {
            "job_title": "Synthetic Hybrid", "normalized_archetype": "design engineer",
            "pond_prompt_family": "design", "search_scope": {"location": None, "filters": {}},
            "traits": {"must_have": [{"trait": "production frontend work", "tier": "core"}]},
        }
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"seeds":["Designer who can code"]}'))])
        client = mock.Mock()
        client.chat.completions.create.return_value = response
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(dj, "retrieve_next_moves", return_value=[card]):
            raw_path = Path(td) / "queries.raw.json"
            dj.generate_queries(
                jd="Full JD", plan=plan,
                client=client, raw_response_path=raw_path)
            recorded = json.loads(raw_path.read_text(encoding="utf-8"))

        self.assertEqual(recorded["seeds"], ["Designer who can code"])
        self.assertEqual(recorded["precedent_cards"], [{**card, "chain": card["chain"][:1]}])

    def test_retrieve_precedent_cards_uses_plan_and_jd(self):
        card = {"quality": "seed", "quality_tier": 2}
        plan = {
            "job_title": "Synthetic Hybrid",
            "normalized_archetype": "design engineer",
            "traits": {"must_have": [{"trait": "production frontend work"}]},
        }
        with mock.patch.object(dj, "retrieve_next_moves", return_value=[card]) as retrieve:
            self.assertEqual(dj.retrieve_precedent_cards("Full JD", plan), [card])

        retrieve.assert_called_once_with(
            title="Synthetic Hybrid",
            brief={"occupation": "design engineer",
                   "defining_capability": "production frontend work"},
            query="Full JD",
            diagnosis="",
            limit=1,
        )

    def test_generate_queries_leaves_query_bare_when_plan_is_global(self):
        plan = {
            "job_title": "Software Engineer",
            "normalized_archetype": "Software Engineer",
            "pond_prompt_family": "engineering",
            "search_scope": {"location": None, "filters": {}},
            "traits": {"must_have": []},
        }
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"seeds":["Software Engineer"]}'))])
        client = mock.Mock()
        client.chat.completions.create.return_value = response

        seeds = dj.generate_queries(
            jd="Build production software", plan=plan, client=client, use_precedents=False,
        )

        self.assertEqual(seeds, [{"key": "q00", "query": "Software Engineer"}])

    def test_invalid_approved_plan_fails_before_model_client(self):
        with tempfile.TemporaryDirectory() as td:
            plan = Path(td) / "plan.json"
            plan.write_text(json.dumps({
                "search_scope": {"location": "San Francisco", "filters": {}},
            }))
            argv = sys.argv
            sys.argv = [
                "decompose", "--jd", "Build finance systems", "--plan", str(plan),
                "--api-key", "test", "--out", str(Path(td) / "seeds.json"),
            ]
            try:
                with mock.patch.object(dj, "make_openai_client") as client:
                    with self.assertRaises(SystemExit) as ctx:
                        dj.main()
                self.assertEqual(ctx.exception.code, 1)
                client.assert_not_called()
            finally:
                sys.argv = argv

    def test_malformed_plan_shapes_fail_cleanly_before_model_client(self):
        for document in ([1], {"search_scope": True}):
            with self.subTest(document=document), tempfile.TemporaryDirectory() as td:
                plan = Path(td) / "plan.json"
                plan.write_text(json.dumps(document))
                argv = sys.argv
                sys.argv = [
                    "decompose", "--jd", "Build finance systems", "--plan", str(plan),
                    "--api-key", "test", "--out", str(Path(td) / "seeds.json"),
                ]
                try:
                    with mock.patch.object(dj, "make_openai_client") as client:
                        with self.assertRaises(SystemExit) as ctx:
                            dj.main()
                    self.assertEqual(ctx.exception.code, 1)
                    client.assert_not_called()
                finally:
                    sys.argv = argv

class TestRequiredLocationScope(unittest.TestCase):
    def test_prefer_metro_area_filters_is_idempotent_and_all_or_nothing(self):
        nyc = {"cities": ["New York City"], "countries": ["US"]}
        preferred = {"metro_areas": ["New York Metropolitan Area"]}
        self.assertEqual(ls.prefer_metro_area_filters(nyc), preferred)
        self.assertEqual(ls.prefer_metro_area_filters(preferred), preferred)
        self.assertEqual(
            ls.prefer_metro_area_filters({
                "cities": ["San Francisco", "Raleigh"],
                "countries": ["United States"],
            }),
            {
                "cities": ["San Francisco", "Raleigh"],
                "countries": ["United States"],
            },
        )

    def test_metro_match_is_strict_and_missing_fails_closed(self):
        required = {"metro_areas": ["San Francisco Bay Area"]}
        self.assertEqual(ls.location_fit(required, "San Francisco, California, United States"), "match")
        self.assertEqual(ls.location_fit(required, "Palo Alto, California, United States"), "match")
        self.assertEqual(ls.location_fit(required, "San Mateo, California, United States"), "match")
        self.assertEqual(ls.location_fit(required, "Santa Monica, California, United States"), "mismatch")
        self.assertEqual(ls.location_fit(required, None), "unknown")

    def test_null_scope_does_not_gate(self):
        self.assertEqual(ls.location_fit({}, None), "not_required")

    def test_approved_global_aliases_must_be_literal_null(self):
        self.assertIsNone(ls.required_location_from_plan({"search_scope": {"location": None, "filters": {}}}))
        for alias in ("", "global", "remote", "worldwide", "anywhere"):
            with self.subTest(alias=alias):
                with self.assertRaises(ValueError):
                    ls.required_location_from_plan({"search_scope": {"location": alias, "filters": {}}})

    def test_approved_scope_requires_filter_contract_even_when_global(self):
        with self.assertRaisesRegex(ValueError, "filters is required"):
            ls.location_scope_from_plan({"search_scope": {"location": None}})
        with self.assertRaisesRegex(ValueError, "must be an object"):
            ls.location_scope_from_plan({"search_scope": {"location": None, "filters": []}})

    def test_reviewed_structured_scopes_cover_regions_states_and_cities(self):
        europe = {"macro_regions": ["Western Europe", "Eurasia"]}
        self.assertEqual(ls.location_fit(europe, "Berlin, Germany"), "match")
        self.assertEqual(ls.location_fit(europe, "San Francisco, California, United States"), "mismatch")
        self.assertEqual(
            ls.location_fit({"states": ["Ontario"], "countries": ["Canada"]}, "Toronto, Ontario, Canada"),
            "match",
        )
        london = {"cities": ["London"], "countries": ["United Kingdom"]}
        self.assertEqual(ls.location_fit(london, "London, England, United Kingdom"), "match")
        self.assertEqual(ls.location_fit(london, "London, Ontario, Canada"), "mismatch")
        africa = ls.canonicalize_generated_location_filters("Africa", {"macro_regions": ["Africa"]})
        oceania = ls.canonicalize_generated_location_filters("Oceania", {"macro_regions": ["Oceania"]})
        self.assertEqual(ls.location_fit(africa, "Accra, Ghana"), "match")
        self.assertEqual(ls.location_fit(oceania, "Sydney, New South Wales, Australia"), "match")

    def test_reviewed_city_scope_requires_country_qualifier(self):
        with self.assertRaisesRegex(ValueError, "country qualifier"):
            ls.location_scope_from_plan({
                "search_scope": {
                    "location": "London, UK",
                    "filters": {"cities": ["London"]},
                }
            })

    def test_generated_scope_canonicalizes_aliases_before_review(self):
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "London, UK", {"cities": ["London"], "countries": ["UK"]},
            ),
            {"countries": ["United Kingdom"]},
        )
        self.assertEqual(
            ls.canonicalize_location_filters({"states": ["CA"], "countries": ["US"]}),
            {"states": ["California"], "countries": ["United States"]},
        )
        self.assertEqual(
            ls.canonicalize_location_filters({"metro_areas": ["New York City metropolitan area"]}),
            {"metro_areas": ["New York Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_location_filters({"metro_areas": ["London Metropolitan Area"]}),
            {"metro_areas": ["London Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "New York City", {"cities": ["New York City"], "countries": ["US"]},
            ),
            {"metro_areas": ["New York Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "New York Metropolitan Area, United States",
                {
                    "metro_areas": ["New York Metropolitan Area"],
                    "countries": ["United States"],
                },
            ),
            {"metro_areas": ["New York Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "Stockholm, Sweden", {"metro_areas": ["Stockholm"]},
            ),
            {"countries": ["Sweden"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "Irvine, California, United States",
                {
                    "cities": ["Irvine"],
                    "states": ["California"],
                    "countries": ["United States"],
                },
            ),
            {"metro_areas": ["Los Angeles Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters("Europe", {"macro_regions": ["Europe"]}),
            {"macro_regions": ["Western Europe", "Eurasia"]},
        )

    def test_every_local_canonical_metro_is_idempotent(self):
        mapping = json.loads(ls.LOCATION_MAPPING_FILE.read_text(encoding="utf-8"))
        metros = {
            value
            for values in mapping["city_to_metro"].values()
            for value in (values if isinstance(values, list) else [values])
        }
        for metro in sorted(metros):
            with self.subTest(metro=metro):
                self.assertEqual(
                    ls.canonicalize_location_filters({"metro_areas": [metro]}),
                    {"metro_areas": [metro]},
                )

    def test_city_canonicalization_is_field_aware_and_review_idempotent(self):
        filters = ls.canonicalize_generated_location_filters(
            "New York City", {"cities": ["New York City"], "countries": ["US"]},
        )
        self.assertEqual(filters, {"metro_areas": ["New York Metropolitan Area"]})
        self.assertEqual(
            ls.location_scope_from_plan({
                "search_scope": {"location": "New York Metropolitan Area", "filters": filters},
            }),
            ("New York Metropolitan Area", filters),
        )
        for city, country in (("Washington", "United States"), ("Victoria", "Canada")):
            with self.subTest(city=city):
                expected = {"cities": [city], "countries": [country]}
                self.assertEqual(
                    ls.location_scope_from_plan({
                        "search_scope": {
                            "location": ls.canonical_location_label(expected), "filters": expected,
                        },
                    })[1],
                    expected,
                )

    def test_approved_scope_rejects_noncanonical_or_conflicting_filters(self):
        with self.assertRaisesRegex(ValueError, "canonical values"):
            ls.location_scope_from_plan({
                "search_scope": {"location": "California", "filters": {"states": ["CA"], "countries": ["US"]}}
            })
        with self.assertRaisesRegex(ValueError, "conflict"):
            ls.location_scope_from_plan({
                "search_scope": {"location": "San Francisco Bay Area", "filters": {"countries": ["Germany"]}}
            })
        for location, filters in (
            ("Americas", {"macro_regions": ["APAC"]}),
            ("Middle East", {"macro_regions": ["Western Europe"]}),
            ("United States", {"countries": ["United States", "Germany"]}),
            ("London, UK", {"cities": ["London"], "countries": ["United Kingdom", "Canada"]}),
        ):
            with self.subTest(location=location):
                with self.assertRaises(ValueError):
                    ls.location_scope_from_plan({
                        "search_scope": {"location": location, "filters": filters}
                    })

    def test_cross_country_multi_office_scope_must_use_metros(self):
        with self.assertRaisesRegex(ValueError, "exactly one country"):
            ls.location_scope_from_plan({
                "search_scope": {
                    "location": "Vancouver or Portland",
                    "filters": {
                        "cities": ["Vancouver", "Portland"],
                        "countries": ["Canada", "United States"],
                    },
                }
            })
        metros = {"metro_areas": ["Vancouver Metropolitan Area", "Portland Metropolitan Area"]}
        label = ls.canonical_location_label(metros)
        self.assertEqual(
            ls.location_scope_from_plan({"search_scope": {"location": label, "filters": metros}}),
            (label, metros),
        )
        with self.assertRaisesRegex(ValueError, "conflict|broaden"):
            ls.location_scope_from_plan({
                "search_scope": {"location": "Vancouver or Portland", "filters": metros},
            })

    def test_reviewed_label_cannot_be_silently_broadened_or_contradicted(self):
        cases = (
            ("San Francisco Bay Area", {"countries": ["United States"]}),
            ("London, UK", {"countries": ["United Kingdom"]}),
            ("California", {"countries": ["United States"]}),
            ("San Francisco, CA", {"states": ["California"], "countries": ["United States"]}),
            ("San Francisco", {"countries": ["Germany"]}),
            ("San Francisco", {"metro_areas": ["San Francisco Bay Area"]}),
            ("Remote US", {"countries": ["Germany"]}),
        )
        for location, filters in cases:
            with self.subTest(location=location):
                with self.assertRaisesRegex(ValueError, "conflict|broaden"):
                    ls.location_scope_from_plan({
                        "search_scope": {"location": location, "filters": filters},
                    })

    def test_ambiguous_state_abbreviations_need_country_context(self):
        with self.assertRaisesRegex(ValueError, "country"):
            ls.canonicalize_generated_location_filters("Perth, WA", {"cities": ["Perth"]})
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "Perth, WA", {"cities": ["Perth"], "countries": ["Australia"]},
            ),
            {"countries": ["Australia"]},
        )
        self.assertEqual(
            ls.location_fit(
                {"cities": ["Perth"], "countries": ["Australia"]}, "Perth, WA",
            ),
            "unknown",
        )

    def test_broad_continent_scopes_are_country_unions_for_both_backends(self):
        africa = ls.canonicalize_generated_location_filters("Africa", {"macro_regions": ["Africa"]})
        oceania = ls.canonicalize_generated_location_filters("Oceania", {"macro_regions": ["Oceania"]})
        latin_america = ls.canonicalize_generated_location_filters(
            "Latin America", {"macro_regions": ["Latin America"]},
        )
        self.assertIn("Ghana", africa["countries"])
        self.assertIn("Australia", oceania["countries"])
        self.assertIn("Brazil", latin_america["countries"])
        self.assertNotIn("United States", latin_america["countries"])
        mixed = ls.canonicalize_generated_location_filters(
            "Africa or Middle East", {"macro_regions": ["Africa", "Middle East"]},
        )
        self.assertIn("Ghana", mixed["countries"])
        self.assertIn("Israel", mixed["countries"])


_sb_spec = importlib.util.spec_from_file_location(
    "seniority_bands", ROOT / "packs" / "search" / "primitives" / "shared" / "seniority_bands.py"
)
sb = importlib.util.module_from_spec(_sb_spec)
_sb_spec.loader.exec_module(sb)  # type: ignore[union-attr]


class TestPreserveSemanticQuery(unittest.TestCase):
    def test_preserves_raw_query_and_keeps_bm25_and_filters(self):
        payload = {"role_search_filters": {
            "semantic_query": "Engineers specializing in distributed systems design and implementation",
            "bm25_queries": ["distributed systems engineer", "scheduler engineer"],
            "seniority_bands": ["staff"], "cities": ["San Francisco"],
        }}
        raw = "Distributed systems engineer who built admission control and bin packing for a GPU cluster"
        out = sb.pin_payload_semantic_query(payload, raw)
        f = out["role_search_filters"]
        self.assertEqual(f["semantic_query"], raw)          # raw query becomes the vector
        self.assertTrue(f["semantic_query_preserved"])
        self.assertEqual(f["bm25_queries"], ["distributed systems engineer", "scheduler engineer"])  # bm25 kept
        self.assertEqual(f["seniority_bands"], ["staff"])   # filters kept
        self.assertEqual(f["cities"], ["San Francisco"])
        self.assertTrue(any("semantic_query preserved" in n for n in out["notes"]))

    def test_does_not_mutate_input(self):
        payload = {"role_search_filters": {"semantic_query": "orig", "bm25_queries": ["x"]}}
        sb.pin_payload_semantic_query(payload, "new")
        self.assertEqual(payload["role_search_filters"]["semantic_query"], "orig")


class TestBuildEvalInputs(unittest.TestCase):
    def test_plan_from_obj_shapes_traits_and_scope(self):
        plan = bei.plan_from_obj(
            {"job_title": "MTS", "normalized_archetype": "distsys engineer",
             "hiring_company_name": "Firecrawl",
             "hire_stage": "scale", "usable_cutoff": "Senior IC in band.",
             "must_have": ["schedulers", "control plane", ""], "nice_to_have": ["gpus"]},
            set_name="s", set_id="sid", source_url=None, created_at="2026-01-01T00:00:00Z",
            source_metadata={"company_website_url": "https://firecrawl.dev"})
        self.assertEqual([t["trait"] for t in plan["traits"]["must_have"]], ["schedulers", "control plane"])
        self.assertEqual(plan["traits"]["nice_to_have"], [{"trait": "gpus", "source": "jd"}])
        self.assertEqual(plan["set_scope"], {"name": "s", "set_id": "sid"})
        self.assertEqual(plan["normalized_archetype"], "distsys engineer")
        self.assertEqual(plan["hiring_company"], {
            "name": "Firecrawl", "website_url": "https://firecrawl.dev"})
        self.assertEqual(plan["hire_stage"], "scaling_late")
        self.assertEqual(plan["search_scope"], {"location": None, "filters": {}, "source": "jd"})
        self.assertFalse(plan["retrieval_ran"])

    def test_plan_from_obj_keeps_only_verbatim_population_hints_and_comp_band(self):
        jd = ("The role combines visual craft with implementation.\n"
              "Base Salary Range: $140,000/yr to $220,000/yr.")
        plan = bei.plan_from_obj({
            "must_have": [{"trait": "hybrid craft", "tier": "core"}],
            "candidate_populations": [{
                "population": "visual craft practitioner who implements",
                "hint_kind": "dual-craft-sentence",
                "evidence_quote": "The role combines visual craft with implementation.",
            }, {
                "population": "unsupported population",
                "hint_kind": "stated-background",
                "evidence_quote": "This quote is not in the JD.",
            }],
            "comp_band": {
                "currency": "usd", "minimum": 140000, "maximum": 220000,
                "period": "year",
                "evidence_quote": "Base Salary Range: $140,000/yr to $220,000/yr.",
            },
        }, set_name="s", set_id="sid", source_url=None, created_at="t", jd_text=jd)

        self.assertEqual(plan["candidate_populations"], [{
            "population": "visual craft practitioner who implements",
            "hint_kind": "dual-craft-sentence",
            "evidence_quote": "The role combines visual craft with implementation.",
        }])
        self.assertEqual(plan["comp_band"]["currency"], "USD")
        self.assertEqual(plan["comp_band"]["minimum"], 140000)
        self.assertEqual(plan["comp_band"]["maximum"], 220000)

    def test_plan_population_prompt_defines_kinds_without_benchmark_examples(self):
        for hint_kind in bei.VALID_HINT_KINDS:
            self.assertIn(hint_kind, bei.DEEP_PLAN_ADAPTER_PROMPT)
        for benchmark_term in ("Lovable", "Pylon", "WebGL", "designer who codes"):
            self.assertNotIn(benchmark_term, bei.DEEP_PLAN_ADAPTER_PROMPT)

    def test_plan_from_obj_requires_reviewable_structured_location(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        inferred_europe = bei.plan_from_obj(
            {**base, "location": "Europe"},
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            inferred_europe["search_scope"]["filters"],
            {"macro_regions": ["Western Europe", "Eurasia"]},
        )
        plan = bei.plan_from_obj(
            {
                **base,
                "location": "Europe",
                "location_filters": {"macro_regions": ["Western Europe", "Eurasia"]},
            },
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            plan["search_scope"],
            {
                "location": "Europe",
                "filters": {"macro_regions": ["Western Europe", "Eurasia"]},
                "source": "jd",
            },
        )
        remote = bei.plan_from_obj(
            {**base, "location": "remote", "location_filters": {}},
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(remote["search_scope"], {"location": None, "filters": {}, "source": "jd"})

        remote_us = bei.plan_from_obj(
            {**base, "location": "remote", "location_filters": {"countries": ["US"]}},
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            remote_us["search_scope"],
            {"location": "United States", "filters": {"countries": ["United States"]}, "source": "jd"},
        )

        remote_multi = bei.plan_from_obj(
            {
                **base, "location": "remote",
                "location_filters": {"countries": ["US", "Canada"]},
            },
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            remote_multi["search_scope"],
            {
                "location": "United States or Canada",
                "filters": {"countries": ["United States", "Canada"]},
                "source": "jd",
            },
        )

        africa = bei.plan_from_obj(
            {**base, "location": "Africa", "location_filters": {"countries": ["Ghana"]}},
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(africa["search_scope"]["location"], "Africa")
        self.assertEqual(
            set(africa["search_scope"]["filters"]["countries"]),
            set(ls.CONTINENT_COUNTRIES["Africa"]),
        )
        remote_africa = bei.plan_from_obj(
            {**base, "location": "Remote Africa", "location_filters": {"macro_regions": ["Africa"]}},
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(remote_africa["search_scope"]["location"], "Africa")

        latin_america = bei.plan_from_obj(
            {
                **base, "location": "LATAM",
                "location_filters": {"macro_regions": ["Americas"]},
            },
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(latin_america["search_scope"]["location"], "Latin America")
        self.assertEqual(
            set(latin_america["search_scope"]["filters"]["countries"]),
            ls.LATIN_AMERICA_COUNTRIES,
        )

        nyc = bei.plan_from_obj(
            {
                **base, "location": "New York City",
                "location_filters": {"cities": ["New York City"], "countries": ["US"]},
            },
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            nyc["search_scope"],
            {
                "location": "New York Metropolitan Area",
                "filters": {"metro_areas": ["New York Metropolitan Area"]},
                "source": "jd",
            },
        )
        generated = bei.plan_from_obj(
            {
                **base, "location": "San Francisco",
                "location_filters": {"countries": ["Germany"]},
            },
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            generated["search_scope"],
            {
                "location": "Europe",
                "filters": {"macro_regions": ["Western Europe", "Eurasia"]},
                "source": "jd",
            },
        )

    def test_generated_location_accepts_natural_exact_or_labels_and_metro_aliases(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        cases = (
            (
                "Vancouver or Portland",
                {"metro_areas": ["Vancouver Metropolitan Area", "Portland Metropolitan Area"]},
            ),
            (
                "New York and Boston",
                {"metro_areas": ["New York Metropolitan Area", "Boston Metropolitan Area"]},
            ),
            (
                "New York, Boston, or Chicago",
                {"metro_areas": [
                    "New York Metropolitan Area", "Boston Metropolitan Area",
                    "Chicago Metropolitan Area",
                ]},
            ),
            (
                "Bay Area, New York, or Boston",
                {"metro_areas": [
                    "San Francisco Bay Area", "New York Metropolitan Area",
                    "Boston Metropolitan Area",
                ]},
            ),
            (
                "San Francisco, CA or New York, NY",
                {"metro_areas": ["San Francisco Bay Area", "New York Metropolitan Area"]},
            ),
            (
                "New York, Boston, or Chicago",
                {"metro_areas": [
                    "New York Metropolitan Area", "Boston Metropolitan Area",
                    "Chicago Metropolitan Area",
                ]},
            ),
            ("US and Canada", {"countries": ["United States", "Canada"]}),
            (
                "California, Texas, or New York",
                {
                    "states": ["California", "Texas", "New York"],
                    "countries": ["United States"],
                },
            ),
            ("Silicon Valley", {"metro_areas": ["San Francisco Bay Area"]}),
            ("NYC metro", {"metro_areas": ["New York Metropolitan Area"]}),
            ("Tri-state area", {"metro_areas": ["New York Metropolitan Area"]}),
        )
        for location, filters in cases:
            with self.subTest(location=location):
                plan = bei.plan_from_obj(
                    {**base, "location": location, "location_filters": filters},
                    set_name="s", set_id="sid", source_url=None, created_at="t",
                )
                self.assertEqual(plan["search_scope"]["filters"], filters)
                self.assertEqual(
                    plan["search_scope"]["location"],
                    ls.canonical_location_label(filters),
                )

    def test_generated_location_uses_structured_filters_as_authoritative(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        plan = bei.plan_from_obj(
            {
                **base, "location": "San Francisco or New York",
                "location_filters": {"metro_areas": [
                    "San Francisco Bay Area", "New York Metropolitan Area",
                    "Boston Metropolitan Area",
                ]},
            },
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(
            plan["search_scope"]["location"],
            "San Francisco Bay Area or New York Metropolitan Area or Boston Metropolitan Area",
        )

    def test_missing_archetype_falls_back_to_role_not_engineer(self):
        plan = bei.plan_from_obj(
            {"job_title": "Strategic Finance Lead", "must_have": [{"trait": "operating P&L", "tier": "core"}]},
            set_name="s", set_id="sid", source_url=None, created_at="t",
        )
        self.assertEqual(plan["normalized_archetype"], "Strategic Finance Lead")

    def test_plan_from_obj_user_preferences_override_jd_and_record_provenance(self):
        plan = bei.plan_from_obj(
            {
                "hire_stage": "growth",
                "must_have": [{"trait": "systems", "tier": "core"}],
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
            user_preferences={"hire_stage": "early", "pedigree_policy": "ignore"},
        )
        policy = plan["recruiter_policy"]
        self.assertEqual(plan["hire_stage"], "founding_early")
        self.assertEqual(policy["preferences"]["pedigree_policy"], "ignore")
        self.assertEqual(policy["provenance"]["hire_stage"]["source"], "user")
        self.assertEqual(policy["provenance"]["pedigree_policy"]["source"], "user")

    def test_plan_from_obj_ignores_model_authored_taste_preferences(self):
        plan = bei.plan_from_obj(
            {
                "hire_stage": "growth",
                "must_have": [{"trait": "systems", "tier": "core"}],
                "recruiter_preferences": {
                    "pedigree_policy": "ignore",
                    "current_founder_c_suite_for_non_exec_ic": "review",
                },
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
            user_preferences={"current_founder_c_suite_for_non_exec_ic": "eligible"},
        )
        policy = plan["recruiter_policy"]
        self.assertEqual(policy["preferences"]["pedigree_policy"], "positive_prior_not_gate")
        self.assertEqual(policy["provenance"]["pedigree_policy"]["source"], "default")
        self.assertEqual(policy["preferences"]["current_founder_c_suite_for_non_exec_ic"], "eligible")
        self.assertEqual(
            policy["provenance"]["current_founder_c_suite_for_non_exec_ic"]["source"],
            "user",
        )

    def test_plan_from_obj_ignores_non_object_recruiter_preferences(self):
        plan = bei.plan_from_obj(
            {
                "must_have": [{"trait": "search systems", "tier": "core"}],
                "recruiter_preferences": ["not", "an", "object"],
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            plan["recruiter_policy"]["provenance"]["pedigree_policy"]["source"],
            "default",
        )

    def test_plan_from_obj_requires_must_have(self):
        with self.assertRaises(ValueError):
            bei.plan_from_obj({"must_have": []}, set_name="s", set_id="i", source_url=None, created_at="t")

    def test_plan_target_level_valid_passes_through(self):
        plan = bei.plan_from_obj({"must_have": ["x"], "target_level": "VP"},
                                 set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "vp")  # normalized lowercase

    def test_plan_target_level_invalid_defaults_to_senior_ic(self):
        plan = bei.plan_from_obj({"must_have": ["x"], "target_level": "supreme_overlord"},
                                 set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "senior_ic")

    def test_plan_target_level_absent_defaults_to_senior_ic(self):
        plan = bei.plan_from_obj({"must_have": ["x"]}, set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "senior_ic")

    def test_build_plan_messages_carries_jd(self):
        msgs = bei.build_plan_messages("Design schedulers")
        self.assertIn("Design schedulers", msgs[-1]["content"])

    def test_build_plan_messages_accepts_reviewed_system_prompt(self):
        msgs = bei.build_plan_messages("Design schedulers", "MY REVIEWED PLAN PROMPT")
        self.assertEqual(msgs[0]["content"], "MY REVIEWED PLAN PROMPT")

    def test_plan_request_is_the_extract_plan_request(self):
        request = bei.plan_request(
            jd="Design schedulers", model="gpt-5.6-luna",
            reasoning_effort="medium", service_tier="flex",
            source_metadata={"department": "Engineering"},
        )

        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertEqual(request["reasoning_effort"], "medium")
        self.assertEqual(request["service_tier"], "flex")
        self.assertIn("Source department hint: Engineering",
                      request["messages"][-1]["content"])

    def test_must_trait_tagged_object_preserves_tier(self):
        self.assertEqual(bei._must_trait({"trait": "distributed systems", "tier": "core"}),
                         {"trait": "distributed systems", "tier": "core", "source": "jd"})

    def test_must_trait_invalid_tier_defaults_table_stakes(self):
        # A mis-tagged/absent tier must NOT over-gate -> degrade to table_stakes (gate falls back).
        self.assertEqual(bei._must_trait({"trait": "x", "tier": "bogus"})["tier"], "table_stakes")
        self.assertEqual(bei._must_trait({"trait": "x"})["tier"], "table_stakes")

    def test_must_trait_bare_string_is_table_stakes(self):
        self.assertEqual(bei._must_trait("schedulers"),
                         {"trait": "schedulers", "tier": "table_stakes", "source": "jd"})
        self.assertIsNone(bei._must_trait("   "))

    def test_plan_from_obj_carries_core_tier(self):
        plan = bei.plan_from_obj(
            {"must_have": [{"trait": "fusion hardware", "tier": "core"},
                           {"trait": "leadership", "tier": "table_stakes"}]},
            set_name="s", set_id="i", source_url=None, created_at="t")
        tiers = {t["trait"]: t["tier"] for t in plan["traits"]["must_have"]}
        self.assertEqual(tiers, {"fusion hardware": "core", "leadership": "table_stakes"})

    def test_plan_core_groups_are_alternative_all_of_gates(self):
        plan = bei.plan_from_obj(
            {"must_have": [
                {"trait": "distributed schedulers", "tier": "core"},
                {"trait": "control planes", "tier": "core"},
                {"trait": "inference serving", "tier": "core"},
            ], "core_groups": [
                {"name": "scheduler", "all_of": ["distributed schedulers", "control planes"]},
                {"name": "inference", "all_of": ["inference serving"]},
            ]},
            set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["core_groups"][0]["all_of"], ["distributed schedulers", "control planes"])
        self.assertEqual(plan["core_groups"][1]["all_of"], ["inference serving"])

    def test_generated_plan_conforms_to_published_schema(self):
        plan = bei.plan_from_obj(
            {"job_title": "Staff Engineer", "normalized_archetype": "systems engineer",
             "hire_stage": "growth", "location": "San Francisco Bay Area",
             "location_filters": {"metro_areas": ["San Francisco Bay Area"]},
             "must_have": [{"trait": "distributed systems", "tier": "core"}],
             "nice_to_have": ["GPU infrastructure"]},
            set_name="team", set_id="set-1", source_url=None,
            created_at="2026-07-10T00:00:00Z")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"
            path.write_text(json.dumps(plan))
            cp = subprocess.run([
                sys.executable,
                str(ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py"),
                "--schema", "search-network-jd-plan", "--file", str(path),
            ], text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)

    def test_main_requires_created_at_before_openai(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build distributed systems")
        argv = sys.argv
        sys.argv = ["build", "--run-dir", str(d), "--jd-file", str(jd)]
        try:
            with mock.patch.object(bei, "make_openai_client") as client, \
                 self.assertRaises(SystemExit):
                bei.main()
            client.assert_not_called()
        finally:
            sys.argv = argv


class TestDeepSearchLoop(unittest.TestCase):
    def _approved_plan(self, directory: Path) -> Path:
        path = directory / "plan.json"
        path.write_text(json.dumps(bei.plan_from_obj(
            {
                "job_title": "Staff Engineer",
                "hire_stage": "growth",
                "must_have": [{"trait": "distributed systems", "tier": "core"}],
            },
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="t",
        )))
        return path

    def test_approved_plan_cross_field_validation_accepts_resolved_contract(self):
        directory = Path(tempfile.mkdtemp())
        plan_path = self._approved_plan(directory)

        validated = rl.validate_approved_plan(plan_path)

        self.assertEqual(validated["hire_stage"], "scaling_late")

    def test_approved_plan_schema_rejects_non_object_shapes_before_cross_field_checks(self):
        directory = Path(tempfile.mkdtemp())
        plan_path = directory / "plan.json"
        for document in ([1], {"search_scope": True}):
            with self.subTest(document=document):
                plan_path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    rl.validate_approved_plan(plan_path)

    def test_approved_plan_cross_field_validation_rejects_stage_and_core_drift(self):
        directory = Path(tempfile.mkdtemp())
        plan_path = self._approved_plan(directory)
        plan = json.loads(plan_path.read_text())
        plan["hire_stage"] = "founding_early"
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            rl.validate_approved_plan(plan_path)

        plan["hire_stage"] = "scaling_late"
        plan["core_groups"][0]["all_of"] = ["invented trait"]
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "core_groups reference non-core"):
            rl.validate_approved_plan(plan_path)

        plan = json.loads(self._approved_plan(directory).read_text())
        plan["search_scope"]["location"] = "remote"
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "search_scope"):
            rl.validate_approved_plan(plan_path)

    def test_approved_plan_rejects_oversized_conjunction_and_url_drift(self):
        directory = Path(tempfile.mkdtemp())
        plan_path = self._approved_plan(directory)
        plan = json.loads(plan_path.read_text())
        plan["traits"]["must_have"] = [
            {"trait": trait, "tier": "core", "source": "jd"}
            for trait in ("a", "b", "c", "d")
        ]
        plan["core_groups"] = [{"name": "mega", "all_of": ["a", "b", "c", "d"], "source": "jd"}]
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "at most 3"):
            rl.validate_approved_plan(plan_path)

        plan["core_groups"] = [{"name": "default conjunction", "all_of": ["a", "b"], "source": "default"},
                               {"name": "c", "all_of": ["c"], "source": "default"},
                               {"name": "d", "all_of": ["d"], "source": "default"}]
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "default core_groups must be singleton"):
            rl.validate_approved_plan(plan_path)

        plan["core_groups"] = [{"name": trait, "all_of": [trait], "source": "default"}
                               for trait in ("a", "b", "c", "d")]
        plan["source_url"] = "https://example.test/original"
        plan_path.write_text(json.dumps(plan))
        self.assertEqual(
            rl.validate_approved_plan(
                plan_path,
                expected_source_url="https://EXAMPLE.test/original#apply",
            )["source_url"],
            "https://example.test/original",
        )
        with self.assertRaisesRegex(ValueError, "conflicts with requested URL"):
            rl.validate_approved_plan(plan_path, expected_source_url="https://example.test/other")

    def test_url_source_binding_rejects_missing_or_different_metadata(self):
        directory = Path(tempfile.mkdtemp())
        source = directory / "source.json"
        with self.assertRaisesRegex(ValueError, "cannot verify"):
            rl.validate_bound_jd_source(source, "https://example.test/job")
        source.write_text(json.dumps({
            "requested_url": "https://example.test/job",
            "source_url": "https://redirect.test/final",
        }))
        self.assertEqual(
            rl.validate_bound_jd_source(source, "https://EXAMPLE.test/job#apply")["source_url"],
            "https://redirect.test/final",
        )
        with self.assertRaisesRegex(ValueError, "conflicts with the URL bound"):
            rl.validate_bound_jd_source(source, "https://example.test/other")

    def test_approved_plan_binding_rejects_contract_or_backend_drift(self):
        directory = Path(tempfile.mkdtemp())
        run_dir = directory / "run"
        run_dir.mkdir()
        plan_path = self._approved_plan(directory)
        jd_path = directory / "jd.txt"
        jd_path.write_text("original role")
        retrieval = {"backend": "local", "db_path": "/tmp/a.duckdb", "db_size": 1, "db_mtime_ns": 2}
        canonical, digest = rl.bind_approved_plan(run_dir, plan_path, retrieval, jd_path)
        self.assertEqual(canonical, run_dir / "epoch0" / "plan.json")
        self.assertEqual(json.loads((run_dir / "plan_binding.json").read_text())["plan_sha256"], digest)

        plan = json.loads(plan_path.read_text())
        plan["job_title"] = "Different role"
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "differs from the contract"):
            rl.bind_approved_plan(run_dir, plan_path, retrieval, jd_path)
        with self.assertRaisesRegex(ValueError, "retrieval corpus differs"):
            rl.bind_approved_plan(
                run_dir,
                canonical,
                {"backend": "local", "db_path": "/tmp/b.duckdb", "db_size": 1, "db_mtime_ns": 2},
                jd_path,
            )
        jd_path.write_text("changed role")
        with self.assertRaisesRegex(ValueError, "JD source differs"):
            rl.bind_approved_plan(run_dir, canonical, retrieval, jd_path)

    def test_approved_plan_binding_rejects_reviewed_query_drift(self):
        directory = Path(tempfile.mkdtemp())
        run_dir = directory / "run"
        run_dir.mkdir()
        plan_path = self._approved_plan(directory)
        jd_path = directory / "jd.txt"
        jd_path.write_text("original role")
        queries_path = directory / "queries.json"
        queries_path.write_text('[{"key":"q00","query":"first"}]')
        retrieval = {"backend": "powerset", "set_id": "set-reviewed"}

        canonical, _ = rl.bind_approved_plan(
            run_dir, plan_path, retrieval, jd_path, queries_path)
        queries_path.write_text('[{"key":"q00","query":"changed"}]')

        with self.assertRaisesRegex(ValueError, "reviewed queries differ"):
            rl.bind_approved_plan(
                run_dir, canonical, retrieval, jd_path, queries_path)

    def test_retrieval_identity_enforces_reviewed_set_and_local_db(self):
        directory = Path(tempfile.mkdtemp())
        plan_path = self._approved_plan(directory)
        plan = json.loads(plan_path.read_text())
        plan["set_scope"]["set_id"] = "set-reviewed"

        identity, set_id, db = rl.resolve_retrieval_identity(
            "powerset", plan, None, "unused.duckdb"
        )
        self.assertEqual(identity, {"backend": "powerset", "set_id": "set-reviewed"})
        self.assertEqual(set_id, "set-reviewed")
        self.assertEqual(db, "unused.duckdb")
        with self.assertRaisesRegex(ValueError, "conflicts with approved plan"):
            rl.resolve_retrieval_identity("powerset", plan, "set-other", "unused.duckdb")

        db_path = directory / "local.duckdb"
        db_path.write_bytes(b"duckdb fixture")
        identity, set_id, resolved_db = rl.resolve_retrieval_identity(
            "local", plan, "ignored", str(db_path)
        )
        self.assertEqual(identity["backend"], "local")
        self.assertEqual(identity["db_path"], str(db_path.resolve()))
        self.assertEqual(identity["db_size"], len(b"duckdb fixture"))
        self.assertIsNone(set_id)
        self.assertEqual(resolved_db, str(db_path.resolve()))

    def test_unbound_execution_artifacts_cannot_be_reused(self):
        artifacts = (
            "results.json",
            "manifest.json",
            "ponds/pond-1/payload.json",
        )
        for relative in artifacts:
            with self.subTest(relative=relative):
                directory = Path(tempfile.mkdtemp())
                run_dir = directory / "run"
                path = run_dir / relative
                path.parent.mkdir(parents=True)
                path.write_text("{}\n")
                plan_path = self._approved_plan(directory)
                with self.assertRaisesRegex(ValueError, "without an approved-plan binding"):
                    rl.bind_approved_plan(
                        run_dir, plan_path, {"backend": "powerset", "set_id": "set-1"},
                    )
                self.assertFalse((run_dir / "plan_binding.json").exists())

    def test_pre_review_artifacts_remain_bindable(self):
        directory = Path(tempfile.mkdtemp())
        run_dir = directory / "run"
        (run_dir / "epoch0").mkdir(parents=True)
        plan_path = self._approved_plan(directory)
        (run_dir / "queries.json").write_text('[{"key":"q00","query":"first"}]')
        (run_dir / "network_floors.json").write_text(json.dumps({"floors": []}))
        (run_dir / "decision.json").write_text(json.dumps({"surface": "people"}))
        canonical, _ = rl.bind_approved_plan(
            run_dir, plan_path, {"backend": "powerset", "set_id": "set-1"},
        )
        self.assertEqual(canonical, run_dir / "epoch0" / "plan.json")

    def test_main_reports_failure_status_on_command_error(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        err = rl.CommandError(["fake"], returncode=2, stderr="bad", description="build deep-search plan")
        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t"]
        stdout = io.StringIO()
        try:
            with mock.patch("search_harness.run_search_harness", side_effect=err), \
                 contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as ctx:
                    rl.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv
        self.assertEqual(json.loads(stdout.getvalue())["status"], "failed")


class TestFetchJd(unittest.TestCase):
    """URL->JD front-end that lets $search deep mode accept a job-posting URL."""

    def test_extract_drops_chrome_keeps_content_and_title(self):
        html = (
            "<html><head><title> Senior Backend Engineer - Acme </title><style>.x{}</style></head>"
            "<body><nav>Home About</nav><h1>Senior Backend Engineer</h1>"
            "<p>Build production APIs.</p><ul><li>5+ years Python</li><li>Postgres</li></ul>"
            "<script>var x=1;</script><footer>copyright 2026</footer></body></html>"
        )
        text, title = fj.extract(html)
        self.assertEqual(title, "Senior Backend Engineer - Acme")
        self.assertIn("Senior Backend Engineer", text)
        self.assertIn("5+ years Python", text)
        self.assertIn("Postgres", text)
        # script/style/nav/footer chrome is dropped
        for junk in ("var x=1", "copyright", "Home About"):
            self.assertNotIn(junk, text)

    def test_extract_separates_block_elements(self):
        text, _ = fj.extract("<p>one</p><p>two</p><li>three</li>")
        # block boundaries prevent words running together
        self.assertNotIn("onetwo", text)
        self.assertEqual([ln for ln in text.splitlines() if ln], ["one", "two", "three"])

    def test_extracts_hiring_company_and_website_from_json_ld(self):
        html = '''<script type="application/ld+json">{
          "@type":"JobPosting","hiringOrganization":{"name":"Firecrawl",
          "sameAs":"https://www.firecrawl.dev/"}}</script>'''
        metadata = fj.extract_company_metadata(html, "https://jobs.ashbyhq.com/firecrawl/id")
        self.assertEqual(metadata["company_name"], "Firecrawl")
        self.assertEqual(metadata["company_website_url"], "https://www.firecrawl.dev/")

    def test_company_owned_careers_url_beats_embedded_job_board_link(self):
        html = '<a href="https://jobs.ashbyhq.com/lovable/id">Apply</a>'
        metadata = fj.extract_company_metadata(
            html, "https://lovable.dev/careers/design-engineer")

        self.assertEqual(metadata["company_website_url"], "https://lovable.dev")
        self.assertNotIn("https://jobs.ashbyhq.com/lovable/id",
                         metadata["company_website_urls"])

    def test_extracts_only_linkedin_company_slug(self):
        html = ('<a href="https://www.linkedin.com/in/person">Person</a>'
                '<a href="https://linkedin.com/company/lovable-dev/about">Company</a>')
        self.assertEqual(
            fj.extract_linkedin_company_slug(html, "https://lovable.dev"), "lovable-dev")

    def test_main_writes_jd_and_source_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            html = "<html><head><title>Role X</title></head><body><p>" + ("do the work " * 100) + "</p></body></html>"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/job", "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch", return_value=(html, "https://example.test/job")):
                    fj.main()  # status ok -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())
            self.assertIn("do the work", out.read_text())
            src = json.loads((Path(d) / "source.json").read_text())
            self.assertEqual(src["requested_url"], "https://example.test/job")
            self.assertEqual(src["source_url"], "https://example.test/job")
            self.assertEqual(src["source_title"], "Role X")
            self.assertIn("company_website_urls", src)
            self.assertIn("fetched_at", src)

    def test_main_thin_content_still_writes_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/js", "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch", return_value=("<html><body>App</body></html>", "https://example.test/js")):
                    fj.main()  # thin is not a failure -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())  # thin content is still written

    def test_main_uses_ashby_api_when_page_fetch_fails(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            url = "https://jobs.ashbyhq.com/acme/2e718684-4f75-4a99-8d6b-3b6bd44e4228"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", url, "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch_ashby",
                                       return_value=(("Role X\n\n" + "work " * 100), "Role X")), \
                     mock.patch.object(fj, "fetch", side_effect=fj.urllib.error.URLError("blocked")):
                    fj.main()
            finally:
                sys.argv = argv

            self.assertIn("Role X", out.read_text())
            source = json.loads((Path(d) / "source.json").read_text())
            self.assertEqual(source["source_url"], url)
            self.assertEqual(source["via"], "ashby_posting_api")

    def test_deep_search_loop_requires_exactly_one_jd_input(self):
        with tempfile.TemporaryDirectory() as d:
            argv = sys.argv
            # neither jd-file nor jd-url
            sys.argv = ["loop", "--run-dir", str(Path(d) / "r"), "--created-at", "t"]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    rl.main()
                self.assertEqual(ctx.exception.code, 2)
                # both jd-file and jd-url
                sys.argv = ["loop", "--jd-file", "x.txt", "--jd-url", "http://y", "--run-dir", str(Path(d) / "r2"), "--created-at", "t"]
                with self.assertRaises(SystemExit) as ctx2:
                    rl.main()
                self.assertEqual(ctx2.exception.code, 2)
            finally:
                sys.argv = argv

    def test_deep_search_loop_jd_url_fetches_before_loop(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            build_cmd = None
            argv = sys.argv
            sys.argv = ["loop", "--jd-url", "https://example.test/job", "--run-dir", str(run_dir), "--created-at", "t"]
            try:
                def fake_fetch(cmd, *, expected_paths=None, description=None):
                    self.assertEqual(description, "fetch_jd URL->JD")
                    (run_dir / "jd.txt").write_text(
                        "Senior Backend Engineer\n\n" + ("Build high-throughput APIs. " * 20))
                    (run_dir / "source.json").write_text(json.dumps({
                        "requested_url": "https://example.test/job",
                        "source_url": "https://example.test/job",
                    }))

                def fake_harness_command(cmd, *, expected_paths=None, description=None):
                    nonlocal build_cmd
                    if description == "build deep-search plan":
                        build_cmd = [str(part) for part in cmd]
                        (run_dir / "epoch0" / "plan.json").write_text(
                            json.dumps({"traits": {"must_have": []}}))
                    elif description == "generate initial search queries":
                        (run_dir / "queries.json").write_text(
                            json.dumps([{"key": "q00", "query": "Backend engineer"}]))
                    else:
                        self.fail(f"unexpected child command before review: {description}")

                with mock.patch.object(rl, "run", side_effect=fake_fetch), \
                     mock.patch("search_harness.run_checked", side_effect=fake_harness_command), \
                     mock.patch.object(rl, "resolve_retrieval_identity",
                                       return_value=({"backend": "powerset", "set_id": "x"}, "x", "db")), \
                     mock.patch.object(rl, "probe_populations", return_value={"floors": []}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    rl.main()  # returns at awaiting_plan_approval (no SystemExit)
            finally:
                sys.argv = argv
            self.assertTrue((run_dir / "jd.txt").exists())  # URL was fetched to jd.txt before the loop
            self.assertIn("--source-url", build_cmd)
            self.assertEqual(build_cmd[build_cmd.index("--source-url") + 1], "https://example.test/job")
            self.assertIn("--source-json", build_cmd)

    def test_deep_search_loop_rejects_thin_fetched_jd(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            argv = sys.argv
            sys.argv = ["loop", "--jd-url", "https://example.test/js-job", "--run-dir", str(run_dir), "--created-at", "t"]
            try:
                # a JS-rendered page fetches to near-empty text: the loop must stop, not build a garbage plan
                def fake_run(cmd, **kw):
                    (run_dir / "jd.txt").write_text("Apply now\n")
                    (run_dir / "source.json").write_text(json.dumps({
                        "requested_url": "https://example.test/js-job",
                        "source_url": "https://example.test/js-job",
                    }))
                with mock.patch.object(rl, "run", side_effect=fake_run):
                    with self.assertRaises(SystemExit) as ctx:
                        rl.main()
                self.assertEqual(ctx.exception.code, 1)  # thin JD -> hard fail before sourcing
            finally:
                sys.argv = argv


class TestFetchJDAshby(unittest.TestCase):
    """fetch_ashby early-outs (no network in either case)."""

    def test_non_ashby_host_returns_none(self):
        fj = _load("fetch_jd")
        self.assertIsNone(fj.fetch_ashby("https://jobs.lever.co/acme/2e718684-4f75-4a99-8d6b-3b6bd44e4228"))

    def test_ashby_url_without_job_uuid_returns_none(self):
        fj = _load("fetch_jd")
        self.assertIsNone(fj.fetch_ashby("https://jobs.ashbyhq.com/supabase"))


class TestLoopParserDefaults(unittest.TestCase):
    def test_loop_parser_defaults_pond_models(self):
        dsl = _load("deep_search_loop")
        # parse defaults directly via a fresh parser run
        parser_defaults = None
        real_parse = argparse.ArgumentParser.parse_args

        def spy(self, *a, **k):
            nonlocal parser_defaults
            parser_defaults = real_parse(self, ["--jd-file", "x", "--run-dir", "y", "--created-at", "z"])
            raise SystemExit(0)

        with unittest.mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
            with self.assertRaises(SystemExit):
                dsl.main()
        self.assertEqual(parser_defaults.query_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.query_reasoning_effort, "medium")
        self.assertEqual(parser_defaults.expand_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.expand_reasoning_effort, "medium")
        self.assertEqual(parser_defaults.filter_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.filter_reasoning_effort, "none")
        self.assertEqual(parser_defaults.rerank_model, "gpt-5.6-luna")
        self.assertEqual(parser_defaults.rerank_reasoning_effort, "medium")


if __name__ == "__main__":
    unittest.main()
