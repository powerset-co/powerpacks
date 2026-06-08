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
        "INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            self.assertEqual(out["summary"]["returned_people"], 1)
            state = json.loads(Path(out["state"]).read_text())
            expand = next(step for step in state["steps"] if step["id"] == "expand_search_request")
            filters = expand["output"]["role_search_filters"]
            self.assertEqual(filters["role_ids"], ["software_engineer"])
            self.assertEqual(filters["bm25_queries"], ["software engineer", "Software Engineer"])
            with Path(out["artifacts"]["csv"]).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["person_id"] for row in rows], [PERSON_STANFORD])

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
            self.assertEqual(filters["set_id"], "wrong-set")
            self.assertEqual(filters["operator_ids"], ["wrong-operator"])
            self.assertEqual(filters["allowed_operator_ids"], ["wrong-operator"])

            hydrate = next(step for step in state["steps"] if step["id"] == "hydrate_people")
            self.assertEqual(hydrate["output"]["source"]["backend"], "duckdb")
            self.assertEqual(hydrate["output"]["source"]["type"], "local_duckdb")

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
