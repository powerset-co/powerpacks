import tempfile
import unittest
from pathlib import Path

from packs.indexing.lib.io import read_jsonl, write_jsonl
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import (
    reset_processing_checkpoints,
    upsert_people_jsonl,
)


class ProcessingLimitModeTest(unittest.TestCase):
    def test_flatten_output_upserts_without_overwriting_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "unified/flattened_people.jsonl"
            write_jsonl(output, [
                {"id": "existing-1", "linkedin_url": "https://linkedin.com/in/old", "name": "Old"},
                {"id": "keep-1", "linkedin_url": "https://linkedin.com/in/keep", "name": "Keep"},
            ])

            stats = upsert_people_jsonl(output, [
                {"id": "existing-1", "linkedin_url": "https://linkedin.com/in/old", "name": "Updated"},
                {"id": "new-1", "linkedin_url": "https://linkedin.com/in/new", "name": "New"},
            ])
            rows = read_jsonl(output)

        self.assertEqual(stats["existing_rows"], 2)
        self.assertEqual(stats["inserted_rows"], 1)
        self.assertEqual(stats["updated_rows"], 1)
        self.assertEqual([row["id"] for row in rows], ["existing-1", "keep-1", "new-1"])
        self.assertEqual(rows[0]["name"], "Updated")

    def test_checkpoint_reset_preserves_stage_outputs_as_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep_files = [
                root / "roles/roles_with_dense_text_remapped.jsonl",
                root / "roles/roles_with_embeddings.jsonl",
                root / "company/companies_corpus_v3.jsonl",
                root / "company/company_embeddings_v3.jsonl",
                root / "unified/summary_embeddings.jsonl",
            ]
            for path in keep_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            remove_files = [
                root / "roles/checkpoint.json",
                root / "roles/manifest.json",
                root / "roles/chunks/roles.000001.jsonl",
                root / "roles/embedding_checkpoints/checkpoint.json",
                root / "company/enrichment_checkpoints/checkpoint.json",
                root / "summaries/embedding_checkpoints/checkpoint.json",
                root / "vectors/checkpoint.json",
            ]
            for path in remove_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")

            reset_processing_checkpoints(root)

            for path in keep_files:
                self.assertTrue(path.exists(), path)
            for path in remove_files:
                self.assertFalse(path.exists(), path)


if __name__ == "__main__":
    unittest.main()
