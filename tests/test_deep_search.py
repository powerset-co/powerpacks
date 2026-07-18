"""Unit tests for the $search deep-mode consensus + ground-truth-gap primitives (pure functions)."""
from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import json
import os
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


jc = _load("judge_consensus")
su = _load("subprocess_utils")
sg = _load("score_ground_truth_gaps")
dv = _load("diversify_probe_bm25")
dj = _load("decompose_jd")
ea = _load("expand_from_anchor")
bei = _load("build_eval_inputs")
tc = _load("triage_candidates")
cj_judge = _load("codex_judge")
rs = _load("robust_source")
rl = _load("deep_search_loop")
fj = _load("fetch_jd")
ms = _load("micro_sort_shortlist")
pc = _load("plan_critic")
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

    def test_run_wide_search_imports_sibling_subprocess_utils(self):
        self.assertTrue(hasattr(_load("run_wide_search"), "run_checked"))


class TestRunWideSearchPartialFailure(unittest.TestCase):
    """A single flaky probe must NOT abort the whole wide search (robustness: tolerate dead probes,
    fail only when none survive)."""

    def test_prepare_returns_none_on_probe_failure_instead_of_raising(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            # rsg.CommandError (not su.CommandError): run_wide_search's except binds its own import.
            boom = rsg.CommandError(["prepare"], returncode=1, description="prepare probe q00")
            with mock.patch.object(rsg, "run_checked", side_effect=boom):
                result = rsg._prepare(
                    {"key": "q00", "query": "flaky", "required_location": "", "location_filters": {}},
                    probe_dir, ".env", True, "powerset", None,
                )
        self.assertIsNone(result)  # tolerated (dropped by ok_seeds), not propagated

    def test_prepare_returns_payload_path_on_success(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            prep = probe_dir / "prep" / "sub"
            prep.mkdir(parents=True)
            (prep / "expand_search_request.json").write_text("{}")
            with mock.patch.object(rsg, "run_checked", return_value=None):
                dest = rsg._prepare(
                    {"key": "q00", "query": "ok", "required_location": "", "location_filters": {}},
                    probe_dir, ".env", True, "powerset", None,
                )
            self.assertEqual(dest, probe_dir / "payload.json")
            self.assertTrue((probe_dir / "payload.json").exists())

    def test_prepare_rejects_seed_without_reviewed_location_metadata(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            with mock.patch.object(rsg, "run_checked") as run_checked:
                result = rsg._prepare(
                    {"key": "q00", "query": "unbound"},
                    probe_dir, ".env", True, "powerset", None,
                )
        self.assertIsNone(result)
        run_checked.assert_not_called()

    def test_prepare_rejects_required_location_with_empty_filters_before_spend(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            with mock.patch.object(rsg, "run_checked") as run_checked:
                result = rsg._prepare(
                    {
                        "key": "q00", "query": "unbound",
                        "required_location": "San Francisco", "location_filters": {},
                    },
                    probe_dir, ".env", True, "powerset", None,
                )
        self.assertIsNone(result)
        run_checked.assert_not_called()

    def test_prepare_overwrites_model_geo_with_required_location(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            prep = probe_dir / "prep" / "sub"
            prep.mkdir(parents=True)
            (prep / "expand_search_request.json").write_text(json.dumps({
                "role_search_filters": {
                    "cities": ["New York"],
                    "countries": ["United States"],
                    "company_cities": ["Boston"],
                    "company_metro_areas": ["New York Metropolitan Area"],
                }
            }))
            seed = {
                "key": "q00", "query": "finance builder", "required_location": "San Francisco Bay Area",
                "location_filters": {"metro_areas": ["San Francisco Bay Area"]},
            }
            with mock.patch.object(rsg, "run_checked", return_value=None):
                dest = rsg._prepare(seed, probe_dir, ".env", True, "powerset", None)

            filters = json.loads(dest.read_text())["role_search_filters"]
            self.assertEqual(filters["metro_areas"], ["San Francisco Bay Area"])
            self.assertNotIn("cities", filters)
            self.assertNotIn("countries", filters)
            self.assertNotIn("company_cities", filters)
            self.assertNotIn("company_metro_areas", filters)

    def test_prepare_approved_global_clears_model_geo(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            prep = probe_dir / "prep" / "sub"
            prep.mkdir(parents=True)
            (prep / "expand_search_request.json").write_text(json.dumps({
                "role_search_filters": {"cities": ["New York"]}
            }))
            seed = {"key": "q00", "query": "finance builder", "required_location": "", "location_filters": {}}
            with mock.patch.object(rsg, "run_checked", return_value=None):
                dest = rsg._prepare(seed, probe_dir, ".env", True, "powerset", None)

            filters = json.loads(dest.read_text())["role_search_filters"]
            self.assertNotIn("cities", filters)

    def test_prepare_rejects_hard_filters_that_bypass_required_geo(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            prep = probe_dir / "prep" / "sub"
            prep.mkdir(parents=True)
            (prep / "expand_search_request.json").write_text(json.dumps({
                "role_search_filters": {"hard_filters": {"field": "role_track", "op": "Eq", "value": "Finance"}}
            }))
            seed = {
                "key": "q00", "query": "finance", "required_location": "San Francisco Bay Area",
                "location_filters": {"metro_areas": ["San Francisco Bay Area"]},
            }
            with mock.patch.object(rsg, "run_checked", return_value=None):
                dest = rsg._prepare(seed, probe_dir, ".env", True, "powerset", None)
        self.assertIsNone(dest)

    def test_run_returns_false_on_probe_failure_instead_of_raising(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            (probe_dir / "payload.json").write_text(json.dumps({"role_search_filters": {}}))
            boom = rsg.CommandError(["run"], returncode=1, description="run probe q00")
            with mock.patch.object(rsg, "run_checked", side_effect=boom):
                ok = rsg._run(
                    {"key": "q00", "query": "flaky", "required_location": "", "location_filters": {}},
                    probe_dir, "set-123", ".env", 200, 6000, "powerset", None,
                )
        self.assertFalse(ok)  # tolerated (build_union skips the missing ledger), not propagated

    def test_run_returns_true_on_success(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            (probe_dir / "payload.json").write_text(json.dumps({"role_search_filters": {}}))
            with mock.patch.object(rsg, "run_checked", return_value=None):
                ok = rsg._run(
                    {"key": "q00", "query": "ok", "required_location": "", "location_filters": {}},
                    probe_dir, None, ".env", 200, 6000, "powerset", None,
                )
        self.assertTrue(ok)

    def test_run_discards_completed_ledger_before_fresh_payload(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            (probe_dir / "payload.json").write_text(json.dumps({"role_search_filters": {}}))
            ledger = probe_dir / "ledger.json"
            ledger.write_text(json.dumps({"steps": [{"id": "execute_role_search", "status": "completed"}]}))

            def fake_run(*args, **kwargs):
                self.assertFalse(ledger.exists())
                ledger.write_text(json.dumps({"artifacts": {}}))

            with mock.patch.object(rsg, "run_checked", side_effect=fake_run):
                ok = rsg._run(
                    {"key": "q00", "query": "fresh", "required_location": "", "location_filters": {}},
                    probe_dir, None, ".env", 200, 6000, "powerset", None,
                )
        self.assertTrue(ok)

    def test_run_reasserts_required_location_after_diversification(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            payload = probe_dir / "payload.json"
            payload.write_text(json.dumps({"role_search_filters": {"cities": ["New York"]}}))
            seed = {
                "key": "q00", "query": "finance", "required_location": "San Francisco Bay Area",
                "location_filters": {"metro_areas": ["San Francisco Bay Area"]},
            }
            with mock.patch.object(rsg, "run_checked", return_value=None):
                ok = rsg._run(seed, probe_dir, None, ".env", 200, 6000, "local", "x.duckdb")

            filters = json.loads(payload.read_text())["role_search_filters"]
        self.assertTrue(ok)
        self.assertEqual(filters["metro_areas"], ["San Francisco Bay Area"])
        self.assertNotIn("cities", filters)

    def test_union_preserves_canonical_headline_and_position_title_for_anchors(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            probe_dir = run_dir / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            profiles = run_dir / "llm_profiles.jsonl"
            profiles.write_text(json.dumps({
                "person_id": "p1",
                "headline": "Strategic finance builder",
                "positions": [{
                    "position_title": "Director of FP&A",
                    "company_name": "CloudCo",
                    "company_description": "GPU cloud platform",
                }],
            }) + "\n")
            results = run_dir / "results.csv"
            with results.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=[
                    "rank", "person_id", "name", "linkedin_url", "current_titles",
                    "current_companies", "location",
                ])
                writer.writeheader()
                writer.writerow({
                    "rank": 1, "person_id": "p1", "name": "Fran",
                    "current_titles": "Director of FP&A", "current_companies": "CloudCo",
                    "location": "San Rafael, California, United States",
                })
            retrieval = run_dir / "retrieval.json"
            retrieval.write_text(json.dumps({
                "namespace": "aleph_people_v1",
                "candidates": [{
                    "person_id": "p1", "city": "San Rafael", "state": "California",
                    "country": "United States", "macro_region": "Americas",
                    "metro_areas": ["San Francisco Bay Area"],
                }],
            }))
            (probe_dir / "ledger.json").write_text(json.dumps({
                "artifacts": {
                    "llm_profiles_path": str(profiles), "csv": str(results),
                    "retrieval_artifact": str(retrieval),
                }
            }))

            union = rsg.build_union(run_dir, [{"key": "q00", "query": "finance"}], keep=10)

        self.assertEqual(union[0]["headline"], "Strategic finance builder")
        self.assertEqual(union[0]["positions"][0]["position_title"], "Director of FP&A")
        self.assertEqual(union[0]["location_fields"]["city"], "San Rafael")
        self.assertEqual(union[0]["location_fields"]["metro_areas"], ["San Francisco Bay Area"])

    def test_union_rejects_missing_or_corrupt_structured_retrieval_provenance(self):
        rsg = _load("run_wide_search")
        for kind in ("missing", "corrupt"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                run_dir = Path(td)
                probe_dir = run_dir / "probes" / "q00"
                probe_dir.mkdir(parents=True)
                results = run_dir / "results.csv"
                with results.open("w", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=[
                        "rank", "person_id", "name", "linkedin_url", "current_titles",
                        "current_companies", "location",
                    ])
                    writer.writeheader()
                    writer.writerow({
                        "rank": 1, "person_id": "p1", "name": "Fran",
                        "location": "San Francisco, California, United States",
                    })
                retrieval = run_dir / "retrieval.json"
                if kind == "corrupt":
                    retrieval.write_text("{")
                (probe_dir / "ledger.json").write_text(json.dumps({
                    "artifacts": {
                        "csv": str(results),
                        "retrieval_artifact": str(retrieval),
                    },
                }))

                union = rsg.build_union(
                    run_dir, [{"key": "q00", "query": "finance"}], keep=10,
                )

                self.assertEqual(union, [])

    def test_union_marks_missing_candidate_geo_as_authoritative_unknown(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            probe_dir = run_dir / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            results = run_dir / "results.csv"
            with results.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["rank", "person_id", "location"])
                writer.writeheader()
                writer.writerow({
                    "rank": 1, "person_id": "p1",
                    "location": "San Francisco, California, United States",
                })
            retrieval = run_dir / "retrieval.json"
            retrieval.write_text(json.dumps({"candidates": [{"person_id": "p1"}]}))
            (probe_dir / "ledger.json").write_text(json.dumps({
                "artifacts": {"csv": str(results), "retrieval_artifact": str(retrieval)},
            }))

            union = rsg.build_union(
                run_dir, [{"key": "q00", "query": "finance"}], keep=10,
            )

        self.assertEqual(union[0]["location_fields"], {})
        self.assertEqual(
            ls.location_fit({"metro_areas": ["San Francisco Bay Area"]}, union[0]),
            "unknown",
        )


class TestLocalBackendThreading(unittest.TestCase):
    """--backend/--db threading through the deep-search sourcing chain (post search-backend fold)."""

    class _HaltAfterParse(Exception):
        pass

    def test_backend_args_local_vs_powerset(self):
        rsg = _load("run_wide_search")
        self.assertEqual(rsg._backend_args("local", "x.duckdb"), ["--backend", "local", "--db", "x.duckdb"])
        self.assertEqual(rsg._backend_args("powerset", None), [])

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

    def test_deep_search_loop_parser_exposes_sendable_threshold(self):
        args = self._parse_with_real_parser(
            rl,
            ["loop", "--jd-file", "jd.txt", "--run-dir", "run", "--created-at", "t",
             "--sendable-threshold", "0.61"],
        )
        self.assertEqual(args.sendable_threshold, 0.61)

    def test_robust_source_parser_accepts_local_backend(self):
        args = self._parse_with_real_parser(
            rs,
            ["robust", "--jd-file", "jd.txt", "--plan", "plan.json", "--run-dir", "run",
             "--backend", "local", "--db", "x.duckdb"],
        )
        self.assertEqual(args.backend, "local")
        self.assertEqual(args.db, "x.duckdb")


class TestDecomposeJd(unittest.TestCase):
    def test_parse_seeds_strings_and_objects(self):
        self.assertEqual(dj.parse_seeds({"seeds": ["a", "b"]}),
                         [{"key": "q00", "query": "a"}, {"key": "q01", "query": "b"}])
        self.assertEqual(dj.parse_seeds({"seeds": [{"query": "x"}, {"seed": "y"}]}),
                         [{"key": "q00", "query": "x"}, {"key": "q01", "query": "y"}])

    def test_parse_seeds_truncates_and_skips_empty(self):
        out = dj.parse_seeds({"seeds": ["a", "", "b", "c"]}, n=2)
        self.assertEqual([s["query"] for s in out], ["a", "b"])

    def test_parse_seeds_raises_on_empty(self):
        with self.assertRaises(ValueError):
            dj.parse_seeds({"seeds": []})

    def test_build_messages_includes_jd_and_count(self):
        msgs = dj.build_messages("Build RAG systems", 7)
        self.assertIn("Build RAG systems", msgs[-1]["content"])
        self.assertIn("7", msgs[-1]["content"])

    def test_non_null_location_applies_to_every_seed(self):
        seeds = [{"key": f"q{i:02d}", "query": f"seed {i}."} for i in range(8)]
        geo = dj.apply_location_scope(
            seeds, "San Francisco Bay Area", {"metro_areas": ["San Francisco Bay Area"]},
        )
        self.assertEqual(geo, 8)
        self.assertTrue(all(seed["required_location"] == "San Francisco Bay Area" for seed in seeds))
        self.assertTrue(all("based in" not in seed["query"] for seed in seeds))

    def test_location_mix_noop_when_empty(self):
        seeds = [{"key": "q00", "query": "seed 0."}]
        self.assertEqual(dj.apply_location_scope(seeds, "", {}), 0)
        self.assertEqual(dj.apply_location_scope(seeds, "   ", {}), 0)
        self.assertEqual(seeds[0]["query"], "seed 0.")
        self.assertEqual(seeds[0]["required_location"], "")
        self.assertEqual(seeds[0]["location_filters"], {})

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
            {"cities": ["London"], "countries": ["United Kingdom"]},
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
            {"cities": ["New York"], "countries": ["United States"]},
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
        self.assertEqual(filters, {"cities": ["New York"], "countries": ["United States"]})
        self.assertEqual(
            ls.location_scope_from_plan({
                "search_scope": {"location": "New York City", "filters": filters},
            }),
            ("New York City", filters),
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
            {"cities": ["Perth"], "countries": ["Australia"]},
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


class TestMicroSortShortlist(unittest.TestCase):
    def _rows(self, scores):
        return [{"person_id": f"p{i:02d}", "name": f"P{i}", "mean_score": s} for i, s in enumerate(scores)]

    def test_banding_packs_adjacent_and_passes_through_low_scores(self):
        rows = self._rows([0.82, 0.81, 0.80, 0.74, 0.73, 0.71, 0.44, 0.41])
        batches, passthrough = ms.build_batches(rows)
        self.assertEqual(len(batches), 1)  # 0.8 + 0.7 bands pack under the cap
        self.assertEqual(len(batches[0]), 6)
        self.assertEqual([r["person_id"] for r in passthrough], ["p06", "p07"])  # <0.5 untouched

    def test_oversized_band_splits_into_subbatches(self):
        rows = self._rows([0.80 + i * 0.001 for i in range(45)])  # one 0.8 band of 45
        batches, passthrough = ms.build_batches(rows)
        self.assertEqual(passthrough, [])
        self.assertEqual([len(b) for b in batches], [20, 20, 5])  # split at BATCH_CAP, merged in order

    def test_tiny_band_keeps_original_order(self):
        rows = self._rows([0.9, 0.9])  # below MIN_BAND_SIZE
        batches, passthrough = ms.build_batches(rows)
        self.assertEqual([len(b) for b in batches], [2])  # flushed as-is, never split

    def test_validate_ordering_dedupes_and_restores_missing(self):
        batch = self._rows([0.8, 0.8, 0.8])
        out = ms.validate_ordering(["p02", "p02", "ghost", "p00"], batch, "person_id")
        self.assertEqual([r["person_id"] for r in out], ["p02", "p00", "p01"])  # dupe+ghost dropped, missing appended

    def test_cap_batches_bounds_runtime_deterministically(self):
        batches = [[{"person_id": f"b{i}p{j}"} for j in range(20)] for i in range(15)]
        kept, dropped = ms.cap_batches(batches, 10)
        self.assertEqual(len(kept), 10)          # <=10 LLM calls, always
        self.assertEqual(len(dropped), 100)      # 5 batches x 20 keep score order
        self.assertEqual(dropped[0]["person_id"], "b10p0")  # order preserved
        kept2, dropped2 = ms.cap_batches(batches[:3], 10)
        self.assertEqual((len(kept2), dropped2), (3, []))  # under cap: untouched


class TestPlanCritic(unittest.TestCase):
    def test_conjunctive_core_group_is_flagged(self):
        # Measured on the audited benchmark: an all-of-3 group cut a validated 22-person
        # shortlist to 1. Every conjunction must surface at Review.
        plan = {"hire_stage": "founding_early",
                "traits": {"must_have": [{"trait": c, "tier": "core"} for c in "abcd"]},
                "core_groups": [{"name": "mega", "all_of": ["a", "b", "c", "d"]}]}
        for traits in (["a", "b"], ["a", "b", "c"], ["a", "b", "c", "d"]):
            with self.subTest(n=len(traits)):
                plan["core_groups"] = [{"name": "mega", "all_of": traits}]
                issues = pc.deterministic_checks(plan)
                self.assertTrue(any(f"ALL {len(traits)} traits" in i for i in issues), issues)
        # per-trait groups (the measured default) pass clean
        plan["core_groups"] = [{"name": f"g{c}", "all_of": [c]} for c in "abcd"]
        self.assertEqual([i for i in pc.deterministic_checks(plan) if "ALL" in i], [])

    def test_empty_powerset_set_id_surfaces_at_review(self):
        plan = {"hire_stage": "founding_early", "route": "deep",
                "set_scope": {"set_id": ""},
                "traits": {"must_have": [{"trait": "x", "tier": "core"}]},
                "core_groups": [{"name": "g", "all_of": ["x"]}]}
        issues = pc.deterministic_checks(plan, backend="powerset")
        self.assertTrue(any("set_scope.set_id is empty" in i for i in issues), issues)
        self.assertFalse(any("set_scope.set_id is empty" in i
                             for i in pc.deterministic_checks(plan, backend="local")))

    def test_critic_omits_temperature_for_reasoning_model_families(self):
        self.assertFalse(pc.supports_custom_temperature("gpt-5.4"))
        self.assertFalse(pc.supports_custom_temperature("o4-mini"))
        self.assertTrue(pc.supports_custom_temperature("gpt-4o"))

    def test_deterministic_checks_flag_off_enum_stage_and_missing_core(self):
        issues = pc.deterministic_checks({
            "hire_stage": "growth",
            "search_scope": {"location": None, "filters": {}},
            "traits": {"must_have": [{"trait": "x", "tier": "table_stakes"}]},
        })
        self.assertEqual(len(issues), 2)
        self.assertIn("off-enum", issues[0])
        self.assertIn("core", issues[1])

    def test_deterministic_checks_pass_valid_plan(self):
        issues = pc.deterministic_checks({"hire_stage": "founding_early",
                                          "search_scope": {"location": None, "filters": {}},
                                          "traits": {"must_have": [{"trait": "x", "tier": "core"}]},
                                          "core_groups": [{"name": "default", "all_of": ["x"]}]})
        self.assertEqual(issues, [])


class TestExpandFromAnchor(unittest.TestCase):
    def test_anchor_to_seed_from_profile(self):
        prof = {"name": "Ada", "headline": "AI Engineer at Notion",
                "positions": [{"position_title": "AI Engineer", "company_name": "Notion",
                               "company_description": "productivity software"}]}
        plan = {"normalized_archetype": "AI infrastructure engineer",
                "search_scope": {
                    "location": "San Francisco Bay Area",
                    "filters": {"metro_areas": ["San Francisco Bay Area"]},
                },
                "traits": {"must_have": []}}
        seed = ea.anchor_to_seed(prof, plan)
        self.assertEqual(seed["anchor"], "Ada")
        self.assertIn("AI infrastructure engineer", seed["query"])
        self.assertIn("AI Engineer", seed["query"])
        self.assertIn("proven-strong match", seed["query"])
        self.assertEqual(seed["required_location"], "San Francisco Bay Area")
        self.assertNotIn("based in", seed["query"])
        self.assertNotIn("productivity software", seed["query"])

    def test_anchor_to_seed_fallback_and_none(self):
        self.assertIn("Eng", ea.anchor_to_seed({"current_title": "Eng", "current_company": "Acme"})["query"])
        self.assertIsNone(ea.anchor_to_seed({"name": "x"}))  # no usable text

    def test_reviewed_global_anchor_carries_explicit_empty_scope(self):
        seed = ea.anchor_to_seed(
            {"name": "Fran", "current_title": "Finance Lead"},
            {"job_title": "Finance Lead", "search_scope": {"location": None, "filters": {}}, "traits": {}},
        )
        self.assertIn("required_location", seed)
        self.assertEqual(seed["required_location"], "")

    def test_cli_requires_approved_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            anchors = root / "anchors.json"
            anchors.write_text("[]")
            argv = sys.argv
            sys.argv = ["expand", "--anchors", str(anchors), "--out", str(root / "seeds.json")]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    ea.main()
                self.assertEqual(ctx.exception.code, 2)
            finally:
                sys.argv = argv

    def test_finance_anchor_uses_plan_and_judged_core_evidence(self):
        plan = {
            "job_title": "Strategic Finance Lead",
            "normalized_archetype": "strategic finance leader",
            "search_scope": {
                "location": "San Francisco Bay Area",
                "filters": {"metro_areas": ["San Francisco Bay Area"]},
            },
            "traits": {"must_have": [
                {"trait": "build operating P&L models", "tier": "core"},
                {"trait": "translate infrastructure costs into pricing", "tier": "core"},
                {"trait": "prepare board materials", "tier": "table_stakes"},
            ]},
        }
        anchor = {
            "name": "Fran",
            "current_title": "Head of Strategic Finance",
            "positions": [{
                "position_title": "Director of FP&A",
                "company_name": "CloudCo",
                "company_description": "GPU cloud engineers building distributed systems",
            }],
            "per_judge": {"loop": {"must_have": [
                {"trait": "build operating P&L models", "status": "doing_now",
                 "evidence": "Built the first operating model and unit economics from scratch."},
                {"trait": "translate infrastructure costs into pricing", "status": "capable",
                 "evidence": "Adjacent exposure only."},
                {"trait": "prepare board materials", "status": "experienced",
                 "evidence": "Prepared board decks."},
            ]}},
        }

        seed = ea.anchor_to_seed(anchor, plan)

        self.assertIn("strategic finance leader", seed["query"])
        self.assertIn("Head of Strategic Finance", seed["query"])
        self.assertIn("Director of FP&A", seed["query"])
        self.assertIn("build operating P&L models", seed["query"])
        self.assertIn("Built the first operating model", seed["query"])
        self.assertEqual(seed["required_location"], "San Francisco Bay Area")
        self.assertEqual(seed["location_filters"], {"metro_areas": ["San Francisco Bay Area"]})
        self.assertNotIn("based in", seed["query"])
        self.assertNotIn("Engineer whose", seed["query"])
        self.assertNotIn("GPU cloud engineers", seed["query"])
        self.assertNotIn("Adjacent exposure only", seed["query"])
        self.assertNotIn("Prepared board decks", seed["query"])

    def test_build_seeds_takes_top_k_by_score_and_keys(self):
        recs = [{"name": "lo", "headline": "h1", "mean_score": 0.3},
                {"name": "hi", "headline": "h2", "mean_score": 0.9}]
        seeds = ea.build_seeds(recs, top_k=1)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["anchor"], "hi")
        self.assertEqual(seeds[0]["key"], "anchor00")

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


class TestJudgeConsensus(unittest.TestCase):
    def _judges(self):
        # 3 judges; A is unanimous strong, B gated by 2/3 (too_senior), C is a 2/3 in-band split.
        return {
            "j1": [
                {"person_id": "A", "name": "Ada", "in_band": True, "verdict": "top_tier", "score": 0.9, "seniority_fit": "in_band"},
                {"person_id": "B", "name": "Boss", "in_band": False, "verdict": "out", "score": 0.2, "seniority_fit": "too_senior"},
                {"person_id": "C", "name": "Cam", "in_band": True, "verdict": "high_potential", "score": 0.6, "seniority_fit": "in_band"},
            ],
            "j2": [
                {"person_id": "A", "name": "Ada", "in_band": True, "verdict": "high_potential", "score": 0.8, "seniority_fit": "in_band"},
                {"person_id": "B", "name": "Boss", "in_band": False, "verdict": "out", "score": 0.3, "seniority_fit": "too_senior"},
                {"person_id": "C", "name": "Cam", "in_band": True, "verdict": "high_potential", "score": 0.5, "seniority_fit": "in_band"},
            ],
            "j3": [
                {"person_id": "A", "name": "Ada", "in_band": True, "verdict": "top_tier", "score": 1.0, "seniority_fit": "in_band"},
                {"person_id": "B", "name": "Boss", "in_band": True, "verdict": "high_potential", "score": 0.7, "seniority_fit": "in_band"},
                {"person_id": "C", "name": "Cam", "in_band": False, "verdict": "out", "score": 0.3, "seniority_fit": "wrong_track"},
            ],
        }

    def test_strong_requires_majority_inband_and_notout(self):
        rows, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=2, min_notout_votes=2)
        ids = [r["person_id"] for r in strong]
        self.assertIn("A", ids)          # 3/3 in-band, 3/3 not-out
        self.assertIn("C", ids)          # 2/3 in-band, 2/3 not-out
        self.assertNotIn("B", ids)       # only 1/3 in-band -> gated out
        self.assertEqual(ids[0], "A")    # ranked first (highest mean score)

    def test_consensus_fields(self):
        rows, _ = jc.build_consensus(self._judges(), {}, min_inband_votes=2, min_notout_votes=2)
        a = next(r for r in rows if r["person_id"] == "A")
        self.assertEqual(a["inband_votes"], 3)
        self.assertEqual(a["notout_votes"], 3)
        self.assertEqual(a["n_judges"], 3)
        self.assertAlmostEqual(a["mean_score"], 0.9, places=3)
        b = next(r for r in rows if r["person_id"] == "B")
        self.assertEqual(b["gated_votes"], 2)  # two judges said too_senior

    def test_meta_enriches_rows(self):
        meta = {"A": {"person_id": "A", "name": "Ada", "current_company": "Acme", "found_by": ["routing", "scheduler"]}}
        rows, _ = jc.build_consensus(self._judges(), meta, min_inband_votes=2, min_notout_votes=2)
        a = next(r for r in rows if r["person_id"] == "A")
        self.assertEqual(a["current_company"], "Acme")
        self.assertEqual(a["found_by"], ["routing", "scheduler"])

    def test_required_location_excludes_mismatch_and_unknown_from_shortlist(self):
        meta = {
            "A": {
                "person_id": "A", "location": "San Rafael, California, United States",
                "location_fields": {
                    "city": "San Rafael", "state": "California", "country": "United States",
                    "macro_region": "Americas", "metro_areas": ["San Francisco Bay Area"],
                },
            },
            "B": {"person_id": "B", "location": "New York, New York, United States"},
            "C": {"person_id": "C", "location": None},
        }
        rows, strong = jc.build_consensus(
            self._judges(),
            meta,
            min_inband_votes=1,
            min_notout_votes=1,
            required_location="San Francisco, CA",
            required_location_filters={"metro_areas": ["San Francisco Bay Area"]},
        )

        self.assertEqual([row["person_id"] for row in strong], ["A"])
        fits = {row["person_id"]: row["location_fit"] for row in rows}
        self.assertEqual(fits, {"A": "match", "B": "mismatch", "C": "unknown"})
        self.assertEqual(
            ls.location_fit(
                {"metro_areas": ["San Francisco Bay Area"]},
                "San Rafael, California, United States",
            ),
            "unknown",
        )
        self.assertEqual(next(row for row in rows if row["person_id"] == "A")["location_fields"]["city"], "San Rafael")


class TestCoreGate(unittest.TestCase):
    CORE = {"distributed systems"}  # normalized core-trait text

    def _judges(self):
        # GEM: lower score but DOES the core domain (doing_now). WRONG: higher score, strong+senior
        # but only `capable` on the core (adjacent/wrong-domain). GATED: does the core but too_senior.
        return {"j1": [
            {"person_id": "GEM", "name": "Gem", "in_band": True, "verdict": "high_potential",
             "score": 0.6, "seniority_fit": "in_band",
             "must_have": [{"trait": "Distributed Systems", "status": "doing_now"}]},
            {"person_id": "WRONG", "name": "Wrong", "in_band": True, "verdict": "top_tier",
             "score": 0.9, "seniority_fit": "in_band",
             "must_have": [{"trait": "distributed systems", "status": "capable"}]},
            {"person_id": "GATED", "name": "Gated", "in_band": False, "verdict": "out",
             "score": 0.8, "seniority_fit": "too_senior",
             "must_have": [{"trait": "distributed systems", "status": "doing_now"}]},
        ]}

    def test_core_traits_from_plan_reads_core_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.json"
            p.write_text(json.dumps({"traits": {"must_have": [
                {"trait": "Distributed Systems", "tier": "core"},
                {"trait": "leadership", "tier": "table_stakes"}]}}))
            self.assertEqual(jc.core_traits_from_plan(p), {"distributed systems"})

    def test_meets_core_requires_experienced_not_capable(self):
        j = self._judges()["j1"]
        gem = {x["person_id"]: x for x in j}
        self.assertTrue(jc.candidate_meets_core({"j1": gem["GEM"]}, ["j1"], self.CORE))
        self.assertFalse(jc.candidate_meets_core({"j1": gem["WRONG"]}, ["j1"], self.CORE))

    def test_core_gate_excludes_wrong_domain_keeps_gem(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1,
                                       score_threshold=0.40, core_traits=self.CORE)
        ids = [r["person_id"] for r in strong]
        self.assertEqual(ids, ["GEM"])  # WRONG excluded (capable), GATED excluded (too_senior)

    def test_core_gate_passes_when_one_of_multiple_core_traits_met(self):
        judges = {"j1": [
            {"person_id": "ONE", "name": "One", "in_band": True, "verdict": "high_potential",
             "score": 0.7, "seniority_fit": "in_band",
             "must_have": [
                 {"trait": "distributed systems", "status": "experienced"},
                 {"trait": "gpu kernels", "status": "missing"},
             ]},
        ]}
        _, strong = jc.build_consensus(judges, {}, min_inband_votes=1, min_notout_votes=1,
                                       score_threshold=0.40, core_traits={"distributed systems", "gpu kernels"})
        self.assertEqual([r["person_id"] for r in strong], ["ONE"])

    def test_explicit_core_group_requires_all_traits(self):
        judges = {"j1": [
            {"person_id": "PARTIAL", "in_band": True, "verdict": "high_potential",
             "score": 0.8, "seniority_fit": "in_band", "must_have": [
                 {"trait": "distributed systems", "status": "experienced"},
                 {"trait": "control planes", "status": "capable"},
                 {"trait": "inference serving", "status": "missing"},
             ]},
            {"person_id": "ALT", "in_band": True, "verdict": "high_potential",
             "score": 0.7, "seniority_fit": "in_band", "must_have": [
                 {"trait": "distributed systems", "status": "missing"},
                 {"trait": "control planes", "status": "missing"},
                 {"trait": "inference serving", "status": "doing_now"},
             ]},
        ]}
        groups = [{"distributed systems", "control planes"}, {"inference serving"}]
        _, strong = jc.build_consensus(
            judges, {}, min_inband_votes=1, min_notout_votes=1,
            score_threshold=0.40, core_groups=groups,
        )
        self.assertEqual([r["person_id"] for r in strong], ["ALT"])

    def test_no_core_traits_falls_back_to_score_gate(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1,
                                       score_threshold=0.40, core_traits=set())
        # No core-gate: WRONG (0.9, in-band) qualifies on score alone; GATED still out (not in-band).
        self.assertIn("WRONG", [r["person_id"] for r in strong])


class TestScoreGroundTruthGaps(unittest.TestCase):
    def test_recall_precision_and_missed(self):
        gt = {"x", "y", "z"}
        epoch = ["x", "q", "y"]  # finds x,y ; misses z ; q is net-new
        self.assertEqual(sg.recall_at_k(gt, epoch, 10), round(2 / 3, 4))
        self.assertEqual(sg.precision_at_k(gt, epoch, 3), round(2 / 3, 4))
        self.assertEqual(sg.recall_at_k(gt, epoch, 1), round(1 / 3, 4))

    def test_ranked_ids_honors_rank_then_score(self):
        recs = [{"person_id": "a", "rank": 2}, {"person_id": "b", "rank": 1}]
        self.assertEqual(sg.ranked_ids(recs), ["b", "a"])
        recs2 = [{"person_id": "a", "score": 0.1}, {"person_id": "b", "score": 0.9}]
        self.assertEqual(sg.ranked_ids(recs2), ["b", "a"])

    def test_load_records_json_and_jsonl(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.json").write_text(json.dumps([{"person_id": "a"}]), encoding="utf-8")
        (d / "b.jsonl").write_text('{"person_id": "a"}\n{"person_id": "b"}\n', encoding="utf-8")
        self.assertEqual(len(sg.load_records(d / "a.json")), 1)
        self.assertEqual(len(sg.load_records(d / "b.jsonl")), 2)

    def test_main_writes_gaps_and_convergence(self):
        d = Path(tempfile.mkdtemp())
        gt = [{"person_id": "x", "name": "X"}, {"person_id": "y", "name": "Y"}, {"person_id": "z", "name": "Z"}]
        (d / "gt.json").write_text(json.dumps(gt), encoding="utf-8")
        (d / "epoch.jsonl").write_text('{"person_id":"x","rank":1}\n{"person_id":"y","rank":2}\n', encoding="utf-8")
        import sys
        argv = sys.argv
        sys.argv = ["score", "--ground-truth", str(d / "gt.json"), "--epoch-candidates", str(d / "epoch.jsonl"),
                    "--epoch-dir", str(d / "epochs" / "epoch-01"), "--epoch-label", "epoch-01",
                    "--convergence-csv", str(d / "convergence.csv"), "--ks", "10"]
        try:
            sg.main()
        finally:
            sys.argv = argv
        gaps = json.loads((d / "epochs" / "epoch-01" / "gaps.json").read_text())
        self.assertEqual(gaps["overall_recall"], round(2 / 3, 4))
        self.assertEqual(gaps["missed_count"], 1)
        self.assertEqual(gaps["missed"][0]["person_id"], "z")
        with (d / "convergence.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["epoch"], "epoch-01")


class TestDiversifyProbeBm25(unittest.TestCase):
    def _p(self, *bm25):
        return {"role_search_filters": {"semantic_query": "x", "bm25_queries": list(bm25)}}

    def test_shared_terms_are_high_df_lead_terms(self):
        payloads = [
            self._p("distributed systems engineer", "scheduler engineer"),
            self._p("distributed systems engineer", "routing engineer"),
            self._p("distributed systems engineer", "consensus engineer"),
            self._p("distributed systems engineer", "observability engineer"),
        ]
        shared = dv.shared_bm25_terms(payloads, df_threshold=0.35, min_df=1)
        self.assertIn("distributed systems engineer", shared)   # in all 4
        self.assertNotIn("scheduler engineer", shared)          # distinctive, in 1

    def test_diversify_drops_shared_keeps_distinctive(self):
        f = {"semantic_query": "x", "bm25_queries": ["distributed systems engineer", "raft engineer"]}
        out = dv.diversify_filters(f, {"distributed systems engineer"})
        self.assertEqual(out["bm25_queries"], ["raft engineer"])
        self.assertTrue(out["bm25_diversified"])

    def test_min_df_guards_small_sets(self):
        # 2 probes sharing a term: with min_df=3 nothing is dropped (too small to trust)
        payloads = [self._p("ai product engineer", "rag"), self._p("ai product engineer", "agents")]
        self.assertEqual(dv.shared_bm25_terms(payloads, df_threshold=0.35, min_df=3), set())


class TestBuildEvalInputs(unittest.TestCase):
    def test_build_frontier_scores_by_probe_count(self):
        union = [
            {"person_id": "a", "name": "A", "found_by": ["q0", "q1", "q2"]},
            {"person_id": "b", "name": "B", "found_by": ["q0"]},
            {"name": "no-id"},  # dropped
        ]
        front = bei.build_frontier(union)
        self.assertEqual({c["person_id"] for c in front}, {"a", "b"})
        a = next(c for c in front if c["person_id"] == "a")
        self.assertEqual(a["matched_probe_ids"], ["q0", "q1", "q2"])
        self.assertEqual(a["source_rows"][0]["score"], 3.0)
        self.assertEqual(a["candidate_id"], "a")

    def test_build_frontier_single_probe_has_score_one(self):
        front = bei.build_frontier([{"person_id": "b", "found_by": ["q0"]}])
        self.assertEqual(front[0]["source_rows"][0]["score"], 1.0)

    def test_plan_from_obj_shapes_traits_and_scope(self):
        plan = bei.plan_from_obj(
            {"job_title": "MTS", "normalized_archetype": "distsys engineer",
             "hire_stage": "scale", "usable_cutoff": "Senior IC in band.",
             "must_have": ["schedulers", "control plane", ""], "nice_to_have": ["gpus"]},
            set_name="s", set_id="sid", source_url=None, created_at="2026-01-01T00:00:00Z")
        self.assertEqual([t["trait"] for t in plan["traits"]["must_have"]], ["schedulers", "control plane"])
        self.assertEqual(plan["traits"]["nice_to_have"], [{"trait": "gpus", "source": "jd"}])
        self.assertEqual(plan["set_scope"], {"name": "s", "set_id": "sid"})
        self.assertEqual(plan["normalized_archetype"], "distsys engineer")
        self.assertEqual(plan["hire_stage"], "scaling_late")
        self.assertEqual(plan["search_scope"], {"location": None, "filters": {}, "source": "jd"})
        self.assertFalse(plan["retrieval_ran"])

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
                "location": "New York, United States",
                "filters": {"cities": ["New York"], "countries": ["United States"]},
                "source": "jd",
            },
        )
        with self.assertRaisesRegex(ValueError, "conflict|broaden"):
            bei.plan_from_obj(
                {
                    **base, "location": "San Francisco",
                    "location_filters": {"countries": ["Germany"]},
                },
                set_name="s", set_id="sid", source_url=None, created_at="t",
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
                {
                    "cities": ["New York", "Boston", "Chicago"],
                    "countries": ["United States"],
                },
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

    def test_generated_location_rejects_broader_or_wrong_alternatives(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        with self.assertRaisesRegex(ValueError, "conflict|broaden"):
            bei.plan_from_obj(
                {
                    **base, "location": "San Francisco or New York",
                    "location_filters": {"metro_areas": [
                        "San Francisco Bay Area", "New York Metropolitan Area",
                        "Boston Metropolitan Area",
                    ]},
                },
                set_name="s", set_id="sid", source_url=None, created_at="t",
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

    def test_plan_from_obj_extracts_only_explicit_jd_preferences_below_user(self):
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
        self.assertEqual(policy["preferences"]["pedigree_policy"], "ignore")
        self.assertEqual(policy["provenance"]["pedigree_policy"]["source"], "jd")
        self.assertEqual(policy["preferences"]["current_founder_c_suite_for_non_exec_ic"], "eligible")
        self.assertEqual(
            policy["provenance"]["current_founder_c_suite_for_non_exec_ic"]["source"],
            "user",
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

    def test_build_frontier_has_merge_compatible_fields(self):
        union_row = {
            "person_id": "p1", "name": "Ada", "linkedin_url": "https://linkedin.com/in/ada",
            "current_title": "Staff Engineer", "current_company": "Acme", "location": "SF",
            "found_by": ["q1", "q2"],
        }
        # Source/Channel carry the REAL import provenance from the profile source_map, not the method
        front = bei.build_frontier([union_row], {"p1": ("Jane Doe", "gmail")})
        row = front[0]
        self.assertEqual(row["candidate_id"], "p1")
        self.assertEqual(row["current_role"], "Staff Engineer")
        self.assertEqual(row["source_operator"], "Jane Doe")
        self.assertEqual(row["source_channel"], "gmail")
        self.assertEqual(row["duplicate_signal"]["matched_probe_count"], 2)
        self.assertEqual(row["duplicate_signal"]["matched_probe_ids"], ["q1", "q2"])
        # no provenance on file -> empty, never a hardcoded placeholder value
        bare = bei.build_frontier([union_row])[0]
        self.assertEqual((bare["source_operator"], bare["source_channel"]), ("", ""))

    def test_profile_source_map_reads_primary_provenance(self):
        d = Path(tempfile.mkdtemp())
        (d / "hydrate_people").mkdir(parents=True)
        with gzip.open(d / "hydrate_people" / "profiles.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"person_id": "p1", "primary_source_operator": "Jane Doe", "primary_source_channel": "gmail"}) + "\n")
            fh.write(json.dumps({"person_id": "p2", "source_operators": ["Bob"], "source_channels": ["linkedin"]}) + "\n")
        m = bei.profile_source_map([str(d)])
        self.assertEqual(m["p1"], ("Jane Doe", "gmail"))
        self.assertEqual(m["p2"], ("Bob", "linkedin"))  # falls back to first of the *_operators/_channels lists

    def test_write_frontier_artifacts_writes_same_full_ids(self):
        d = Path(tempfile.mkdtemp())
        frontier = bei.build_frontier([
            {"person_id": "a", "found_by": ["q0"], "current_title": "Eng"},
            {"person_id": "b", "found_by": ["q1"], "current_title": "MTS"},
        ])
        bei.write_frontier_artifacts(d, frontier)
        json_doc = json.loads((d / "candidate_frontier.json").read_text())
        jsonl_rows = [json.loads(line) for line in (d / "candidate_frontier.jsonl").read_text().splitlines()]
        self.assertEqual(json_doc["candidate_count"], 2)
        self.assertEqual({r["person_id"] for r in json_doc["candidates"]}, {"a", "b"})
        self.assertEqual({r["person_id"] for r in jsonl_rows}, {"a", "b"})
        self.assertIn("duplicate_signal", json_doc["candidates"][0])

    def test_main_reuses_plan_without_created_at_and_writes_json_and_jsonl(self):
        d = Path(tempfile.mkdtemp())
        (d / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"], "current_title": "Eng"}) + "\n")
        plan = {"traits": {"must_have": [{"trait": "systems", "tier": "core"}], "nice_to_have": []},
                "created_at": "original", "target_level": "staff_ic"}
        plan_path = d / "approved.json"
        plan_path.write_text(json.dumps(plan))
        argv = sys.argv
        sys.argv = ["build", "--run-dir", str(d), "--plan", str(plan_path)]
        try:
            bei.main()
        finally:
            sys.argv = argv
        self.assertTrue((d / "candidate_frontier.json").exists())
        self.assertTrue((d / "candidate_frontier.jsonl").exists())
        self.assertEqual(json.loads((d / "plan.json").read_text())["created_at"], "original")

    def test_main_requires_created_at_for_new_plan_before_openai(self):
        d = Path(tempfile.mkdtemp())
        (d / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"]}) + "\n")
        jd = d / "jd.txt"
        jd.write_text("Build distributed systems")
        argv = sys.argv
        sys.argv = ["build", "--run-dir", str(d), "--jd-file", str(jd)]
        try:
            with self.assertRaises(SystemExit):
                bei.main()
        finally:
            sys.argv = argv

    def test_emitted_frontier_works_with_capture_and_export_defaults(self):
        d = Path(tempfile.mkdtemp())
        frontier = bei.build_frontier([{
            "person_id": "p1", "name": "Ada", "linkedin_url": "https://linkedin.com/in/ada",
            "current_title": "Staff Engineer", "current_company": "Acme", "location": "SF",
            "found_by": ["q0", "q1"],
        }], {"p1": ("Jane Doe", "gmail")})
        bei.write_frontier_artifacts(d, frontier)
        (d / "plan.json").write_text(json.dumps({"traits": {"must_have": [{"trait": "systems"}], "nice_to_have": []}}))
        raw = {
            "candidate_id": "p1", "person_id": "p1", "rank": 1, "jd_score": 0.8,
            "verdict": "top_tier", "seniority_fit": "in_band", "rationale": "strong fit",
            "must_have": [{"trait": "systems", "status": "doing_now", "evidence": "built it"}],
            "nice_to_have": [], "duplicate_signal": frontier[0]["duplicate_signal"], "caveats": [],
        }
        (d / "candidate_evaluations.raw.jsonl").write_text(json.dumps(raw) + "\n")
        capture = ROOT / "packs/search/primitives/capture_jd_evaluations/capture_jd_evaluations.py"
        export = ROOT / "packs/search/primitives/export_candidate_shortlist/export_candidate_shortlist.py"
        cp = subprocess.run([sys.executable, str(capture), "--run-dir", str(d), "--evaluator-mode", "harness_single_agent", "--force"],
                            text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        cp = subprocess.run([sys.executable, str(export), "--run-dir", str(d)], text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        with (d / "shortlist.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["Current Role"], "Staff Engineer")
        self.assertEqual(rows[0]["Source"], "Jane Doe")
        self.assertEqual(rows[0]["Channel"], "gmail")

    def test_export_keeps_empty_sendable_shortlist_empty(self):
        d = Path(tempfile.mkdtemp())
        (d / "candidate_frontier.json").write_text(json.dumps({"candidates": [{
            "candidate_id": "p1", "name": "Not a fit", "current_role": "Other",
        }]}))
        (d / "candidate_evaluations.json").write_text(json.dumps({"evaluations": [{
            "candidate_id": "p1", "rank": 1, "verdict": "out", "rationale": "wrong domain",
        }]}))
        export = ROOT / "packs/search/primitives/export_candidate_shortlist/export_candidate_shortlist.py"
        cp = subprocess.run([sys.executable, str(export), "--run-dir", str(d)],
                            text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        with (d / "shortlist.csv").open() as fh:
            self.assertEqual(list(csv.DictReader(fh)), [])


class TestTriageCandidates(unittest.TestCase):
    def test_compact_card_merges_front_and_profile(self):
        card = tc.compact_card(
            "p1",
            {"current_title": "MTS", "current_company": "xAI", "name": "fallback"},
            {"name": "Real Name", "headline": "Systems eng",
             "positions": [{"title": "SWE", "company_name": "Google"}, {"title": "", "company_name": ""}],
             "education": [{"school_name": "MIT", "degree": "BS", "field_of_study": "CS"}],
             "tech_skills": ["go", "k8s", 5]})
        self.assertEqual(card["id"], "p1")
        self.assertEqual(card["name"], "Real Name")  # profile wins over front fallback
        self.assertEqual(card["current"], "MTS @ xAI")
        self.assertEqual(card["positions"], ["SWE @ Google"])  # empty position dropped
        self.assertEqual(card["education"], ["MIT BS CS"])
        self.assertEqual(card["skills"], ["go", "k8s"])  # non-str skill dropped

    def test_compact_card_handles_missing_profile(self):
        card = tc.compact_card("p2", {"name": "Only Front", "current_company": "Acme"}, None)
        self.assertEqual(card["name"], "Only Front")
        self.assertEqual(card["current"], "@ Acme")
        self.assertEqual(card["positions"], [])

    def test_parse_verdicts_lowercases(self):
        out = tc.parse_verdicts('{"verdicts":[{"id":"a","v":"KEEP"},{"id":"b","v":"Drop"},{"v":"x"}]}')
        self.assertEqual(out, {"a": "keep", "b": "drop"})

    def test_parse_verdicts_bad_json(self):
        self.assertEqual(tc.parse_verdicts("not json"), {})

    def test_keep_set_is_conservative(self):
        self.assertIn("maybe", tc.KEEP)
        self.assertIn("keep", tc.KEEP)
        self.assertNotIn("drop", tc.KEEP)

    def test_build_batch_messages_lists_traits(self):
        msgs = tc.build_batch_messages(
            {"must_have": [{"trait": "schedulers"}], "nice_to_have": [{"trait": "gpus"}]},
            [{"id": "a", "name": "A"}])
        self.assertIn("schedulers", msgs[-1]["content"])
        self.assertIn("gpus", msgs[-1]["content"])

    def test_build_batch_messages_preserves_alternative_core_paths(self):
        msgs = tc.build_batch_messages(
            {
                "must_have": [
                    {"trait": "scheduler", "tier": "core"},
                    {"trait": "inference", "tier": "core"},
                ],
                "nice_to_have": [],
            },
            [{"id": "a"}],
            core_groups=[
                {"name": "scheduler path", "all_of": ["scheduler"]},
                {"name": "inference path", "all_of": ["inference"]},
            ],
        )
        prompt = msgs[-1]["content"]
        self.assertIn("OR across paths", prompt)
        self.assertIn("scheduler path: scheduler", prompt)
        self.assertIn("inference path: inference", prompt)


class TestNormalizeVerdict(unittest.TestCase):
    def test_maps_eval_raw_to_consensus_schema(self):
        r = jc.normalize_verdict({"candidate_id": "p1", "jd_score": 0.7, "seniority_fit": "ideal", "verdict": "top_tier"})
        self.assertEqual(r["person_id"], "p1")
        self.assertEqual(r["score"], 0.7)
        self.assertTrue(r["in_band"])  # ideal is not gated

    def test_gated_fit_is_not_in_band(self):
        r = jc.normalize_verdict({"candidate_id": "p2", "jd_score": 0.2, "seniority_fit": "too_senior", "verdict": "out"})
        self.assertFalse(r["in_band"])

    def test_unknown_seniority_stays_in_band_but_is_surfaced(self):
        # Audit-validated semantics: thin-but-real profiles (seniority unknown) stay in the
        # pool — hard-gating unknown starved anchor expansion and broke the founder-eligible
        # override. unknown is surfaced via unknown_seniority_votes for human review instead.
        r = jc.normalize_verdict({
            "candidate_id": "p2",
            "jd_score": 0.9,
            "seniority_fit": "unknown",
            "verdict": "top_tier",
        })
        self.assertTrue(r["in_band"])
        r = jc.normalize_verdict({
            "person_id": "p3",
            "score": 0.9,
            "seniority_fit": "unknown",
            "verdict": "top_tier",
            "in_band": True,
        })
        self.assertTrue(r["in_band"])  # explicit native in_band is honored

    def test_missing_or_invalid_seniority_is_not_in_band(self):
        for fit in (None, "", "not_a_seniority_band", ["ideal"]):
            with self.subTest(fit=fit):
                verdict = {"person_id": "p", "score": 0.9, "verdict": "top_tier", "in_band": True}
                if fit is not None:
                    verdict["seniority_fit"] = fit
                self.assertFalse(jc.normalize_verdict(verdict)["in_band"])

    def test_evaluator_marker_distinguishes_missing_from_explicit_unknown(self):
        missing = jc.normalize_verdict({
            "person_id": "p1",
            "score": 0.9,
            "verdict": "top_tier",
            "seniority_fit": "unknown",
            "_seniority_assessment_valid": False,
        })
        explicit = jc.normalize_verdict({
            "person_id": "p2",
            "score": 0.9,
            "verdict": "top_tier",
            "seniority_fit": "unknown",
            "_seniority_assessment_valid": True,
        })
        self.assertFalse(missing["in_band"])
        self.assertTrue(explicit["in_band"])

    def test_known_seniority_is_normalized_before_classification(self):
        verdict = jc.normalize_verdict({
            "person_id": "p",
            "score": 0.9,
            "verdict": "top_tier",
            "seniority_fit": " IDEAL ",
        })
        self.assertEqual(verdict["seniority_fit"], "ideal")
        self.assertTrue(verdict["in_band"])

    def test_native_schema_passthrough(self):
        native = {"person_id": "p3", "score": 0.5, "in_band": True, "seniority_fit": "in_band", "verdict": "high_potential"}
        self.assertEqual(jc.normalize_verdict(dict(native)), native)


class TestCodexJudgeExtract(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(cj_judge.extract_json('{"seniority_fit":"ideal"}'), {"seniority_fit": "ideal"})

    def test_fenced_json(self):
        self.assertEqual(cj_judge.extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_json_with_prose_around(self):
        self.assertEqual(cj_judge.extract_json('Here is the result:\n{"verdict":"out"}\nDone.'), {"verdict": "out"})

    def test_empty_and_garbage(self):
        self.assertEqual(cj_judge.extract_json(""), {})
        self.assertEqual(cj_judge.extract_json("no json here"), {})

    def test_judge_one_passes_prompt_via_stdin_not_argv(self):
        long_prompt = "PRIVATE PROFILE " * 100
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["input"] = kwargs.get("input")
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_text('{"seniority_fit":"ideal"}')
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(cj_judge.subprocess, "run", side_effect=fake_run):
            parsed, err = cj_judge.judge_one(long_prompt, None, "low", 5)
        self.assertEqual(parsed, {"seniority_fit": "ideal"})
        self.assertIsNone(err)
        self.assertEqual(seen["input"], long_prompt)
        self.assertFalse(any(long_prompt in str(part) for part in seen["cmd"]))

    def test_judge_one_surfaces_nonzero_exit(self):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=42, stdout="", stderr="permission denied")

        with mock.patch.object(cj_judge.subprocess, "run", side_effect=fake_run):
            parsed, err = cj_judge.judge_one("prompt", None, "low", 5)
        self.assertEqual(parsed, {})
        self.assertIn("codex_exit_42", err)
        self.assertIn("permission denied", err)

    def test_main_fails_when_all_codex_subprocesses_error(self):
        d = Path(tempfile.mkdtemp())
        candidate = {"person_id": "p1", "candidate_id": "p1"}
        argv = sys.argv
        sys.argv = ["codex_judge", "--run-dir", str(d)]
        try:
            with mock.patch.object(cj_judge.EV, "read_json", return_value={"traits": {"must_have": []}}), \
                 mock.patch.object(cj_judge.EV, "load_frontier", return_value=[candidate]), \
                 mock.patch.object(cj_judge.EV, "collect_profiles", return_value={"p1": {"person_id": "p1"}}), \
                 mock.patch.object(cj_judge.EV, "build_user_prompt", return_value="profile prompt"), \
                 mock.patch.object(cj_judge, "judge_one", return_value=({}, "codex_exit_1: auth")):
                with self.assertRaises(SystemExit) as ctx:
                    cj_judge.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv
        self.assertTrue((d / "candidate_evaluations.raw.jsonl").exists())


class TestConsensusScoreThreshold(unittest.TestCase):
    def _judges(self):
        # one judge file; in_band derived from seniority_fit
        return {"j": [
            {"person_id": "a", "seniority_fit": "ideal", "verdict": "out", "score": 0.45, "in_band": True},
            {"person_id": "b", "seniority_fit": "ideal", "verdict": "out", "score": 0.30, "in_band": True},
            {"person_id": "c", "seniority_fit": "too_senior", "verdict": "out", "score": 0.9, "in_band": False},
        ]}

    def test_threshold_never_rescues_categorical_out(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1, score_threshold=0.40)
        self.assertEqual(strong, [])

    def test_threshold_gates_out_of_band_even_if_high_score(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1, score_threshold=0.10)
        self.assertNotIn("c", [r["person_id"] for r in strong])  # too_senior never kept

    def test_no_threshold_uses_notout_gate(self):
        # without threshold, all verdicts are "out" -> nobody passes the not-out gate
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1)
        self.assertEqual(strong, [])

    def test_cli_defaults_support_one_judge_and_bench_unknown_seniority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            judges = root / "judges"
            out = root / "out"
            judges.mkdir()
            rows = [
                {"person_id": "known", "seniority_fit": "ideal", "verdict": "top_tier", "score": 0.9},
                {"person_id": "unknown", "seniority_fit": "unknown", "verdict": "top_tier", "score": 0.9},
                {"person_id": "missing", "verdict": "top_tier", "score": 0.9},
            ]
            (judges / "one.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            cp = subprocess.run(
                [
                    sys.executable,
                    str(PRIM / "judge_consensus.py"),
                    "--judges-dir",
                    str(judges),
                    "--out-dir",
                    str(out),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
            shortlist = {row["person_id"] for row in json.loads((out / "shortlist_ranked.json").read_text())}
            sendable = {row["person_id"] for row in json.loads((out / "sendable_ranked.json").read_text())}
            bench = {row["person_id"] for row in json.loads((out / "bench_ranked.json").read_text())}
            self.assertEqual(shortlist, {"known", "unknown"})
            self.assertEqual(sendable, {"known"})
            self.assertEqual(bench, {"unknown"})

    def test_cli_plan_location_gates_every_shortlist_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            judges = root / "judges"
            out = root / "out"
            judges.mkdir()
            rows = [
                {"person_id": "local", "seniority_fit": "ideal", "verdict": "top_tier", "score": 0.9},
                {"person_id": "remote", "seniority_fit": "ideal", "verdict": "top_tier", "score": 0.9},
                {"person_id": "missing", "seniority_fit": "ideal", "verdict": "top_tier", "score": 0.9},
            ]
            (judges / "one.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            union = root / "union.jsonl"
            union.write_text("".join(json.dumps(row) + "\n" for row in [
                {"person_id": "local", "location": "Palo Alto, California, United States"},
                {"person_id": "remote", "location": "New York, New York, United States"},
                {"person_id": "missing", "location": None},
            ]))
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "search_scope": {
                    "location": "San Francisco Bay Area",
                    "filters": {"metro_areas": ["San Francisco Bay Area"]},
                    "source": "jd",
                },
                "traits": {"must_have": [], "nice_to_have": []},
            }))

            cp = subprocess.run(
                [
                    sys.executable,
                    str(PRIM / "judge_consensus.py"),
                    "--judges-dir", str(judges),
                    "--union", str(union),
                    "--out-dir", str(out),
                    "--plan", str(plan),
                    "--score-threshold", "0.40",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
            for name in ("shortlist_ranked.json", "sendable_ranked.json"):
                self.assertEqual(
                    {row["person_id"] for row in json.loads((out / name).read_text())},
                    {"local"},
                )
            self.assertEqual(json.loads((out / "bench_ranked.json").read_text()), [])
            consensus = {row["person_id"]: row for row in json.loads((out / "consensus.json").read_text())}
            self.assertEqual(consensus["remote"]["location_fit"], "mismatch")
            self.assertEqual(consensus["missing"]["location_fit"], "unknown")

            plan.write_text(json.dumps({
                "search_scope": {"location": None, "filters": {}, "source": "jd"},
                "traits": {"must_have": [], "nice_to_have": []},
            }))
            global_out = root / "global-out"
            cp = subprocess.run(
                [
                    sys.executable,
                    str(PRIM / "judge_consensus.py"),
                    "--judges-dir", str(judges),
                    "--union", str(union),
                    "--out-dir", str(global_out),
                    "--plan", str(plan),
                    "--score-threshold", "0.40",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
            self.assertEqual(
                {row["person_id"] for row in json.loads((global_out / "sendable_ranked.json").read_text())},
                {"local", "remote", "missing"},
            )


class TestRobustSourceMerge(unittest.TestCase):
    def _write(self, rows):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        return Path(f.name)

    def test_merge_dedups_and_counts_net_new(self):
        into = {}
        p0 = self._write([{"person_id": "a", "found_by": ["q0"]}, {"person_id": "b", "found_by": ["q1"]}])
        n0 = rs._merge(into, p0, "r0")
        self.assertEqual(n0, 2)
        self.assertEqual(set(into), {"a", "b"})
        self.assertEqual(into["a"]["found_by"], ["r0:q0"])  # provenance tagged with round
        # round 1: a is a repeat (no net-new), c is new
        p1 = self._write([{"person_id": "a", "found_by": ["q5"]}, {"person_id": "c", "found_by": ["q9"]}])
        n1 = rs._merge(into, p1, "r1")
        self.assertEqual(n1, 1)  # only c is net-new
        self.assertEqual(set(into), {"a", "b", "c"})
        self.assertEqual(into["a"]["found_by"], ["r0:q0", "r1:q5"])  # accumulated across rounds

    def test_merge_skips_idless_and_missing_file(self):
        into = {"x": {"person_id": "x", "found_by": []}}
        self.assertEqual(rs._merge(into, Path("/nonexistent/none.jsonl"), "r0"), 0)
        p = self._write([{"found_by": ["q"]}, {"person_id": "y", "found_by": ["q"]}])
        self.assertEqual(rs._merge(into, p, "r0"), 1)  # idless dropped, y added

    def test_emphases_are_distinct(self):
        self.assertEqual(len(set(rs.EMPHASES)), len(rs.EMPHASES))

    def test_main_fails_on_child_command_error(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build distributed systems")
        plan_path = d / "plan.json"
        plan_path.write_text(json.dumps(bei.plan_from_obj(
            {
                "job_title": "Staff Engineer", "hire_stage": "growth",
                "must_have": [{"trait": "distributed systems", "tier": "core"}],
            },
            set_name="team", set_id="set-1", source_url=None, created_at="t",
        )))
        err = rs.CommandError(["fake"], returncode=9, stderr="boom", description="decompose round 0")
        argv = sys.argv
        sys.argv = [
            "robust", "--jd-file", str(jd), "--plan", str(plan_path),
            "--run-dir", str(d / "run"), "--max-rounds", "1",
        ]
        try:
            with mock.patch.object(rs, "run_checked", side_effect=err):
                with self.assertRaises(SystemExit) as ctx:
                    rs.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv

    def test_invalid_plan_fails_before_any_sourcing_subprocess(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build distributed systems")
        plan = d / "plan.json"
        plan.write_text(json.dumps({
            "search_scope": {"location": "San Francisco", "filters": {}},
        }))
        argv = sys.argv
        sys.argv = [
            "robust", "--jd-file", str(jd), "--plan", str(plan),
            "--run-dir", str(d / "run"), "--max-rounds", "1",
        ]
        try:
            with mock.patch.object(rs, "run_checked") as run_checked:
                with self.assertRaises(SystemExit):
                    rs.main()
            run_checked.assert_not_called()
        finally:
            sys.argv = argv

    def test_malformed_plan_shapes_fail_before_any_sourcing_subprocess(self):
        for document in ([1], {"search_scope": True}):
            with self.subTest(document=document):
                d = Path(tempfile.mkdtemp())
                jd = d / "jd.txt"
                jd.write_text("Build distributed systems")
                plan = d / "plan.json"
                plan.write_text(json.dumps(document))
                argv = sys.argv
                sys.argv = [
                    "robust", "--jd-file", str(jd), "--plan", str(plan),
                    "--run-dir", str(d / "run"), "--max-rounds", "1",
                ]
                try:
                    with mock.patch.object(rs, "run_checked") as run_checked:
                        with self.assertRaises(SystemExit) as ctx:
                            rs.main()
                    self.assertEqual(ctx.exception.code, 1)
                    run_checked.assert_not_called()
                finally:
                    sys.argv = argv


class TestRecruitLoopAnchors(unittest.TestCase):
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

    def test_diverse_anchors_dedups_by_company_and_ranks_by_score(self):
        strong = [
            {"person_id": "a", "current_company": "SpaceX", "mean_score": 0.9},
            {"person_id": "b", "current_company": "SpaceX", "mean_score": 0.8},  # same co -> skipped
            {"person_id": "c", "current_company": "Meta", "mean_score": 0.85},
            {"person_id": "d", "current_company": "NVIDIA", "mean_score": 0.7},
        ]
        union = {"a": {"person_id": "a", "tech_skills": ["x"]}}
        out = rl.diverse_anchors(strong, union, k=3)
        self.assertEqual([r["person_id"] for r in out], ["a", "c", "d"])  # b dropped (dup co), ranked by score
        self.assertEqual(out[0]["tech_skills"], ["x"])  # enriched from union profile

    def test_diverse_anchors_empty_when_no_strong(self):
        self.assertEqual(rl.diverse_anchors([], {}, k=5), [])

    def test_diverse_anchors_respects_k(self):
        strong = [{"person_id": str(i), "current_company": f"co{i}", "mean_score": 1.0 - i / 10} for i in range(10)]
        self.assertEqual(len(rl.diverse_anchors(strong, {}, k=4)), 4)

    def test_anchor_expansion_command_always_passes_approved_plan(self):
        command = [str(part) for part in rl.anchor_expansion_command(
            Path("anchors.json"), Path("plan.json"), Path("seeds.json"), 6,
        )]
        self.assertEqual(command[command.index("--plan") + 1], "plan.json")
        self.assertEqual(command[command.index("--anchors") + 1], "anchors.json")

    def test_stage_judge_input_does_not_mutate_canonical_frontier(self):
        d = Path(tempfile.mkdtemp())
        (d / "plan.json").write_text(json.dumps({"traits": {"must_have": []}}))
        (d / "probe_summaries.json").write_text("[]")
        full = [
            {"person_id": "a", "candidate_id": "a"},
            {"person_id": "b", "candidate_id": "b"},
        ]
        original = "".join(json.dumps(r, sort_keys=True) + "\n" for r in full)
        (d / "candidate_frontier.jsonl").write_text(original)
        (d / "candidate_frontier.json").write_text(json.dumps({"candidates": full}))

        jdir = rl.stage_judge_input(d, [full[0]])
        self.assertEqual((d / "candidate_frontier.jsonl").read_text(), original)
        staged = [json.loads(line) for line in (jdir / "candidate_frontier.jsonl").read_text().splitlines()]
        self.assertEqual([r["person_id"] for r in staged], ["a"])
        self.assertTrue((jdir / "plan.json").exists())
        self.assertTrue((jdir / "probe_summaries.json").exists())

    def test_judge_consensus_help_describes_alternative_core_groups(self):
        cp = subprocess.run([sys.executable, str(PRIM / "judge_consensus.py"), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0)
        self.assertIn("one complete alternative core group", cp.stdout)

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

    def test_advisory_critic_non_object_is_unavailable(self):
        directory = Path(tempfile.mkdtemp())
        critic = directory / "plan_critic.json"
        critic.write_text("[]")
        loaded = rl.load_advisory_critic(critic)
        self.assertEqual(loaded["verdict"], "unavailable")
        self.assertIn("JSON object", loaded["error"])

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
            "epoch0/union.jsonl",
            "epoch0/round0/union.jsonl",
            "epoch0/round0/probes/q00/ledger.json",
            "epoch1/probes/anchor00/ledger.json",
            "epoch0/candidate_frontier.full.jsonl",
            "epoch0/probe_summaries.json",
            "epoch0/triage.json",
            "epoch0/candidate_evaluations.raw.jsonl",
            "epoch1/anchors.json",
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
        (run_dir / "epoch0" / "plan_critic.json").write_text(json.dumps({"verdict": "ok"}))
        (run_dir / "decision.json").write_text(json.dumps({"surface": "people"}))
        (run_dir / "loop.json").write_text(json.dumps([{"status": "awaiting_plan_approval"}]))
        canonical, _ = rl.bind_approved_plan(
            run_dir, plan_path, {"backend": "powerset", "set_id": "set-1"},
        )
        self.assertEqual(canonical, run_dir / "epoch0" / "plan.json")

    def test_main_stops_at_gate_without_approval_and_does_not_judge(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"

        def fake_run(cmd, *, expected_paths=None, description=None):
            if description == "build recruiter plan":
                e0 = run_dir / "epoch0"
                (e0 / "plan.json").write_text(json.dumps({"route": "deep", "traits": {"must_have": []}, "created_at": "t"}))
            elif description == "plan critic":
                (run_dir / "epoch0" / "plan_critic.json").write_text(json.dumps({"verdict": "ok"}))
            elif description == "validate recruiter plan":
                pass
            else:
                self.fail(f"unexpected run before gate: {description}")

        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1"]
        try:
            with mock.patch.object(rl, "run", side_effect=fake_run), mock.patch.object(rl, "judge", side_effect=AssertionError("judge called")):
                rl.main()
        finally:
            sys.argv = argv
        hist = json.loads((run_dir / "loop.json").read_text())
        self.assertEqual(hist[0]["status"], "awaiting_plan_approval")
        self.assertFalse(hist[0]["source_started"])
        self.assertFalse((run_dir / "epoch0" / "union.jsonl").exists())
        self.assertTrue((run_dir / "epoch0" / "plan.json").exists())

    def test_plan_approved_resume_preserves_existing_plan_and_skips_build(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        e0 = run_dir / "epoch0"
        e0.mkdir(parents=True)
        plan_bytes = b'{"traits":{"must_have":[]},"created_at":"human-edited"}\n'
        (e0 / "plan.json").write_bytes(plan_bytes)
        (e0 / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"]}) + "\n")
        rows = [{"person_id": "p1", "candidate_id": "p1"}]
        (e0 / "candidate_frontier.jsonl").write_text(json.dumps(rows[0]) + "\n")
        (e0 / "candidate_frontier.json").write_text(json.dumps({"candidates": rows}))
        (e0 / "probe_summaries.json").write_text("[]")
        calls = []

        def fake_run(cmd, *, expected_paths=None, description=None):
            calls.append(description)
            self.assertNotEqual(description, "epoch0 build_eval_inputs")
            if description == "epoch0 consensus":
                out = run_dir / "shortlist"
                out.mkdir(parents=True, exist_ok=True)
                (out / "consensus.json").write_text("[]")
                (out / "ground_truth_ranked.json").write_text("[]")
                (out / "shortlist_ranked.json").write_text("[]")
                (out / "sendable_ranked.json").write_text("[]")
                (out / "bench_ranked.json").write_text("[]")

        def fake_judge(edir, candidates, judge_kind, effort, concurrency):
            (edir / "candidate_evaluations.raw.jsonl").write_text(json.dumps({"candidate_id": "p1", "jd_score": 0.1}) + "\n")

        argv = sys.argv
        # --no-triage/--no-micro-sort: this test pins plan preservation + build skip on resume,
        # not phase-1 filtering or the final ordering pass
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1", "--plan-approved", "--no-triage", "--no-micro-sort"]
        try:
            with mock.patch.object(rl, "run", side_effect=fake_run), \
                 mock.patch.object(rl, "validate_approved_plan"), \
                 mock.patch.object(
                     rl,
                     "resolve_retrieval_identity",
                     return_value=({"backend": "powerset", "set_id": "x"}, "x", "db"),
                 ), \
                 mock.patch.object(rl, "bind_approved_plan", return_value=(e0 / "plan.json", "digest")), \
                 mock.patch.object(rl, "judge", side_effect=fake_judge):
                rl.main()
        finally:
            sys.argv = argv
        self.assertEqual((e0 / "plan.json").read_bytes(), plan_bytes)
        self.assertEqual(calls, ["validate approved recruiter plan", "epoch0 consensus"])

    def test_unapproved_rerun_with_existing_plan_does_not_clobber_or_build(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        e0 = run_dir / "epoch0"
        e0.mkdir(parents=True)
        plan_bytes = b'{"traits":{"must_have":[{"trait":"edited"}]},"created_at":"human"}\n'
        (e0 / "plan.json").write_bytes(plan_bytes)

        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1"]
        try:
            with mock.patch.object(rl, "run", side_effect=AssertionError("should not run child commands")), \
                 mock.patch.object(rl, "judge", side_effect=AssertionError("judge called")):
                rl.main()
        finally:
            sys.argv = argv
        self.assertEqual((e0 / "plan.json").read_bytes(), plan_bytes)
        hist = json.loads((run_dir / "loop.json").read_text())
        self.assertTrue(hist[0]["existing_plan"])
        self.assertEqual(hist[0]["status"], "awaiting_plan_approval")

    def test_main_writes_failure_status_on_command_error(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        err = rl.CommandError(["fake"], returncode=2, stderr="bad", description="epoch0 robust_source")
        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1"]
        try:
            with mock.patch.object(rl, "run", side_effect=err):
                with self.assertRaises(SystemExit) as ctx:
                    rl.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv
        hist = json.loads((run_dir / "loop.json").read_text())
        self.assertEqual(hist[-1]["status"], "failed")


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
                def fake_run(cmd, *, expected_paths=None, description=None):
                    nonlocal build_cmd
                    if description == "fetch_jd URL->JD":
                        (run_dir / "jd.txt").write_text(
                            "Senior Backend Engineer\n\n" + ("Build high-throughput APIs. " * 20))
                        (run_dir / "source.json").write_text(json.dumps({
                            "requested_url": "https://example.test/job",
                            "source_url": "https://example.test/job",
                        }))
                    elif description == "build recruiter plan":
                        build_cmd = [str(part) for part in cmd]
                        epoch0 = run_dir / "epoch0"
                        epoch0.mkdir(parents=True, exist_ok=True)
                        (epoch0 / "plan.json").write_text(json.dumps({"route": "deep", "traits": []}))
                    elif description == "plan critic":
                        (run_dir / "epoch0" / "plan_critic.json").write_text(json.dumps({"verdict": "ok"}))
                with mock.patch.object(rl, "run", side_effect=fake_run):
                    rl.main()  # returns at awaiting_plan_approval (no SystemExit)
            finally:
                sys.argv = argv
            self.assertTrue((run_dir / "jd.txt").exists())  # URL was fetched to jd.txt before the loop
            self.assertIn("--source-url", build_cmd)
            self.assertEqual(build_cmd[build_cmd.index("--source-url") + 1], "https://example.test/job")

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


class TestJudgeErrorHandling(unittest.TestCase):
    """Transient judge failures must not become cached rejections (PR #153 review finding 1)."""

    def test_consensus_read_jsonl_skips_error_rows(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loop.jsonl"
            path.write_text(
                json.dumps({"candidate_id": "p1", "jd_score": 0.8, "verdict": "top_tier", "seniority_fit": "ideal"}) + "\n"
                + json.dumps({"candidate_id": "p2", "jd_score": 0.0, "verdict": "out", "seniority_fit": "unknown", "error": "timeout"}) + "\n",
                encoding="utf-8",
            )
            rows = jc.read_jsonl(path)
        self.assertEqual([r["person_id"] for r in rows], ["p1"])


class TestTwoPhaseJudging(unittest.TestCase):
    def test_loop_parser_accepts_triage_and_judge_flags(self):
        argv_seen = {}
        real_parse = argparse.ArgumentParser.parse_args

        def spy(self, *a, **k):
            ns = real_parse(self, ["--jd-file", "x", "--run-dir", "y", "--created-at", "z",
                                   "--no-triage", "--judge", "gpt"])
            argv_seen.update(vars(ns))
            raise SystemExit(0)

        dsl = _load("deep_search_loop")
        with unittest.mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
            with self.assertRaises(SystemExit):
                dsl.main()
        self.assertFalse(argv_seen["triage"])
        self.assertEqual(argv_seen["judge"], "gpt")

    def test_triage_default_on(self):
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
        self.assertTrue(parser_defaults.triage)


class TestNoCliBulkFilter(unittest.TestCase):
    """CLI agent engines are phase-2 judges only: --no-triage over a large frontier must refuse codex."""

    def _staged_run(self, d, n_candidates):
        run_dir = d / "run"
        e0 = run_dir / "epoch0"
        e0.mkdir(parents=True)
        (e0 / "plan.json").write_text(json.dumps({"traits": {"must_have": []}, "created_at": "t"}))
        rows = [{"person_id": f"p{i}", "candidate_id": f"p{i}"} for i in range(n_candidates)]
        (e0 / "union.jsonl").write_text("".join(json.dumps({"person_id": r["person_id"], "found_by": ["q0"]}) + "\n" for r in rows))
        (e0 / "candidate_frontier.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (e0 / "candidate_frontier.json").write_text(json.dumps({"candidates": rows}))
        (e0 / "probe_summaries.json").write_text("[]")
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        return run_dir, jd

    def test_no_triage_large_frontier_codex_refused(self):
        dsl = _load("deep_search_loop")
        d = Path(tempfile.mkdtemp())
        run_dir, jd = self._staged_run(d, dsl.MAX_CLI_JUDGE_FRONTIER + 1)
        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t",
                    "--max-epochs", "1", "--plan-approved", "--no-triage", "--judge", "codex"]
        try:
            with unittest.mock.patch.object(dsl, "run"), \
                 unittest.mock.patch.object(dsl, "validate_approved_plan"), \
                 unittest.mock.patch.object(
                     dsl,
                     "resolve_retrieval_identity",
                     return_value=({"backend": "powerset", "set_id": "x"}, "x", "db"),
                 ), \
                 unittest.mock.patch.object(
                     dsl,
                     "bind_approved_plan",
                     return_value=(run_dir / "epoch0" / "plan.json", "digest"),
                 ), \
                 unittest.mock.patch.object(dsl, "judge"):
                with self.assertRaises(SystemExit) as ctx:
                    dsl.main()
        finally:
            sys.argv = argv
        self.assertEqual(ctx.exception.code, 1)

    def test_no_triage_small_frontier_codex_allowed(self):
        dsl = _load("deep_search_loop")
        d = Path(tempfile.mkdtemp())
        run_dir, jd = self._staged_run(d, 3)

        def fake_run(cmd, *, expected_paths=None, description=None):
            if description and "consensus" in description:
                out = run_dir / "shortlist"
                out.mkdir(parents=True, exist_ok=True)
                (out / "consensus.json").write_text("[]")
                (out / "ground_truth_ranked.json").write_text("[]")
                (out / "shortlist_ranked.json").write_text("[]")
                (out / "sendable_ranked.json").write_text("[]")
                (out / "bench_ranked.json").write_text("[]")

        def fake_judge(edir, candidates, judge_kind, effort, concurrency):
            (edir / "candidate_evaluations.raw.jsonl").write_text(
                "".join(json.dumps({"candidate_id": c["candidate_id"], "jd_score": 0.1}) + "\n" for c in candidates))

        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t",
                    "--max-epochs", "1", "--plan-approved", "--no-triage", "--judge", "codex"]
        try:
            with unittest.mock.patch.object(dsl, "run", side_effect=fake_run), \
                 unittest.mock.patch.object(dsl, "validate_approved_plan"), \
                 unittest.mock.patch.object(
                     dsl,
                     "resolve_retrieval_identity",
                     return_value=({"backend": "powerset", "set_id": "x"}, "x", "db"),
                 ), \
                 unittest.mock.patch.object(
                     dsl,
                     "bind_approved_plan",
                     return_value=(run_dir / "epoch0" / "plan.json", "digest"),
                 ), \
                 unittest.mock.patch.object(dsl, "judge", side_effect=fake_judge):
                dsl.main()
        finally:
            sys.argv = argv
        self.assertTrue((run_dir / "loop.json").exists())


class TestJudgeDefault(unittest.TestCase):
    """The phase-2 judge defaults to the paid gpt API; codex is the free opt-in.
    The selected judge + reasoning effort are recorded in loop.json history."""

    def _staged_run(self, d):
        run_dir = d / "run"
        e0 = run_dir / "epoch0"
        e0.mkdir(parents=True)
        (e0 / "plan.json").write_text(json.dumps({"traits": {"must_have": []}, "created_at": "t"}))
        rows = [{"person_id": "p0", "candidate_id": "p0"}]
        (e0 / "union.jsonl").write_text(json.dumps({"person_id": "p0", "found_by": ["q0"]}) + "\n")
        (e0 / "candidate_frontier.jsonl").write_text(json.dumps(rows[0]) + "\n")
        (e0 / "candidate_frontier.json").write_text(json.dumps({"candidates": rows}))
        (e0 / "probe_summaries.json").write_text("[]")
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        return run_dir, jd

    def test_default_judge_is_gpt_and_recorded_in_history(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("POWERPACKS_DEEP_JUDGE", None)
            dsl = _load("deep_search_loop")
            d = Path(tempfile.mkdtemp())
            run_dir, jd = self._staged_run(d)
            seen = {}

            def fake_run(cmd, *, expected_paths=None, description=None):
                if description and "consensus" in description:
                    out = run_dir / "shortlist"
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "consensus.json").write_text("[]")
                    (out / "ground_truth_ranked.json").write_text("[]")
                    (out / "shortlist_ranked.json").write_text("[]")
                    (out / "sendable_ranked.json").write_text("[]")
                    (out / "bench_ranked.json").write_text("[]")

            def fake_judge(edir, candidates, judge_kind, effort, concurrency):
                seen["judge"] = judge_kind
                seen["effort"] = effort
                (edir / "candidate_evaluations.raw.jsonl").write_text(
                    json.dumps({"candidate_id": "p0", "jd_score": 0.1}) + "\n")

            argv = sys.argv
            sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t",
                        "--max-epochs", "1", "--plan-approved", "--no-triage"]
            try:
                with unittest.mock.patch.object(dsl, "run", side_effect=fake_run), \
                     unittest.mock.patch.object(dsl, "validate_approved_plan"), \
                     unittest.mock.patch.object(
                         dsl,
                         "resolve_retrieval_identity",
                         return_value=({"backend": "powerset", "set_id": "x"}, "x", "db"),
                     ), \
                     unittest.mock.patch.object(
                         dsl,
                         "bind_approved_plan",
                         return_value=(run_dir / "epoch0" / "plan.json", "digest"),
                     ), \
                     unittest.mock.patch.object(dsl, "judge", side_effect=fake_judge):
                    dsl.main()
            finally:
                sys.argv = argv
        self.assertEqual(seen["judge"], "gpt")
        self.assertEqual(seen["effort"], "low")
        history = json.loads((run_dir / "loop.json").read_text())
        epoch_rows = [row for row in history if row.get("epoch") == 0 and "judge" in row]
        self.assertTrue(epoch_rows, history)
        self.assertEqual(epoch_rows[0]["judge"], "gpt")
        self.assertEqual(epoch_rows[0]["reasoning_effort"], "low")

    def test_env_preference_still_selects_codex(self):
        with mock.patch.dict(os.environ, {"POWERPACKS_DEEP_JUDGE": "codex"}):
            dsl = _load("deep_search_loop")
            d = Path(tempfile.mkdtemp())
            run_dir, jd = self._staged_run(d)
            seen = {}

            def fake_run(cmd, *, expected_paths=None, description=None):
                if description and "consensus" in description:
                    out = run_dir / "shortlist"
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "consensus.json").write_text("[]")
                    (out / "ground_truth_ranked.json").write_text("[]")
                    (out / "shortlist_ranked.json").write_text("[]")
                    (out / "sendable_ranked.json").write_text("[]")
                    (out / "bench_ranked.json").write_text("[]")

            def fake_judge(edir, candidates, judge_kind, effort, concurrency):
                seen["judge"] = judge_kind
                (edir / "candidate_evaluations.raw.jsonl").write_text(
                    json.dumps({"candidate_id": "p0", "jd_score": 0.1}) + "\n")

            argv = sys.argv
            sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t",
                        "--max-epochs", "1", "--plan-approved", "--no-triage"]
            try:
                with unittest.mock.patch.object(dsl, "run", side_effect=fake_run), \
                     unittest.mock.patch.object(dsl, "validate_approved_plan"), \
                     unittest.mock.patch.object(
                         dsl,
                         "resolve_retrieval_identity",
                         return_value=({"backend": "powerset", "set_id": "x"}, "x", "db"),
                     ), \
                     unittest.mock.patch.object(
                         dsl,
                         "bind_approved_plan",
                         return_value=(run_dir / "epoch0" / "plan.json", "digest"),
                     ), \
                     unittest.mock.patch.object(dsl, "judge", side_effect=fake_judge):
                    dsl.main()
            finally:
                sys.argv = argv
        self.assertEqual(seen["judge"], "codex")


if __name__ == "__main__":
    unittest.main()
