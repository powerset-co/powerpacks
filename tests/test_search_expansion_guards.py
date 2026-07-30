"""Offline tests for post-extraction search-quality guards.

Covers the Series-B-lead regression fixes: the any-seniority band clear, the
investing-context partner/c-suite/owner band-family union, the investing-context
company-funding drop, role-relevance title-cluster selection, and the
judge-pool floor for --limit. All extractor outputs are stubbed; no LLM or
network calls. Fixtures are synthetic (Jordan Bravo, Fixture Capital).
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EXTRACTORS = ROOT / "packs/search/primitives/expand_search_request/parallel_extractors.py"
PIPELINE = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
RESULTS_IO = ROOT / "packs/search/primitives/persist_search_results/results_io.py"
SHARED_DIR = ROOT / "packs/search/primitives/shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
import search_backend_mode  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def merge(mod, *, role=None, company=None, seniority=None, query=""):
    return mod._merge(role or {}, company or {}, {}, {}, {}, seniority or {}, {}, query)


class AnySeniorityGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("parallel_extractors_guards_test", EXTRACTORS)

    def test_any_seniority_phrase_clears_bands_and_writes_note(self):
        phrases = [
            "any seniority",
            "all seniority levels",
            "all levels",
            "any level",
            "regardless of seniority",
            "no seniority requirement",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                filters, notes = merge(
                    self.mod,
                    role={"semantic_query": "recruiting work", "bm25_queries": ["recruiter"]},
                    seniority={"seniority_bands": ["senior", "manager"]},
                    query=f"recruiters at {phrase}",
                )
                self.assertNotIn("seniority_bands", filters)
                self.assertTrue(any("any-seniority" in note for note in notes), notes)

    def test_any_seniority_clears_role_agent_bands_and_csuite_default(self):
        # Bands arriving via the role agent AND the deterministic C-suite
        # parity default must both be cleared by the guard.
        filters, notes = merge(
            self.mod,
            role={
                "semantic_query": "technology executives",
                "bm25_queries": ["technology executive"],
                "seniority": ["director"],
            },
            query="CTOs at any seniority",
        )
        self.assertNotIn("seniority_bands", filters)
        self.assertTrue(any("any-seniority" in note for note in notes), notes)

    def test_without_any_seniority_phrase_bands_survive(self):
        filters, notes = merge(
            self.mod,
            role={"semantic_query": "recruiting work", "bm25_queries": ["recruiter"]},
            seniority={"seniority_bands": ["senior"]},
            query="senior recruiters",
        )
        self.assertEqual(filters["seniority_bands"], ["senior"])
        self.assertEqual(notes, [])


class InvestingBandFamilyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("parallel_extractors_guards_test2", EXTRACTORS)

    def test_investing_department_unions_partner_band_family(self):
        filters, notes = merge(
            self.mod,
            role={
                "semantic_query": "investing partners at venture funds",
                "bm25_queries": ["venture partner"],
                "departments": ["investing"],
            },
            seniority={"seniority_bands": ["partner"]},
            query="partners in investing roles",
        )
        self.assertEqual(set(filters["seniority_bands"]), {"partner", "c-suite", "owner"})
        self.assertTrue(any("band family" in note for note in notes), notes)

    def test_vc_firm_entity_type_unions_partner_band_family(self):
        filters, _ = merge(
            self.mod,
            role={"semantic_query": "fund leadership", "bm25_queries": ["fund leadership"]},
            company={"entity_types": ["vc_firm"]},
            seniority={"seniority_bands": ["c_suite"]},
            query="leaders at VC firms",
        )
        self.assertEqual(set(filters["seniority_bands"]), {"partner", "c-suite", "owner"})

    def test_investing_role_id_unions_partner_band_family(self):
        filters, _ = merge(
            self.mod,
            role={
                "semantic_query": "general partners at funds",
                "bm25_queries": ["general partner"],
                "role_ids": ["general_partner"],
            },
            seniority={"seniority_bands": ["partner"]},
            query="general partners",
        )
        self.assertEqual(set(filters["seniority_bands"]), {"partner", "c-suite", "owner"})

    def test_non_investing_partner_query_keeps_bands_untouched(self):
        # "partners at law firms" is not investing context; partner stays alone.
        filters, notes = merge(
            self.mod,
            role={
                "semantic_query": "law firm partners",
                "bm25_queries": ["attorney"],
                "role_ids": ["attorney"],
            },
            seniority={"seniority_bands": ["partner"]},
            query="partners at law firms",
        )
        self.assertEqual(filters["seniority_bands"], ["partner"])
        self.assertEqual(notes, [])

    def test_investing_context_without_family_band_does_not_union(self):
        # An investing query filtered to e.g. junior bands must not gain
        # partner/c-suite/owner out of nowhere.
        filters, notes = merge(
            self.mod,
            role={
                "semantic_query": "vc associates sourcing deals",
                "bm25_queries": ["vc associate"],
                "role_ids": ["vc_associate"],
            },
            seniority={"seniority_bands": ["junior", "mid"]},
            query="junior VC associates",
        )
        self.assertEqual(filters["seniority_bands"], ["junior", "mid"])
        self.assertEqual(notes, [])


class InvestorFundingGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("parallel_extractors_guards_test3", EXTRACTORS)

    def test_investing_context_drops_funding_fields_with_note(self):
        filters, notes = merge(
            self.mod,
            role={
                "semantic_query": "general partners leading growth rounds",
                "bm25_queries": ["general partner"],
                "role_ids": ["general_partner"],
            },
            company={
                "entity_types": ["vc_firm"],
                "funding_stage_min": "series_b",
                "funding_stage_max": "series_b",
                "funding_amount_min": 20000000,
            },
            query="partners at firms that can lead a Series B with a $20M check",
        )
        for key in ("funding_stage_min", "funding_stage_max", "funding_amount_min", "funding_amount_max"):
            self.assertNotIn(key, filters)
        self.assertEqual(filters["entity_types"], ["vc_firm"])
        self.assertTrue(any("investor capacity" in note for note in notes), notes)

    def test_operating_company_query_keeps_funding_fields(self):
        filters, notes = merge(
            self.mod,
            role={
                "semantic_query": "software engineers at growth startups",
                "bm25_queries": ["software engineer"],
                "role_ids": ["software_engineer"],
                "departments": ["engineering"],
            },
            company={
                "entity_types": ["venture_backed_startup"],
                "funding_stage_min": "series_b",
                "funding_stage_max": "series_b",
            },
            query="engineers at Series B companies",
        )
        self.assertEqual(filters["funding_stage_min"], "series_b")
        self.assertEqual(filters["funding_stage_max"], "series_b")
        self.assertEqual(notes, [])


class SeriesBRegressionCanaryTests(unittest.TestCase):
    """The diagnosed regression, composed end-to-end at merge level.

    A stubbed expansion for the "Series B lead with a $20M+ check, any
    seniority" brief must produce NO seniority hard filter and NO company
    funding filters, so a GP at a VC firm (e.g. Jordan Bravo at Fixture
    Capital, banded c-suite by the indexer) stays in the pool.
    """

    def test_series_b_lead_brief_yields_no_seniority_and_no_funding_filters(self):
        mod = load_module("parallel_extractors_canary_test", EXTRACTORS)
        query = (
            "find people at any seniority in investing roles in my network at firms "
            "like Fixture Capital that could lead our Series B with a $20M+ check"
        )
        role = {
            "semantic_query": (
                "Investor at a venture capital or growth fund who leads Series B "
                "rounds, writes $20M+ checks, and takes board seats in portfolio companies"
            ),
            "bm25_queries": ["investor", "general partner", "venture partner"],
            "role_ids": ["general_partner"],
            "departments": ["investing"],
        }
        company = {
            "company_names": ["Fixture Capital"],
            "entity_types": ["vc_firm"],
            "funding_stage_min": "series_b",
            "funding_stage_max": "series_b",
            "funding_amount_min": 20000000,
        }
        seniority = {"seniority_bands": ["partner"]}

        filters, notes = merge(mod, role=role, company=company, seniority=seniority, query=query)

        self.assertNotIn("seniority_bands", filters)
        for key in ("funding_stage_min", "funding_stage_max", "funding_amount_min", "funding_amount_max"):
            self.assertNotIn(key, filters)
        self.assertEqual(filters["company_names"], ["Fixture Capital"])
        self.assertEqual(filters["entity_types"], ["vc_firm"])
        self.assertEqual(filters["role_ids"], ["general_partner"])
        self.assertTrue(any("any-seniority" in note for note in notes), notes)
        self.assertTrue(any("investor capacity" in note for note in notes), notes)


class TitleClusterSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("search_network_pipeline_cluster_test", PIPELINE)

    CLUSTERS = [
        {"display_title": "Investor", "stemmed": "investor"},
        {"display_title": "Partner", "stemmed": "partner"},
        {"display_title": "Seed Investor", "stemmed": "seed investor"},
        {"display_title": "Software Engineer", "stemmed": "softwar engin"},
        {"display_title": "General Manager", "stemmed": "gener manag"},
        {"display_title": "Venture Partner", "stemmed": "ventur partner"},
        {"display_title": "Investment Partner", "stemmed": "invest partner"},
    ]

    def test_vc_role_tokens_select_investing_clusters_only(self):
        payload = {
            "normalized_query": "software people who can lead our Series B",
            "role_search_filters": {
                "bm25_queries": [
                    "investor",
                    "venture partner",
                    "investment partner",
                    "general partner",
                    "seed investor",
                ],
            },
        }
        role_tokens = self.mod._role_tokens_for_title_cluster(payload)
        # Role tokens come from bm25/role patterns only, never the raw query.
        self.assertNotIn("software", role_tokens)
        self.assertNotIn("series", role_tokens)

        selected = self.mod._select_title_clusters(self.CLUSTERS, role_tokens, max_clusters=20)
        titles = [cluster["display_title"] for cluster in selected]
        self.assertEqual(
            titles,
            ["Investor", "Partner", "Seed Investor", "Venture Partner", "Investment Partner"],
        )
        # "General Manager" shares "general" with "general partner" but a 1-of-2
        # token overlap must not qualify a short cluster.
        self.assertNotIn("General Manager", titles)
        self.assertNotIn("Software Engineer", titles)

    def test_longer_clusters_need_two_overlapping_tokens(self):
        clusters = [
            {"display_title": "Senior Software Engineer"},
            {"display_title": "General Counsel Operations"},
        ]
        selected = self.mod._select_title_clusters(clusters, {"software", "engineer"}, max_clusters=20)
        self.assertEqual([c["display_title"] for c in selected], ["Senior Software Engineer"])

    def test_empty_role_tokens_select_nothing(self):
        self.assertEqual(self.mod._select_title_clusters(self.CLUSTERS, set(), max_clusters=20), [])

    def test_max_clusters_is_respected(self):
        role_tokens = {"investor", "partner", "seed", "venture", "investment", "general"}
        selected = self.mod._select_title_clusters(self.CLUSTERS, role_tokens, max_clusters=2)
        self.assertEqual(len(selected), 2)


class LimitFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("search_network_pipeline_limit_test", PIPELINE)

    def test_retrieval_limit_applies_judge_pool_floor(self):
        self.assertEqual(self.mod.JUDGE_POOL_FLOOR, 100)
        self.assertEqual(self.mod.retrieval_limit(20), 100)
        self.assertEqual(self.mod.retrieval_limit(100), 100)
        self.assertEqual(self.mod.retrieval_limit(250), 250)
        self.assertEqual(self.mod.retrieval_limit(0), 0)

    def test_local_run_floors_retrieval_and_caps_persist(self):
        # run_pipeline_local configures the process-global local backend mode;
        # reset it so later suites (e.g. TurboPuffer/Postgres ones) stay remote.
        self.addCleanup(search_backend_mode.configure_local_backend, None)
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            db.touch()
            state_path = tmp / "state.json"
            payload_path = tmp / "payload.json"
            payload_path.write_text(json.dumps({
                "normalized_query": "venture partners",
                "role_search_filters": {
                    "semantic_query": "venture partners at investment funds",
                    "bm25_queries": ["venture partner"],
                },
            }), encoding="utf-8")

            recorded: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                recorded.append([str(part) for part in cmd])
                if any(str(part).endswith("task_state.py") for part in cmd):
                    return {"returncode": 0, "json": {"status": "ok", "state": str(state_path)}}
                return {"returncode": 0, "json": {"status": "ok"}}

            args = argparse.Namespace(
                backend="local", db=str(db), ledger=str(tmp / "ledger.json"),
                state=None, query="venture partners", payload_json=str(payload_path),
                env_file="/dev/null", seniority_bands=None, current_role=False,
                limit=20, top_k=None, extra_candidates_json=None,
                search_only=True, filter_only=False, execute_approved=True,
                confirm_llm=False, timeout=30, llm_timeout=30, force=False,
            )
            with mock.patch.object(self.mod, "run", side_effect=fake_run):
                result = self.mod.run_pipeline_local(args)

            self.assertEqual(result["status"], "completed")
            exec_cmd = next(cmd for cmd in recorded if any(part.endswith("execute_role_search.py") for part in cmd))
            self.assertGreaterEqual(int(exec_cmd[exec_cmd.index("--limit") + 1]), 100)
            persist_cmd = next(cmd for cmd in recorded if any(part.endswith("results_io.py") for part in cmd))
            self.assertEqual(persist_cmd[persist_cmd.index("--limit") + 1], "20")

    def test_export_limit_cuts_final_rows(self):
        rio = load_module("results_io_limit_test", RESULTS_IO)
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            candidate_ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(150)]
            state = {
                "task_id": "fixture-task",
                "query": "venture partners",
                "steps": [
                    {"id": "execute_role_search", "output": {"candidate_ids": candidate_ids}},
                    {"id": "hydrate_people", "output": {"profiles": [
                        {"person_id": candidate_ids[0], "name": "Jordan Bravo",
                         "headline": "General Partner at Fixture Capital", "positions": []},
                    ]}},
                ],
            }
            state_path = tmp / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            args = argparse.Namespace(state=str(state_path), out_dir=str(tmp / "artifacts"), name="fixture", limit=20)
            with contextlib.redirect_stdout(io.StringIO()):
                rio.cmd_export(args)

            with (tmp / "artifacts" / "fixture.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 20)
            self.assertEqual(rows[0]["name"], "Jordan Bravo")
            manifest = json.loads((tmp / "artifacts" / "fixture.manifest.json").read_text())
            self.assertEqual(manifest["row_count"], 20)

            # No limit keeps the full frontier.
            args = argparse.Namespace(state=str(state_path), out_dir=str(tmp / "artifacts-all"), name="fixture", limit=0)
            with contextlib.redirect_stdout(io.StringIO()):
                rio.cmd_export(args)
            with (tmp / "artifacts-all" / "fixture.csv").open(newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 150)


if __name__ == "__main__":
    unittest.main()
