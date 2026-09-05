from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb
from turbopuffer.types import Row

from packs.indexing.lib.artifact_io import write_parquet_rows
from packs.indexing.lib.io import write_jsonl
from packs.indexing.primitives.build_job_description_evidence.build_job_description_evidence import run
from packs.indexing.primitives.publish_job_description_evidence import publish_job_description_evidence as publisher


class BuildJobDescriptionEvidenceTest(unittest.TestCase):
    def test_builds_from_people_position_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            positions = root / "people.records.parquet"
            jobs = root / "jobs.jsonl"
            output = root / "search-index"
            write_parquet_rows(positions, [{
                "id": "position-1",
                "position_id": "position-1",
                "person_id": "person-1",
                "base_id": "person-1",
                "company_domain": "example.com",
                "position_title": "Backend Engineer",
                "raw_title": "Backend Engineer",
                "start_date_epoch": 1_577_836_800,
                "end_date_epoch": 0,
            }])
            write_jsonl(jobs, [{
                "company": "example.com",
                "title": "Senior Backend Engineer",
                "description": "WHAT YOU'LL DO\n" + "Build reliable Haskell services on Kubernetes. " * 10,
                "postedDate": "2024-06-01",
                "location": "Remote",
                "url": "https://example.com/jobs/backend",
                "atsProvider": "ashby",
            }])

            result = run(None, positions, output, jobs_jsonl=[jobs], operator_id="operator-1")

            self.assertEqual(result["job_descriptions"], 1)
            self.assertEqual(result["matches"], 1)
            connection = duckdb.connect()
            try:
                row = connection.execute(
                    "select person_id, company_domain from read_parquet(?)",
                    [str(output / "records/job_description_positions.records.parquet")],
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("person-1", "example.com"))
            preview = publisher.run(output, dry_run=True)
            self.assertEqual(preview["namespace"], "aleph_job_descriptions_v1")
            self.assertEqual(preview["job_descriptions"], 1)
            self.assertEqual(preview["position_matches"], 1)

            target = mock.Mock()
            target.exists.return_value = False
            with mock.patch.object(publisher, "namespace", return_value=target), \
                 mock.patch.object(publisher, "publish_mappings") as publish_mappings:
                result = publisher.run(output, batch_size=1)
            self.assertEqual(result["status"], "completed")
            target.write.assert_called_once()
            publish_mappings.assert_called_once()

    def test_partial_publish_preserves_existing_operator_access(self) -> None:
        stored = {}
        target = mock.Mock()
        target.exists.side_effect = lambda: bool(stored)
        target.query.side_effect = lambda **kwargs: mock.Mock(rows=[
            Row.from_dict(stored[job_id]) for job_id in kwargs["filters"][2] if job_id in stored
        ])
        target.write.side_effect = lambda **kwargs: stored.update({
            row["id"]: dict(row) for row in kwargs["upsert_rows"]
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_parquet_rows(root / publisher.MATCH_RECORD, [], schema={"id": "VARCHAR"})
            with mock.patch.object(publisher, "namespace", return_value=target), \
                 mock.patch.object(publisher, "publish_mappings"):
                for operator in ["operator-alice", "operator-bob", "operator-bob"]:
                    write_parquet_rows(root / publisher.JOB_RECORD, [{
                        "id": "shared-job", "allowed_operator_ids": [operator], "vector": [1.0, 0.0],
                    }])
                    publisher.run(root)
        self.assertEqual(stored["shared-job"]["allowed_operator_ids"], ["operator-alice", "operator-bob"])
        self.assertEqual(target.query.call_count, 2)
        self.assertEqual(target.query.call_args.kwargs["consistency"], {"level": "strong"})

    def test_mapping_upload_does_not_delete_existing_links(self) -> None:
        driver = mock.Mock()
        connection = mock.MagicMock()
        driver.connect.return_value = connection
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        with mock.patch.object(publisher, "ensure_psycopg2", return_value=driver), \
             mock.patch.object(publisher, "database_url", return_value="test-only"):
            publisher.publish_mappings([{
                "id": "link", "job_description_id": "job", "position_id": "position",
                "person_id": "person", "company_domain": "example.com", "match_score": 0.6,
                "match_type": "title_phrase", "posting_position_gap_days": 0,
            }])
        self.assertFalse(any("DELETE" in call.args[0].upper() for call in cursor.execute.call_args_list))
        self.assertIn("ON CONFLICT (id) DO UPDATE", driver.extras.execute_values.call_args.args[1])

    @unittest.skipUnless(os.environ.get("JD_TEST_POSTGRES_URL"), "requires an isolated test Postgres")
    def test_partial_publish_preserves_other_people_and_updates_existing_link(self) -> None:
        import psycopg2

        url = os.environ["JD_TEST_POSTGRES_URL"]
        alice = {
            "id": "shared-job-alice", "job_description_id": "shared-job", "position_id": "alice-position",
            "person_id": "alice", "company_domain": "example.com", "match_score": 0.6,
            "match_type": "title_phrase", "posting_position_gap_days": 0,
        }
        bob = {**alice, "id": "shared-job-bob", "position_id": "bob-position", "person_id": "bob"}
        with mock.patch.object(publisher, "database_url", return_value=url):
            for rows in [[alice], [bob], [{**bob, "match_score": 0.8}], []]:
                publisher.publish_mappings(rows)
        with psycopg2.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT person_id, match_score FROM job_description_positions ORDER BY person_id")
            self.assertEqual(cursor.fetchall(), [("alice", 0.6), ("bob", 0.8)])

    def test_publisher_rejects_local_operator_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            records = root / "records"
            write_parquet_rows(records / "job_descriptions.records.parquet", [{
                "id": "job-1",
                "allowed_operator_ids": ["local"],
            }])
            write_parquet_rows(records / "job_description_positions.records.parquet", [], schema={"id": "VARCHAR"})

            with self.assertRaisesRegex(ValueError, "actual operator id"):
                publisher.run(root, dry_run=True)


if __name__ == "__main__":
    unittest.main()
