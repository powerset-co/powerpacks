from __future__ import annotations

import csv
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
        "INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

            with Path(out["artifacts"]["csv"]).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["person_id"], PERSON_STANFORD)
            self.assertEqual(rows[0]["hydrated"], "True")


if __name__ == "__main__":
    unittest.main()
