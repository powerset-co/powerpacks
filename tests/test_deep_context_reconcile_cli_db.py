"""Canonical CLI handoff into the Deep Context SQLite projection."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import reconcile_deep_research as reconcile
from packs.ingestion.primitives.deep_context.db.store import Db


class ReconcileCliDbTest(unittest.TestCase):
    def test_default_is_the_fixed_canonical_database(self) -> None:
        args = reconcile.build_parser().parse_args([])
        self.assertEqual(Path(args.db), Path(".powerpacks/deep-context/deep-context.sqlite"))

    def test_missing_database_fails_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite"
            with mock.patch.object(reconcile, "ReconcileDeepResearch") as node:
                with self.assertRaisesRegex(SystemExit, "database is missing"):
                    reconcile.main(["--db", str(missing)])
            self.assertFalse(missing.exists())
            node.assert_not_called()

    def test_unsupported_database_fails_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unsupported = Path(directory) / "unsupported.sqlite"
            unsupported.write_bytes(b"not a sqlite database")
            before = unsupported.read_bytes()
            with mock.patch.object(reconcile, "ReconcileDeepResearch") as node:
                with self.assertRaisesRegex(SystemExit, "database is unsupported"):
                    reconcile.main(["--db", str(unsupported)])
            self.assertEqual(unsupported.read_bytes(), before)
            node.assert_not_called()

    def test_existing_database_is_passed_to_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "deep-context.sqlite"
            Db(db_path)
            with mock.patch.object(reconcile, "ReconcileDeepResearch") as node_type, \
                 mock.patch.object(reconcile, "emit") as emit:
                node_type.return_value.result = {"status": "noop"}
                self.assertEqual(reconcile.main(["--db", str(db_path)]), 0)

            passed = node_type.call_args.kwargs["db"]
            self.assertIsInstance(passed, Db)
            self.assertEqual(passed.db_path, db_path)
            node_type.return_value.run.assert_called_once_with()
            emit.assert_called_once_with({"status": "noop"})


if __name__ == "__main__":
    unittest.main()
