from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

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
            with mock.patch.object(publisher, "namespace", return_value=target), \
                 mock.patch.object(publisher, "publish_mappings") as publish_mappings:
                result = publisher.run(output, batch_size=1)
            self.assertEqual(result["status"], "completed")
            target.write.assert_called_once()
            publish_mappings.assert_called_once()

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
