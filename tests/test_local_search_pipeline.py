from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "packs/search/primitives/local_search_pipeline/local_search_pipeline.py"
PERSON_STANFORD = "00000000-0000-0000-0000-000000000001"
PERSON_OTHER = "00000000-0000-0000-0000-000000000002"
PERSON_ADJACENT = "00000000-0000-0000-0000-000000000003"
PERSON_SUMMARY = "00000000-0000-0000-0000-000000000004"
PERSON_SIGNAL = "00000000-0000-0000-0000-000000000005"
PERSON_ENTRY_ADJACENT = "00000000-0000-0000-0000-000000000006"
PERSON_GROWTH_ADJACENT = "00000000-0000-0000-0000-000000000007"
PERSON_FOUNDER = "00000000-0000-0000-0000-000000000008"
OPERATOR_ID = "20000000-0000-0000-0000-000000000001"
STANFORD_ID = "linkedin:school:stanford-university"


def load_pipeline_module():
    spec = importlib.util.spec_from_file_location("local_search_pipeline_test", PIPELINE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_local_search_db(path: Path) -> None:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest("duckdb is required for local search pipeline tests") from exc

    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE local_people_positions (
          id VARCHAR,
          base_id VARCHAR,
          person_id VARCHAR,
          position_id VARCHAR,
          position_title VARCHAR,
          city VARCHAR,
          state VARCHAR,
          country VARCHAR,
          metro_areas VARCHAR[],
          role_track VARCHAR,
          seniority_band VARCHAR,
          role_ids VARCHAR[],
          is_current BOOLEAN,
          company_id VARCHAR,
          company_name VARCHAR,
          allowed_operator_ids VARCHAR[],
          phrase_tokens VARCHAR[],
          word_tokens VARCHAR[],
          vector DOUBLE[],
          start_date_epoch BIGINT,
          end_date_epoch BIGINT,
          total_years_experience DOUBLE
        )
        """
    )
    conn.executemany(
        "INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f"{PERSON_STANFORD}-1",
                PERSON_STANFORD,
                PERSON_STANFORD,
                f"{PERSON_STANFORD}-1",
                "Senior Software Engineer",
                "San Francisco",
                "California",
                "United States",
                ["San Francisco Bay Area"],
                "engineering",
                "senior",
                ["software_engineer"],
                True,
                "linkedin:company:one",
                "Company One",
                [OPERATOR_ID],
                ["softwar engin"],
                ["software", "engineer", "software engineer"],
                [1.0, 0.0, 0.0],
                1577836800,
                0,
                8.0,
            ),
            (
                f"{PERSON_OTHER}-1",
                PERSON_OTHER,
                PERSON_OTHER,
                f"{PERSON_OTHER}-1",
                "Software Engineer",
                "New York",
                "New York",
                "United States",
                ["New York City Metropolitan Area"],
                "engineering",
                "mid",
                ["product_manager"],
                True,
                "linkedin:company:two",
                "Company Two",
                [OPERATOR_ID],
                ["softwar engin"],
                ["software", "engineer", "software engineer"],
                [0.9, 0.1, 0.0],
                1577836800,
                0,
                5.0,
            ),
            (
                f"{PERSON_ADJACENT}-1",
                PERSON_ADJACENT,
                PERSON_ADJACENT,
                f"{PERSON_ADJACENT}-1",
                "Backend Engineer",
                "San Francisco",
                "California",
                "United States",
                ["San Francisco Bay Area"],
                "engineering",
                "mid",
                ["backend_engineer"],
                True,
                "linkedin:company:one",
                "Company One",
                [OPERATOR_ID],
                ["backend engin"],
                ["backend", "engineer", "backend engineer"],
                [0.8, 0.2, 0.0],
                1577836800,
                0,
                6.0,
            ),
            (
                f"{PERSON_SUMMARY}-1",
                PERSON_SUMMARY,
                PERSON_SUMMARY,
                f"{PERSON_SUMMARY}-1",
                "Platform Operations",
                "Austin",
                "Texas",
                "United States",
                ["Austin Metropolitan Area"],
                "engineering",
                "senior",
                ["software_engineer"],
                True,
                "linkedin:company:two",
                "Company Two",
                [OPERATOR_ID],
                ["platform oper"],
                ["platform", "operations", "platform operations"],
                [0.1, 0.9, 0.0],
                1577836800,
                0,
                7.0,
            ),
            (
                f"{PERSON_SIGNAL}-1",
                PERSON_SIGNAL,
                PERSON_SIGNAL,
                f"{PERSON_SIGNAL}-1",
                "Customer Success Specialist",
                "Denver",
                "Colorado",
                "United States",
                ["Denver Metropolitan Area"],
                "engineering",
                "mid",
                ["software_engineer"],
                True,
                "linkedin:company:signals",
                "Signals Company",
                [OPERATOR_ID],
                ["custom success specialist"],
                ["customer", "success", "specialist", "customer success"],
                [0.2, 0.8, 0.0],
                1577836800,
                0,
                4.0,
            ),
            (
                f"{PERSON_ENTRY_ADJACENT}-1",
                PERSON_ENTRY_ADJACENT,
                PERSON_ENTRY_ADJACENT,
                f"{PERSON_ENTRY_ADJACENT}-1",
                "Growth Lead",
                "San Francisco",
                "California",
                "United States",
                ["San Francisco Bay Area"],
                "marketing",
                "entry",
                ["marketing_manager"],
                True,
                "linkedin:company:one",
                "Company One",
                [OPERATOR_ID],
                [],
                ["growth", "lead", "growth lead"],
                [0.3, 0.7, 0.0],
                1577836800,
                0,
                1.0,
            ),
            (
                f"{PERSON_GROWTH_ADJACENT}-1",
                PERSON_GROWTH_ADJACENT,
                PERSON_GROWTH_ADJACENT,
                f"{PERSON_GROWTH_ADJACENT}-1",
                "Growth Lead",
                "San Francisco",
                "California",
                "United States",
                ["San Francisco Bay Area"],
                "marketing",
                "senior",
                ["marketing_manager"],
                True,
                "linkedin:company:one",
                "Company One",
                [OPERATOR_ID],
                [],
                ["growth", "lead", "growth lead"],
                [0.4, 0.6, 0.0],
                1577836800,
                0,
                6.0,
            ),
            (
                f"{PERSON_FOUNDER}-1",
                PERSON_FOUNDER,
                PERSON_FOUNDER,
                f"{PERSON_FOUNDER}-1",
                "Founder",
                "Palo Alto",
                "California",
                "United States",
                ["San Francisco Bay Area"],
                "general",
                "c_suite",
                ["founder"],
                True,
                "linkedin:company:founder",
                "Founder Co",
                [OPERATOR_ID],
                ["founder"],
                ["founder"],
                [0.5, 0.5, 0.0],
                1577836800,
                0,
                12.0,
            ),
        ],
    )
    for ddl in [
        "ALTER TABLE local_people_positions ADD COLUMN x_twitter_followers BIGINT",
        "ALTER TABLE local_people_positions ADD COLUMN linkedin_followers BIGINT",
        "ALTER TABLE local_people_positions ADD COLUMN linkedin_connections BIGINT",
        "ALTER TABLE local_people_positions ADD COLUMN ig_followers BIGINT",
    ]:
        conn.execute(ddl)
    conn.execute(
        """
        UPDATE local_people_positions
        SET x_twitter_followers = CASE WHEN base_id = ? THEN 1000 ELSE 100 END,
            linkedin_followers = CASE WHEN base_id = ? THEN 5000 ELSE 500 END,
            linkedin_connections = CASE WHEN base_id = ? THEN 3000 ELSE 400 END,
            ig_followers = CASE WHEN base_id = ? THEN 100 ELSE 10 END
        """,
        [PERSON_STANFORD, PERSON_STANFORD, PERSON_STANFORD, PERSON_STANFORD],
    )
    conn.execute(
        """
        CREATE TABLE local_people_education (
          id VARCHAR,
          base_id VARCHAR,
          person_id VARCHAR,
          canonical_education_id VARCHAR,
          school_name VARCHAR,
          degree VARCHAR,
          degree_normalized VARCHAR,
          field_of_study VARCHAR,
          start_year BIGINT,
          end_year BIGINT,
          graduation_year BIGINT,
          allowed_operator_ids VARCHAR[]
        )
        """
    )
    conn.executemany(
        "INSERT INTO local_people_education VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f"{PERSON_STANFORD}-edu",
                PERSON_STANFORD,
                PERSON_STANFORD,
                STANFORD_ID,
                "Stanford University",
                "BS",
                "Bachelors",
                "Computer Science",
                2010,
                2014,
                2014,
                [OPERATOR_ID],
            ),
            (
                f"{PERSON_OTHER}-edu",
                PERSON_OTHER,
                PERSON_OTHER,
                "linkedin:school:berkeley",
                "University of California, Berkeley",
                "BS",
                "Bachelors",
                "Computer Science",
                2010,
                2014,
                2014,
                [OPERATOR_ID],
            ),
        ],
    )
    conn.execute(
        """
        CREATE TABLE local_education (
          id VARCHAR,
          canonical_education_id VARCHAR,
          school_name VARCHAR,
          display_value VARCHAR,
          person_count BIGINT
        )
        """
    )
    conn.execute(
        "INSERT INTO local_education VALUES (?, ?, ?, ?, ?)",
        [STANFORD_ID, STANFORD_ID, "Stanford University", "Stanford University", 1],
    )
    conn.execute(
        """
        CREATE TABLE local_summaries (
          id VARCHAR,
          base_id VARCHAR,
          person_id VARCHAR,
          summary VARCHAR,
          tech_skills VARCHAR[],
          allowed_operator_ids VARCHAR[]
        )
        """
    )
    conn.executemany(
        "INSERT INTO local_summaries VALUES (?, ?, ?, ?, ?, ?)",
        [
            (PERSON_STANFORD, PERSON_STANFORD, PERSON_STANFORD, "Builds production software systems.", ["Python"], [OPERATOR_ID]),
            (PERSON_OTHER, PERSON_OTHER, PERSON_OTHER, "Builds backend services.", ["Go"], [OPERATOR_ID]),
            (PERSON_ADJACENT, PERSON_ADJACENT, PERSON_ADJACENT, "Builds backend systems at Company One.", ["Python"], [OPERATOR_ID]),
            (PERSON_SUMMARY, PERSON_SUMMARY, PERSON_SUMMARY, "Database architect for distributed storage systems.", ["Postgres"], [OPERATOR_ID]),
            (PERSON_SIGNAL, PERSON_SIGNAL, PERSON_SIGNAL, "Database architect signal operator.", ["SQL"], [OPERATOR_ID]),
            (PERSON_ENTRY_ADJACENT, PERSON_ENTRY_ADJACENT, PERSON_ENTRY_ADJACENT, "Entry level growth support.", ["Marketing"], [OPERATOR_ID]),
            (PERSON_GROWTH_ADJACENT, PERSON_GROWTH_ADJACENT, PERSON_GROWTH_ADJACENT, "Senior growth lead at Company One.", ["Marketing"], [OPERATOR_ID]),
            (PERSON_FOUNDER, PERSON_FOUNDER, PERSON_FOUNDER, "Founded Founder Co.", ["Leadership"], [OPERATOR_ID]),
        ],
    )
    conn.execute(
        """
        CREATE TABLE local_company_signals (
          id VARCHAR,
          company_id VARCHAR,
          company_urn VARCHAR,
          signals_text VARCHAR,
          summary VARCHAR,
          doc2query_text VARCHAR,
          word_tokens VARCHAR[],
          signal_tokens VARCHAR[],
          vector DOUBLE[],
          allowed_operator_ids VARCHAR[]
        )
        """
    )
    conn.executemany(
        "INSERT INTO local_company_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "signal-one",
                "linkedin:company:signals",
                "linkedin:company:signals",
                "Database architect platform signal",
                "Company hires database architects.",
                "database architect distributed systems",
                ["database", "architect", "database architect", "platform"],
                ["database", "architect", "platform"],
                [0.0, 1.0, 0.0],
                [OPERATOR_ID],
            )
        ],
    )
    conn.close()


class LocalSearchPipelineTests(unittest.TestCase):
    def test_normalize_query_expansion_payload_accepts_prod_role_schema(self) -> None:
        mod = load_pipeline_module()
        payload = {
            "original_query": "software engineers in sf that went to stanford",
            "traits": [{"value": "Software engineer", "temporal": "current", "meaning": "role"}],
            "filters": {
                "role_semantic_query": "Software engineers building production services and distributed systems.",
                "role_bm25_queries": ["software engineer", "backend engineer"],
                "role_core_patterns": [
                    {"regex": "software\\s+engineer", "examples": ["Software Engineer", "Backend Engineer"]}
                ],
                "education_ids": [{"id": STANFORD_ID, "display_value": "Stanford University"}],
                "metro_areas": [{"id": "San Francisco Bay Area", "display_value": "San Francisco Bay Area"}],
                "seniority_bands": [{"id": "senior", "display_value": "Senior"}],
            },
        }

        normalized = mod.normalize_query_expansion_payload(payload)
        filters = normalized["role_search_filters"]

        self.assertEqual(normalized["intent_type"], "role_search")
        self.assertEqual(normalized["normalized_query"], payload["original_query"])
        self.assertEqual(filters["semantic_query"], payload["filters"]["role_semantic_query"])
        self.assertEqual(filters["role_semantic_query"], payload["filters"]["role_semantic_query"])
        self.assertEqual(filters["education_names"], ["Stanford University"])
        self.assertEqual(filters["education_ids"], [STANFORD_ID])
        self.assertEqual(filters["metro_areas"], ["San Francisco Bay Area"])
        self.assertEqual(filters["seniority_bands"], ["senior"])
        self.assertEqual(
            filters["bm25_queries"],
            ["software engineer", "backend engineer", "Software Engineer", "Backend Engineer"],
        )
        self.assertEqual(normalized["traits"], payload["traits"])
        self.assertTrue(filters["is_current_role"])

    def test_normalize_query_expansion_payload_derives_currentness_from_traits(self) -> None:
        # Regression: a temporal=current role trait must become
        # is_current_role=true at the prepare boundary, otherwise past
        # senior/staff positions admit people who have since moved on.
        mod = load_pipeline_module()
        payload = {
            "original_query": "senior or staff backend infrastructure engineers",
            "traits": [
                {
                    "meaning": "role",
                    "temporal": "current",
                    "value": "Senior or staff backend infrastructure engineer",
                }
            ],
            "role_search_filters": {
                "semantic_query": "Senior backend infrastructure engineers.",
                "bm25_queries": ["backend engineer"],
                "seniority_bands": ["senior", "staff"],
            },
        }

        filters = mod.normalize_query_expansion_payload(payload)["role_search_filters"]
        self.assertTrue(filters["is_current_role"])
        self.assertEqual(filters["seniority_bands"], ["senior", "staff"])

        past = mod.normalize_query_expansion_payload(
            {
                "original_query": "ex-Stripe engineers",
                "traits": [{"meaning": "company", "temporal": "past", "value": "Stripe"}],
                "role_search_filters": {"bm25_queries": ["engineer"], "company_names": ["Stripe"]},
            }
        )["role_search_filters"]
        self.assertFalse(past["is_current_company"])

        untouched = mod.normalize_query_expansion_payload(
            {
                "original_query": "backend engineers",
                "role_search_filters": {"bm25_queries": ["backend engineer"]},
            }
        )["role_search_filters"]
        self.assertNotIn("is_current_role", untouched)
        self.assertNotIn("is_current_company", untouched)

    def test_prod_shaped_role_expansion_reaches_local_duckdb_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "prod-expand.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "original_query": "software engineers",
                "filters": {
                    "role_bm25_queries": ["software engineer"],
                    "role_ids": [{"id": "software_engineer", "display_value": "Software Engineer"}],
                    "role_core_patterns": [{"regex": "software\\s+engineer", "examples": ["Software Engineer"]}],
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "software engineers",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            # Non-shortcut role_ids no longer hard-gate retrieval (deployed
            # network-search-api parity), so the bm25/semantic frontier is
            # wider than the single tagged software engineer.
            self.assertGreaterEqual(out["summary"]["returned_people"], 1)
            state = json.loads(Path(out["state"]).read_text())
            expand = next(step for step in state["steps"] if step["id"] == "expand_search_request")
            filters = expand["output"]["role_search_filters"]
            self.assertEqual(filters["role_ids"], ["software_engineer"])
            self.assertIn("software engineer", filters["bm25_queries"])
            self.assertIn("Software Engineer", filters["bm25_queries"])
            self.assertIn("Senior Software Engineer", filters["bm25_queries"])
            self.assertEqual(filters["local_title_clusters"][0]["stemmed"], "softwar engin")
            self.assertEqual(filters["local_title_clustering_status"]["status"], "completed")
            self.assertGreater(filters["local_title_clustering_status"]["selected_count"], 0)
            self.assertTrue(any(pattern.get("source") == "local_title_cluster" for pattern in filters["role_core_patterns"]))
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")
            candidate = retrieval["output"]["candidates"][0]
            self.assertIn("hybrid", candidate["vertical_sources"])
            self.assertTrue(candidate["has_core_regex"])
            self.assertEqual(candidate["bucket"], "good")
            with Path(out["artifacts"]["csv"]).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn(PERSON_STANFORD, [row["person_id"] for row in rows])

    def test_company_union_uses_static_adjacent_role_ids_against_duckdb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "prod-expand-company-union.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "original_query": "software engineers at AI companies",
                "filters": {
                    "has_domain_intent": True,
                    "company_ids": [{"id": "linkedin:company:one", "display_value": "Company One"}],
                    "role_bm25_queries": ["software engineer"],
                    "role_ids": [{"id": "software_engineer", "display_value": "Software Engineer"}],
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "software engineers at AI companies",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            self.assertEqual(out["summary"]["search_mode"], "COMPANY_UNION")
            self.assertEqual(out["summary"]["company_union_candidates"], 1)
            self.assertEqual(out["summary"]["company_union_added"], 1)
            state = json.loads(Path(out["state"]).read_text())
            prefilters = next(step for step in state["steps"] if step["id"] == "apply_prefilters")
            self.assertEqual(prefilters["output"]["stages"][0]["adjacency_method"], "role_id")
            self.assertEqual(prefilters["output"]["company_union_candidate_ids"], [PERSON_ADJACENT])

            with Path(out["artifacts"]["csv"]).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            person_ids = [row["person_id"] for row in rows]
            # Base retrieval is no longer hard-gated by non-shortcut role_ids;
            # assert the adjacency union still contributes the adjacent person.
            self.assertIn(PERSON_STANFORD, person_ids)
            self.assertIn(PERSON_ADJACENT, person_ids)

    def test_execute_runs_summary_and_company_signal_pools_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "prod-expand-verticals.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "original_query": "software engineer database architects",
                "filters": {
                    "role_bm25_queries": ["software engineer", "database architect"],
                    "role_ids": [{"id": "software_engineer", "display_value": "Software Engineer"}],
                    "role_core_patterns": [{"regex": "software\\s+engineer", "examples": ["Software Engineer"]}],
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "software engineer database architects",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            state = json.loads(Path(out["state"]).read_text())
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")["output"]
            self.assertEqual(retrieval["verticals"]["role"]["status"], "completed")
            self.assertGreaterEqual(retrieval["verticals"]["role"]["row_count"], 1)
            self.assertEqual(retrieval["verticals"]["summary"]["status"], "completed")
            self.assertGreaterEqual(retrieval["verticals"]["summary"]["row_count"], 1)
            self.assertEqual(retrieval["verticals"]["company_signal"]["status"], "completed")
            self.assertGreaterEqual(retrieval["verticals"]["company_signal"]["row_count"], 1)
            self.assertGreaterEqual(retrieval["vertical_source_counts"].get("hybrid", 0), 1)
            self.assertGreaterEqual(retrieval["vertical_source_counts"].get("summary", 0), 1)
            self.assertGreaterEqual(retrieval["vertical_source_counts"].get("company_signal", 0), 1)

            by_person = {candidate["person_id"]: candidate for candidate in retrieval["candidates"]}
            self.assertIn("hybrid", by_person[PERSON_STANFORD]["vertical_sources"])
            self.assertIn("summary", by_person[PERSON_STANFORD]["vertical_sources"])
            self.assertTrue(by_person[PERSON_STANFORD]["has_core_regex"])
            self.assertIn("summary", by_person[PERSON_SUMMARY]["vertical_sources"])
            self.assertIsNone(by_person[PERSON_SUMMARY]["position_id"])
            self.assertEqual(by_person[PERSON_SUMMARY]["matched_position_ids"], [])
            self.assertIn("summary", by_person[PERSON_SIGNAL]["vertical_sources"])
            self.assertIn("company_signal", by_person[PERSON_SIGNAL]["vertical_sources"])
            self.assertIsNotNone(by_person[PERSON_SIGNAL]["position_id"])
            self.assertTrue(by_person[PERSON_SIGNAL]["matched_position_ids"])

    def test_company_signal_pool_uses_vector_when_semantic_payload_has_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "prod-expand-company-signal-vector.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "original_query": "platform companies hiring database architects",
                "filters": {
                    "role_semantic_query": "People at companies with database architecture and platform engineering signals.",
                    "query_embedding": [0.0, 1.0, 0.0],
                    "role_ids": [{"id": "software_engineer", "display_value": "Software Engineer"}],
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "platform companies hiring database architects",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            state = json.loads(Path(out["state"]).read_text())
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")["output"]
            self.assertEqual(retrieval["verticals"]["company_signal"]["status"], "completed")
            self.assertGreaterEqual(retrieval["verticals"]["company_signal"]["row_count"], 1)
            by_person = {candidate["person_id"]: candidate for candidate in retrieval["candidates"]}
            self.assertIn("company_signal", by_person[PERSON_SIGNAL]["vertical_sources"])

    def test_founder_payload_skips_regex_pattern_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "prod-expand-founder.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "original_query": "founders",
                "filters": {
                    "role_function": "founder",
                    "role_bm25_queries": ["founder"],
                    "role_ids": [{"id": "founder", "display_value": "Founder"}],
                    "role_core_patterns": [{"regex": "founder", "examples": ["Founder"]}],
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "founders",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            state = json.loads(Path(out["state"]).read_text())
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")["output"]
            candidate = next(candidate for candidate in retrieval["candidates"] if candidate["person_id"] == PERSON_FOUNDER)
            self.assertIn("hybrid", candidate["vertical_sources"])
            self.assertNotIn("has_core_regex", candidate)
            self.assertNotIn("bucket", candidate)

    def test_summary_vertical_recovers_person_whose_role_match_is_past_position(self) -> None:
        # Parity regression: prod's summary vertical builds its eligibility
        # prefilter with is_current=None (person-level bio search), so a
        # current co-founder whose only engineering positions are PAST still
        # qualifies when the query asks for current software engineers. Local
        # must not forward the role vertical's is_current clause to the
        # summary vertical.
        person_past_eng = "00000000-0000-0000-0000-000000000009"
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "payload.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            import duckdb  # type: ignore

            conn = duckdb.connect(str(db))
            conn.execute(
                "INSERT INTO local_people_positions (id, base_id, person_id, position_id, position_title, city, state, country, metro_areas, role_track, seniority_band, role_ids, is_current, company_id, company_name, allowed_operator_ids, phrase_tokens, word_tokens, vector, start_date_epoch, end_date_epoch, total_years_experience) VALUES "
                "(?, ?, ?, ?, 'Co-Founder', 'San Francisco', 'California', 'United States', ['San Francisco Bay Area'], 'general', 'owner', ['founder'], TRUE, 'linkedin:company:newco', 'NewCo', ?, ['founder'], ['founder'], [0.5, 0.5, 0.0], 1672531200, 0, 10.0), "
                "(?, ?, ?, ?, 'Software Engineer', 'San Francisco', 'California', 'United States', ['San Francisco Bay Area'], 'engineering', 'mid', ['software_engineer'], FALSE, 'linkedin:company:one', 'Company One', ?, ['softwar engin'], ['software', 'engineer', 'software engineer'], [0.9, 0.0, 0.1], 1577836800, 1672531199, 10.0)",
                [
                    f"{person_past_eng}-1", person_past_eng, person_past_eng, f"{person_past_eng}-1", [OPERATOR_ID],
                    f"{person_past_eng}-2", person_past_eng, person_past_eng, f"{person_past_eng}-2", [OPERATOR_ID],
                ],
            )
            conn.execute(
                "INSERT INTO local_summaries VALUES (?, ?, ?, 'Co-founder who previously built production software systems as a software engineer.', ['Python'], ?)",
                [person_past_eng, person_past_eng, person_past_eng, [OPERATOR_ID]],
            )
            conn.close()

            payload = {
                "intent_type": "role_search",
                "normalized_query": "software engineers in SF",
                "vertical": "people",
                "role_search_filters": {
                    "bm25_queries": ["software engineer"],
                    "role_tracks": ["engineering"],
                    "metro_areas": ["San Francisco Bay Area"],
                },
                "traits": [
                    {"value": "Software engineer", "temporal": "current", "meaning": "role"},
                ],
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "software engineers in sf",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            state = json.loads(Path(out["state"]).read_text())
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")["output"]
            # The role vertical keeps is_current=true so the past engineering
            # position cannot create a fake current-role hit.
            self.assertIn(["is_current", "Eq", True], json.loads(json.dumps(retrieval["applied_filter"]))[1])
            by_person = {candidate["person_id"]: candidate for candidate in retrieval["candidates"]}
            self.assertIn(person_past_eng, by_person, f"summary vertical should recover {person_past_eng}")
            self.assertEqual(by_person[person_past_eng]["vertical_sources"], ["summary"])
            self.assertIsNone(by_person[person_past_eng]["position_id"])
            # Current-position engineer still arrives through the role vertical.
            self.assertIn("hybrid", by_person[PERSON_STANFORD]["vertical_sources"])

    def test_company_union_bm25_adjacency_uses_word_fallback_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "prod-expand-bm25-company-union.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "original_query": "growth leaders at AI companies",
                "filters": {
                    "has_domain_intent": True,
                    "company_ids": [{"id": "linkedin:company:one", "display_value": "Company One"}],
                    "role_bm25_queries": ["growth leader"],
                    "company_adjacency_queries": ["Growth Lead", "growth lead"],
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "growth leaders at AI companies",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            self.assertEqual(out["summary"]["search_mode"], "COMPANY_UNION")
            state = json.loads(Path(out["state"]).read_text())
            prefilters = next(step for step in state["steps"] if step["id"] == "apply_prefilters")["output"]
            stage = prefilters["stages"][0]
            self.assertEqual(stage["adjacency_method"], "bm25")
            self.assertEqual(stage["adjacency_query_source"], "llm+static")
            self.assertEqual(stage["adjacency_queries"][:1], ["Growth Lead"])
            self.assertIn(PERSON_GROWTH_ADJACENT, prefilters["company_union_candidate_ids"])
            self.assertNotIn(PERSON_ENTRY_ADJACENT, prefilters["company_union_candidate_ids"])
            growth_candidate = next(
                candidate
                for candidate in prefilters["company_union_candidates"]
                if candidate["person_id"] == PERSON_GROWTH_ADJACENT
            )
            self.assertIn(0, growth_candidate["adjacency_query_indexes"])
            self.assertEqual(growth_candidate["retrieval_mode"], "company_adjacency_bm25")
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")["output"]
            by_person = {candidate["person_id"]: candidate for candidate in retrieval["candidates"]}
            self.assertIn("company_filter", by_person[PERSON_GROWTH_ADJACENT]["vertical_sources"])

    def test_run_uses_duckdb_scope_without_set_resolution_or_postgres(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            db = tmp / "local-search.duckdb"
            payload_path = tmp / "payload.json"
            ledger = tmp / "ledger.json"
            write_local_search_db(db)
            payload = {
                "intent_type": "role_search",
                "source_type": "unit_test",
                "normalized_query": "software engineers in sf that went to stanford",
                "vertical": "people",
                "role_search_filters": {
                    "set_id": "wrong-set",
                    "operator_ids": ["wrong-operator"],
                    "allowed_operator_ids": ["wrong-operator"],
                    "education_names": ["Stanford University"],
                    "metro_areas": ["San Francisco Bay Area"],
                    "role_tracks": ["engineering"],
                    "is_current_role": True,
                    "li_followers_min": 1000,
                },
            }
            payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            env = dict(os.environ)
            env.update({
                "POWERPACKS_DEFAULT_SET_ID": "wrong-default-set",
                "POWERSET_DEFAULT_SET_ID": "wrong-default-set",
                "DATABASE_URL": "postgresql://should-not-be-used",
                "TURBOPUFFER_API_KEY": "should-not-be-used",
            })
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--search-only",
                    "--db",
                    str(db),
                    "--ledger",
                    str(ledger),
                    "--query",
                    "software engineers in sf that went to stanford",
                    "--payload-json",
                    str(payload_path),
                    "--limit",
                    "0",
                    "--top-k",
                    "50",
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], "completed")
            self.assertEqual(out["mode"], "local_duckdb")
            self.assertEqual(out["summary"]["returned_people"], 1)
            self.assertEqual(out["summary"]["hydrated"], 1)
            self.assertEqual(out["summary"]["rows"], 1)
            self.assertEqual(set(out["ignored_remote_scope_keys"]), {"allowed_operator_ids", "operator_ids", "set_id"})

            state = json.loads(Path(out["state"]).read_text())
            step_ids = [step["id"] for step in state["steps"]]
            self.assertNotIn("resolve_set_operators", step_ids)
            expand = next(step for step in state["steps"] if step["id"] == "expand_search_request")
            filters = expand["output"]["role_search_filters"]
            # There is no concept of a set/operator locally: the scope keys are
            # stripped from the executable payload outright (reported via
            # ignored_remote_scope_keys above), not carried along and ignored.
            self.assertNotIn("set_id", filters)
            self.assertNotIn("operator_ids", filters)
            self.assertNotIn("allowed_operator_ids", filters)
            self.assertNotIn("wrong-operator", json.dumps(state))

            hydrate = next(step for step in state["steps"] if step["id"] == "hydrate_people")
            self.assertEqual(hydrate["output"]["source"]["backend"], "duckdb")
            self.assertEqual(hydrate["output"]["source"]["type"], "local_duckdb")
            retrieval = next(step for step in state["steps"] if step["id"] == "execute_role_search")
            # The foreign operator scope must be ignored end to end. With local
            # mode configured before parent-side transforms, prepare-time title
            # clustering is no longer zeroed by the wrong-operator filter, so it
            # contributes BM25 hints and retrieval runs hybrid instead of
            # degrading to filter_only.
            self.assertIn("hybrid", retrieval["output"]["candidates"][0]["vertical_sources"])

            ledger_doc = json.loads(ledger.read_text())
            for step in ["resolve_education", "apply_prefilters", "execute_role_search", "hydrate_people"]:
                command = ledger_doc["steps"][step]["command"]
                self.assertIn("packs/search/primitives/local_duckdb/", command)
                self.assertIn("--db", command)
            self.assertNotIn("POWERPACKS_LOCAL_SEARCH_DB", json.dumps(ledger_doc))

            with Path(out["artifacts"]["csv"]).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["person_id"], PERSON_STANFORD)
            self.assertEqual(rows[0]["hydrated"], "True")


if __name__ == "__main__":
    unittest.main()
