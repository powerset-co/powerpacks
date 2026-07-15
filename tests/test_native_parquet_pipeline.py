from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.indexing.lib.artifact_io import iter_artifact_rows, write_parquet_rows
from packs.indexing.lib.io import write_jsonl
from packs.indexing.modal.run_indexing import WORK_TO_CACHE
from packs.indexing.modal.sandbox_common import merge_cache_file
from packs.indexing.primitives.build_processing_pipeline import build_processing_pipeline as pipeline


def completed_embedding_result(rows: int) -> dict:
    return {
        "status": "completed",
        "counts": {
            "embeddings": rows,
            "input_rows_processed": rows,
            "chunks_written": 1,
            "artifact_hits": rows,
            "artifact_misses": 0,
            "paid_calls": 0,
        },
    }


class NativeParquetPipelineTests(unittest.TestCase):
    def test_writer_supports_integer_contract_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "counts.parquet"

            write_parquet_rows(
                path,
                [{"id": "one", "count": 7}],
                schema={"id": "VARCHAR", "count": "INTEGER"},
            )

            self.assertEqual(list(iter_artifact_rows(path)), [{"id": "one", "count": 7}])

    def test_embedding_working_paths_have_no_jsonl_aliases(self) -> None:
        ps = pipeline.paths(Path("/tmp/native-parquet-paths"))
        self.assertEqual(ps["roles_embeddings"].name, "roles_with_embeddings.parquet")
        self.assertEqual(ps["company_embeddings"].name, "company_embeddings_v3.parquet")
        self.assertEqual(ps["summary_embeddings"].name, "summary_embeddings.parquet")
        self.assertNotIn("aleph_roles_embeddings", ps)
        self.assertNotIn("summary_embeddings_legacy", ps)
        for key in ("people_records", "companies_records", "schools_records", "education_records", "summaries_records"):
            self.assertEqual(ps[key].suffix, ".parquet")
        self.assertEqual(
            WORK_TO_CACHE["roles/roles_with_embeddings.parquet"],
            "artifacts/roles_with_embeddings.parquet",
        )
        self.assertNotIn("roles/roles_with_embeddings.jsonl", WORK_TO_CACHE)

    def test_modal_refresh_merges_native_parquet_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_artifact = root / "run.parquet"
            cache = root / "cache.parquet"
            pipeline._write_embedding_parquet(
                run_artifact,
                [{"person_id": "person-1", "embedding": [0.1, 0.2]}],
                vector_field="embedding",
            )

            self.assertEqual(merge_cache_file(run_artifact, cache, ("person_id",)), (1, 0))
            self.assertEqual(list(iter_artifact_rows(cache))[0]["person_id"], "person-1")

    def test_role_stage_shapes_native_parquet_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ps = pipeline.paths(root)
            ps["roles_dense"].parent.mkdir(parents=True)
            write_jsonl(ps["roles_dense"], [{"title_hash": "role-1", "dense_text": "Engineer"}])
            row = {
                "id": "role-1",
                "embedding": [0.1, 0.2],
                "cluster": "engineering",
                "dense_text": "Engineer",
                "description": "",
                "doc2query": [],
                "inferred_skills": [],
                "raw_title": "Engineer",
                "role_ids": [],
                "role_track": "engineering",
                "role_type": "individual_contributor",
                "seniority_band": "mid",
                "specialization": "",
                "title_hash": "role-1",
            }

            def embed(*args, **kwargs):
                pipeline._write_embedding_parquet(ps["roles_embeddings"], [row], vector_field="embedding")
                return completed_embedding_result(1)

            with mock.patch.object(pipeline, "_run_embedding_stage", side_effect=embed):
                pipeline.step_role_embeddings({"run_dir": str(root)}, ps)

            rows = list(iter_artifact_rows(ps["roles_embeddings"]))
            self.assertEqual(rows[0]["title_hash"], "role-1")
            self.assertEqual(rows[0]["dense_embedding"], [0.10000000149011612, 0.20000000298023224])
            self.assertNotIn("embedding", rows[0])
            self.assertFalse((root / "unified/roles/roles_with_embeddings.jsonl").exists())

    def test_company_and_summary_stages_join_from_native_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ps = pipeline.paths(root)
            ps["companies_corpus_v3"].parent.mkdir(parents=True)
            write_jsonl(ps["companies_corpus_v3"], [{"company_urn": "company-1", "company_name": "One"}])
            write_parquet_rows(ps["companies_records"], [{"id": "company-1", "company_urn": "company-1", "company_name": "One"}])

            def embed_company(*args, **kwargs):
                pipeline._write_embedding_parquet(ps["company_embeddings"], [{
                    "id": "company-1",
                    "company_urn": "company-1",
                    "company_name": "One",
                    "semantic_text": "One company",
                    "embedding": [0.25, 0.5],
                }], vector_field="embedding")
                return completed_embedding_result(1)

            with mock.patch.object(pipeline, "_run_embedding_stage", side_effect=embed_company):
                pipeline.step_company_embeddings({"run_dir": str(root)}, ps)

            company_rows = list(iter_artifact_rows(ps["company_embeddings"]))
            self.assertEqual(set(company_rows[0]), {"company_urn", "company_name", "semantic_text", "embedding"})
            self.assertEqual(list(iter_artifact_rows(ps["companies_records"]))[0]["vector"], [0.25, 0.5])

            ps["summary_internal"].parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(ps["summary_internal"], [{"person_id": "person-1", "base_id": "person-1", "text": "Summary"}])
            write_parquet_rows(ps["summaries_records"], [{"id": "person-1", "person_id": "person-1"}])

            def embed_summary(*args, **kwargs):
                pipeline._write_embedding_parquet(ps["summary_embeddings"], [{
                    "id": "person-1",
                    "person_id": "person-1",
                    "embedding": [0.75, 1.0],
                }], vector_field="embedding")
                return completed_embedding_result(1)

            with mock.patch.object(pipeline, "_run_embedding_stage", side_effect=embed_summary):
                pipeline.step_summary_embeddings({"run_dir": str(root)}, ps)

            summary_rows = list(iter_artifact_rows(ps["summary_embeddings"]))
            self.assertEqual(set(summary_rows[0]), {"person_id", "embedding"})
            self.assertEqual(list(iter_artifact_rows(ps["summaries_records"]))[0]["vector"], [0.75, 1.0])
            self.assertFalse((root / "summaries/summary_embeddings.jsonl").exists())

    def test_final_record_parquet_hashes_are_computed_during_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records/people.records.parquet"
            hashes = root / "records/people.records.hashes.json"
            rows = [{"id": "one", "person_id": "one", "vector": [0.1, 0.2]}]

            first = pipeline.write_record_parquet_with_hashes(records, iter(rows), hashes)
            second = pipeline.write_record_parquet_with_hashes(records, iter(rows), hashes)

            self.assertEqual(first["new_rows"], 1)
            self.assertTrue(first["file_written"])
            self.assertEqual(second["unchanged_rows"], 1)
            self.assertFalse(second["file_written"])
            stored = list(iter_artifact_rows(records))[0]
            self.assertEqual(stored["id"], "one")
            self.assertEqual(stored["vector"], [0.10000000149011612, 0.20000000298023224])


if __name__ == "__main__":
    unittest.main()
