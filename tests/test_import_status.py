"""Tests for the read-only per-source import status primitive.

Created: 2026-07-14
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.discover_contacts_pipeline.common import write_csv_rows
from packs.ingestion.primitives.import_contacts_pipeline import status as import_status


class ImportStatusTests(unittest.TestCase):
    @contextmanager
    def sandbox(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            state = root / ".powerpacks" / "network-import"
            stack.enter_context(mock.patch.object(import_status, "DEFAULT_BASE_DIR", state))
            stack.enter_context(
                mock.patch.object(import_status, "DEFAULT_IMPORT_DIR", state / "import")
            )
            stack.enter_context(
                mock.patch.object(
                    import_status,
                    "CANONICAL_MERGED_PEOPLE_CSV",
                    state / "merged" / "people.csv",
                )
            )
            previous = Path.cwd()
            os.chdir(root)
            try:
                yield state
            finally:
                os.chdir(previous)

    @staticmethod
    def seed_import(state: Path, source: str, people_rows: int, candidates_rows: int = 0) -> None:
        import_dir = state / "import" / source
        people_csv = import_dir / "people.csv"
        write_csv_rows(
            people_csv,
            ["id", "full_name"],
            [{"id": f"{source}-{i}", "full_name": f"P {i}"} for i in range(people_rows)],
        )
        outputs = {"people_csv": str(people_csv)}
        if candidates_rows:
            candidates_csv = import_dir / "candidates.csv"
            write_csv_rows(
                candidates_csv,
                ["candidate_key", "full_name"],
                [{"candidate_key": f"phone:+1415555{i:04d}", "full_name": f"C {i}"} for i in range(candidates_rows)],
            )
            outputs["candidates_csv"] = str(candidates_csv)
        manifest = {
            "source": source,
            "status": "completed",
            "input": {},
            "outputs": outputs,
            "fingerprints": {"input_artifacts": {}, "output_artifacts": {}},
        }
        (import_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_reports_present_and_missing_sources(self) -> None:
        with self.sandbox() as state:
            self.seed_import(state, "gmail", people_rows=3, candidates_rows=2)
            merged = state / "merged" / "people.csv"
            write_csv_rows(merged, ["id"], [{"id": "a"}, {"id": "b"}])

            payload = import_status.status_payload(["gmail", "linkedin", "messages"])

            gmail = payload["sources"]["gmail"]["import"]
            self.assertTrue(gmail["imported"])
            self.assertEqual(gmail["people"], 3)
            self.assertEqual(gmail["candidates"], 2)
            linkedin = payload["sources"]["linkedin"]["import"]
            self.assertFalse(linkedin["imported"])
            self.assertFalse(linkedin["present"])
            self.assertEqual(payload["merged"]["people"], 2)
            self.assertTrue(payload["merged"]["exists"])

    def test_incomplete_manifest_is_not_imported(self) -> None:
        with self.sandbox() as state:
            import_dir = state / "import" / "messages"
            import_dir.mkdir(parents=True)
            (import_dir / "manifest.json").write_text(
                json.dumps({"source": "messages", "status": "blocked_approval"}),
                encoding="utf-8",
            )
            payload = import_status.status_payload(["messages"])
            messages = payload["sources"]["messages"]["import"]
            self.assertTrue(messages["present"])
            self.assertFalse(messages["imported"])
            self.assertEqual(messages["status"], "blocked_approval")

    def test_cli_always_exits_zero(self) -> None:
        with self.sandbox():
            with mock.patch.object(sys, "argv", ["status.py", "status"]), \
                    redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(import_status.main(), 0)
            payload = json.loads(captured.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(
                sorted(payload["sources"]), ["gmail", "linkedin", "messages"]
            )


if __name__ == "__main__":
    unittest.main()
