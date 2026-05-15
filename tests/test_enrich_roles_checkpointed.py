import argparse
import json
import tempfile
import unittest
from pathlib import Path

from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed as stage


class EnrichRolesCheckpointedTests(unittest.TestCase):
    def _write_flattened(self, path: Path, count: int = 5) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for idx in range(count):
                row = {
                    "id": f"person-{idx}",
                    "headline": "Canary profile",
                    "summary": "Local processing canary",
                    "work_experiences": [
                        {
                            "id": f"pos-{idx}",
                            "title": ["Founder", "Staff Software Engineer", "Product Manager", "VP Sales", "Data Scientist"][idx],
                            "company_name": f"Company {idx}",
                            "description": f"Role description {idx}",
                        }
                    ],
                }
                handle.write(json.dumps(row) + "\n")

    def test_checkpoint_resume_without_recomputing_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flattened = root / "flattened_people.jsonl"
            out = root / "roles"
            self._write_flattened(flattened)

            partial = stage.run(
                argparse.Namespace(
                    flattened=str(flattened),
                    output_dir=str(out),
                    checkpoint_every=2,
                    provider="local",
                    force=True,
                    stop_after_chunks=1,
                )
            )
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(partial["input_rows_processed"], 2)
            state_after_partial = json.loads((out / "checkpoint.json").read_text())
            self.assertEqual(state_after_partial["chunks_written"], 1)

            final = stage.run(
                argparse.Namespace(
                    flattened=str(flattened),
                    output_dir=str(out),
                    checkpoint_every=2,
                    provider="local",
                    force=False,
                    stop_after_chunks=None,
                )
            )
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["counts"]["unique_roles"], 5)
            self.assertEqual(final["counts"]["chunks_written"], 3)
            checkpoint = json.loads((out / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["input_rows_processed"], 5)
            self.assertEqual(checkpoint["status"], "completed")

            rows = [json.loads(line) for line in (out / "roles_with_dense_text_remapped.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 5)
            self.assertTrue(any(row["doc2query"] for row in rows))
            self.assertTrue(any(row["role_ids"] for row in rows))

    def test_paid_provider_is_blocked_without_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flattened = root / "flattened_people.jsonl"
            self._write_flattened(flattened, count=1)
            with self.assertRaises(SystemExit):
                stage.run(
                    argparse.Namespace(
                        flattened=str(flattened),
                        output_dir=str(root / "roles"),
                        checkpoint_every=2,
                        provider="tlm",
                        force=True,
                        stop_after_chunks=None,
                    )
                )


if __name__ == "__main__":
    unittest.main()
