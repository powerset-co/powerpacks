from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from packs.indexing.lib.artifact_io import artifact_id_set, iter_artifact_rows
from packs.indexing.modal.sandbox_common import materialize_parquet_records, merge_cache_file
from packs.indexing.primitives.embed_records_checkpointed.embed_records_checkpointed import load_input_embeddings


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ParquetEmbeddingCacheTests(unittest.TestCase):
    def test_parquet_cache_merge_preserves_old_rows_and_new_rows_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "summary_embeddings.parquet"
            initial = root / "initial.jsonl"
            update = root / "update.jsonl"
            write_jsonl(initial, [
                {"person_id": "old", "embedding": [0.1, 0.2]},
                {"person_id": "shared", "embedding": [0.3, 0.4]},
            ])
            self.assertEqual(
                merge_cache_file(initial, cache, ("person_id",), vector_field="embedding"),
                (2, 0),
            )

            write_jsonl(update, [
                {"person_id": "shared", "embedding": [0.7, 0.8]},
                {"person_id": "new", "embedding": [0.9, 1.0]},
            ])
            self.assertEqual(
                merge_cache_file(update, cache, ("person_id",), vector_field="embedding"),
                (2, 1),
            )

            rows = {row[0]: row[1] for row in duckdb.connect().execute(
                "SELECT person_id, embedding FROM read_parquet(?)", [str(cache)]
            ).fetchall()}
            self.assertEqual(set(rows), {"old", "shared", "new"})
            self.assertAlmostEqual(rows["shared"][0], 0.7, places=6)
            column_type = duckdb.connect().execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(cache)]
            ).fetchall()[1][1]
            self.assertEqual(column_type, "FLOAT[]")

    def test_parquet_artifact_ids_and_embedding_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "roles.jsonl"
            cache = root / "roles.parquet"
            write_jsonl(source, [
                {"title_hash": "has-vector", "dense_embedding": [0.125, 0.25]},
                {"title_hash": "empty-vector", "dense_embedding": []},
            ])
            merge_cache_file(source, cache, ("title_hash",), vector_field="dense_embedding")

            self.assertEqual(artifact_id_set(cache, "title_hash"), {"has-vector", "empty-vector"})
            self.assertEqual(
                artifact_id_set(cache, "title_hash", require_vector=True),
                {"has-vector"},
            )
            embeddings = load_input_embeddings(str(cache), "title_hash", "dense_embedding")
            self.assertEqual(embeddings["has-vector"].typecode, "f")
            self.assertEqual(list(embeddings["has-vector"]), [0.125, 0.25])
            self.assertEqual(
                [row["title_hash"] for row in iter_artifact_rows(cache, ["title_hash"])],
                ["has-vector", "empty-vector"],
            )

    def test_first_parquet_merge_does_not_import_jsonl_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "company_embeddings_v3.parquet"
            write_jsonl(
                root / "company_embeddings_v3.jsonl",
                [{"company_urn": "legacy", "company_name": "Legacy", "embedding": [0.1]}],
            )
            update = root / "update.jsonl"
            write_jsonl(
                update,
                [{"company_urn": "new", "company_name": "New", "embedding": [0.2]}],
            )

            self.assertEqual(
                merge_cache_file(
                    update,
                    cache,
                    ("company_urn", "company_name"),
                    vector_field="embedding",
                ),
                (1, 0),
            )
            self.assertEqual(artifact_id_set(cache, "company_urn"), {"new"})

    def test_jsonl_embedding_loader_remains_float64(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "summary.jsonl"
            write_jsonl(source, [{"person_id": "one", "embedding": [0.1, 0.2]}])
            embeddings = load_input_embeddings(str(source), "person_id", "embedding")
            self.assertEqual(embeddings["one"].typecode, "d")

    def test_final_record_parquet_uses_float_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records"
            records.mkdir()
            source = records / "summaries.records.jsonl"
            write_jsonl(source, [
                {"id": "one", "person_id": "one", "vector": [0.1, 0.2], "summary": "One"},
                {"id": "two", "person_id": "two", "vector": [0.3, 0.4], "summary": "Two"},
            ])

            counts = materialize_parquet_records(records)
            parquet = records / "summaries.records.parquet"
            self.assertEqual(counts, {"summaries.records.parquet": 2})
            columns = duckdb.connect().execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet)]
            ).fetchall()
            self.assertEqual({row[0]: row[1] for row in columns}["vector"], "FLOAT[]")


if __name__ == "__main__":
    unittest.main()
