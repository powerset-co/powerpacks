from __future__ import annotations

import unittest
from pathlib import Path
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
