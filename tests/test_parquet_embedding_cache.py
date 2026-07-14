from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import duckdb

from packs.indexing.lib.artifact_io import artifact_id_set, iter_artifact_rows, write_parquet_rows
from packs.indexing.modal.sandbox_common import merge_cache_file
from packs.indexing.primitives.embed_records_checkpointed import embed_records_checkpointed as embedder
from packs.indexing.primitives.embed_records_checkpointed.embed_records_checkpointed import load_input_embeddings


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def embedding_args(root: Path, cache: Path, **overrides) -> Namespace:
    values = {
        "input": str(root / "input.jsonl"),
        "output": str(root / "result.parquet"),
        "output_dir": str(root / "checkpoints"),
        "id_field": "person_id",
        "text_fields": "text",
        "copy_fields": "person_id,text",
        "checkpoint_every": 2,
        "provider": "openai",
        "api_key": None,
        "base_url": None,
        "model": None,
        "dimension": 2,
        "api_batch_size": 128,
        "concurrency": 1,
        "openai_usage_tier": None,
        "cost_per_1k_tokens": 0.00002,
        "input_embeddings": str(cache),
        "input_id_field": "person_id",
        "input_embedding_field": "embedding",
        "allow_paid": False,
        "dry_run": False,
        "force": False,
        "stop_after_chunks": None,
    }
    values.update(overrides)
    return Namespace(**values)


class ParquetEmbeddingCacheTests(unittest.TestCase):
    def test_parquet_row_writer_streams_generator_and_adds_late_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "streamed.parquet"
            consumed = 0

            def records():
                nonlocal consumed
                for idx in range(2050):
                    consumed += 1
                    row = {"id": f"row-{idx:04d}", "embedding": [idx / 10, idx / 20]}
                    if idx >= 2048:
                        row["late_rank"] = idx
                    yield row

            self.assertEqual(
                write_parquet_rows(path, records(), float_array_fields=("embedding",)),
                2050,
            )
            self.assertEqual(consumed, 2050)
            with duckdb.connect() as con:
                schema = {
                    row[0]: row[1]
                    for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
                }
                first, last = con.execute(
                    "SELECT late_rank FROM read_parquet(?) WHERE id IN ('row-0000', 'row-2049') ORDER BY id",
                    [str(path)],
                ).fetchall()
            self.assertEqual(schema["embedding"], "FLOAT[]")
            self.assertEqual(schema["late_rank"], "BIGINT")
            self.assertIsNone(first[0])
            self.assertEqual(last[0], 2049)

    def test_parquet_row_writer_promotes_null_only_first_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "promoted.parquet"

            def records():
                for idx in range(2049):
                    yield {
                        "id": f"row-{idx:04d}",
                        "rank": None if idx < 2048 else 7,
                        "tags": [] if idx < 2048 else [1, 2],
                    }

            write_parquet_rows(path, records())
            with duckdb.connect() as con:
                schema = {
                    row[0]: row[1]
                    for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
                }
            self.assertEqual(schema["rank"], "BIGINT")
            self.assertEqual(schema["tags"], "BIGINT[]")

    def test_parquet_row_writer_merges_mixed_types_within_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.parquet"
            write_parquet_rows(path, [
                {"id": "one", "number": 1, "label": 1},
                {"id": "two", "number": 1.5, "label": "unknown"},
            ])
            with duckdb.connect() as con:
                schema = {
                    row[0]: row[1]
                    for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
                }
                rows = con.execute(
                    "SELECT id, number, label FROM read_parquet(?) ORDER BY id", [str(path)]
                ).fetchall()
            self.assertEqual(schema["number"], "DOUBLE")
            self.assertEqual(schema["label"], "VARCHAR")
            self.assertEqual(rows, [("one", 1.0, "1"), ("two", 1.5, "unknown")])

    def test_embedding_output_and_resume_use_only_native_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(root / "input.jsonl", [
                {"person_id": "c", "text": "Three"},
                {"person_id": "a", "text": "One"},
                {"person_id": "b", "text": "Two"},
            ])
            cache = root / "cache.parquet"
            write_parquet_rows(cache, [
                {"person_id": "a", "embedding": [0.1, 0.2]},
                {"person_id": "b", "embedding": [0.3, 0.4]},
                {"person_id": "c", "embedding": [0.5, 0.6]},
            ], float_array_fields=("embedding",))

            partial = embedder.run(embedding_args(root, cache, stop_after_chunks=1))
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(partial["input_rows_processed"], 2)

            manifest = embedder.run(embedding_args(root, cache))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["counts"], {
                "input_rows_processed": 3,
                "embeddings": 3,
                "chunks_written": 2,
                "artifact_hits": 3,
                "artifact_misses": 0,
                "paid_calls": 0,
            })
            chunks = sorted((root / "checkpoints/chunks").iterdir())
            self.assertEqual([path.suffix for path in chunks], [".parquet", ".parquet"])
            self.assertFalse(list((root / "checkpoints").rglob("*.jsonl")))

            with duckdb.connect() as con:
                rows = con.execute(
                    "SELECT id, embedding, person_id, text FROM read_parquet(?) ORDER BY id",
                    [str(root / "result.parquet")],
                ).fetchall()
                schema = {
                    row[0]: row[1]
                    for row in con.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)", [str(root / "result.parquet")]
                    ).fetchall()
                }
            self.assertEqual([row[0] for row in rows], ["a", "b", "c"])
            self.assertEqual([row[2] for row in rows], ["a", "b", "c"])
            self.assertEqual(schema["embedding"], "FLOAT[]")

    def test_legacy_jsonl_checkpoint_is_reset_before_parquet_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(root / "input.jsonl", [{"person_id": "one", "text": "One"}])
            cache = root / "cache.parquet"
            write_parquet_rows(
                cache,
                [{"person_id": "one", "embedding": [0.1, 0.2]}],
                float_array_fields=("embedding",),
            )
            checkpoints = root / "checkpoints"
            (checkpoints / "chunks").mkdir(parents=True)
            (checkpoints / "checkpoint.json").write_text(json.dumps({
                "status": "running",
                "input_rows_processed": 1,
                "chunks_written": 1,
            }), encoding="utf-8")
            write_jsonl(
                checkpoints / "chunks/embeddings.000001.jsonl",
                [{"id": "one", "embedding": [0.1, 0.2]}],
            )

            result = embedder.run(embedding_args(root, cache))

            self.assertEqual(result["counts"]["embeddings"], 1)
            state = json.loads((checkpoints / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(state["artifact_format"], embedder.CHECKPOINT_ARTIFACT_FORMAT)
            self.assertFalse(any(checkpoints.rglob("*.jsonl")))

    def test_embedding_cache_miss_still_uses_paid_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(root / "input.jsonl", [
                {"person_id": "hit", "text": "Cached"},
                {"person_id": "miss", "text": "New"},
            ])
            cache = root / "cache.parquet"
            write_parquet_rows(
                cache,
                [{"person_id": "hit", "embedding": [0.1, 0.2]}],
                float_array_fields=("embedding",),
            )

            with patch.object(embedder, "openai_embedding_batches", return_value=[[[0.9, 1.0]]]) as mocked:
                manifest = embedder.run(embedding_args(
                    root,
                    cache,
                    allow_paid=True,
                    api_key="test-key",
                ))

            self.assertEqual(manifest["counts"]["artifact_hits"], 1)
            self.assertEqual(manifest["counts"]["artifact_misses"], 1)
            self.assertEqual(manifest["counts"]["paid_calls"], 1)
            mocked.assert_called_once()
            rows = dict(duckdb.connect().execute(
                "SELECT id, embedding FROM read_parquet(?)", [str(root / "result.parquet")]
            ).fetchall())
            self.assertAlmostEqual(rows["hit"][0], 0.1, places=6)
            self.assertAlmostEqual(rows["miss"][0], 0.9, places=6)
            self.assertEqual(rows["miss"][1], 1.0)

    def test_parquet_cache_merge_preserves_old_rows_and_new_rows_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "summary_embeddings.parquet"
            initial = root / "initial.parquet"
            update = root / "update.parquet"
            write_parquet_rows(initial, [
                {"person_id": "old", "embedding": [0.1, 0.2]},
                {"person_id": "shared", "embedding": [0.3, 0.4]},
            ], float_array_fields=("embedding",))
            self.assertEqual(
                merge_cache_file(initial, cache, ("person_id",)),
                (2, 0),
            )

            write_parquet_rows(update, [
                {"person_id": "shared", "embedding": [0.7, 0.8]},
                {"person_id": "new", "embedding": [0.9, 1.0]},
            ], float_array_fields=("embedding",))
            self.assertEqual(
                merge_cache_file(update, cache, ("person_id",)),
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
            source = root / "roles-source.parquet"
            cache = root / "roles.parquet"
            write_parquet_rows(source, [
                {"title_hash": "has-vector", "dense_embedding": [0.125, 0.25]},
                {"title_hash": "empty-vector", "dense_embedding": []},
            ], float_array_fields=("dense_embedding",))
            merge_cache_file(source, cache, ("title_hash",))

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
            update = root / "update.parquet"
            write_parquet_rows(
                update,
                [{"company_urn": "new", "company_name": "New", "embedding": [0.2]}],
                float_array_fields=("embedding",),
            )

            self.assertEqual(
                merge_cache_file(
                    update,
                    cache,
                    ("company_urn", "company_name"),
                ),
                (1, 0),
            )
            self.assertEqual(artifact_id_set(cache, "company_urn"), {"new"})

    def test_jsonl_embedding_loader_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "summary.jsonl"
            write_jsonl(source, [{"person_id": "one", "embedding": [0.1, 0.2]}])
            with self.assertRaisesRegex(SystemExit, "input embeddings must use a .parquet path"):
                load_input_embeddings(str(source), "person_id", "embedding")

    def test_embedding_loader_rejects_wrong_dimension_and_non_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "summary.parquet"
            write_parquet_rows(
                source,
                [{"person_id": "one", "embedding": [0.1]}],
                float_array_fields=("embedding",),
            )
            with self.assertRaisesRegex(SystemExit, "dimension mismatch"):
                load_input_embeddings(
                    str(source), "person_id", "embedding", expected_dimension=2
                )

            write_parquet_rows(
                source,
                [{"person_id": "one", "embedding": [0.1, float("nan")]}],
                float_array_fields=("embedding",),
            )
            with self.assertRaisesRegex(SystemExit, "non-finite"):
                load_input_embeddings(
                    str(source), "person_id", "embedding", expected_dimension=2
                )


if __name__ == "__main__":
    unittest.main()
