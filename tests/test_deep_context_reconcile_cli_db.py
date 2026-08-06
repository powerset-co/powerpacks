"""Canonical CLI handoff into the Deep Context SQLite projection."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import (
    apply_retargets,
    assemble_synthetic_profile,
    build_parents,
    cluster_merge_candidates,
    compose_dossier,
    heal_review,
    persist_review_identities,
    prefetch_profiles,
    reconcile_deep_research as reconcile,
    reconcile_linkedin,
    restart_review,
    synthesize_person_context,
    validate_dossiers,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.review_web import cli as review_web_cli


class ReconcileCliDbTest(unittest.TestCase):
    def test_default_is_the_fixed_canonical_database(self) -> None:
        args = reconcile.build_parser().parse_args([])
        self.assertEqual(Path(args.db), Path(".powerpacks/deep-context/deep-context.sqlite"))

    def test_open_existing_db_fails_without_creating_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite"
            with self.assertRaisesRegex(SystemExit, "database is missing"):
                open_existing_db(missing)
            self.assertFalse(missing.exists())

    def test_guarded_cli_mains_fail_without_creating_missing_database(self) -> None:
        cli_modules = (
            apply_retargets,
            assemble_synthetic_profile,
            build_parents,
            cluster_merge_candidates,
            compose_dossier,
            heal_review,
            persist_review_identities,
            prefetch_profiles,
            reconcile,
            reconcile_linkedin,
            restart_review,
            synthesize_person_context,
            validate_dossiers,
            review_web_cli,
        )
        with tempfile.TemporaryDirectory() as directory:
            for module in cli_modules:
                with self.subTest(module=module.__name__):
                    missing = Path(directory) / f"{module.__name__.rsplit('.', 1)[-1]}.sqlite"
                    env_patch = (
                        mock.patch.object(module, "load_env")
                        if module is prefetch_profiles
                        else nullcontext()
                    )
                    db_patch = (
                        mock.patch.object(review_web_cli, "CANONICAL_DB", missing)
                        if module is review_web_cli
                        else nullcontext()
                    )
                    argv = ["status"] if module is review_web_cli else ["--db", str(missing)]
                    with (
                        env_patch,
                        db_patch,
                        self.assertRaisesRegex(SystemExit, "database is missing"),
                    ):
                        module.main(argv)
                    self.assertFalse(missing.exists())

    def test_unsupported_database_fails_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unsupported = Path(directory) / "unsupported.sqlite"
            unsupported.write_bytes(b"not a sqlite database")
            before = unsupported.read_bytes()
            with self.assertRaisesRegex(SystemExit, "database is unsupported"):
                open_existing_db(unsupported)
            self.assertEqual(unsupported.read_bytes(), before)

    def test_existing_database_is_passed_to_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "deep-context.sqlite"
            Db(db_path)
            with (
                mock.patch.object(reconcile, "ReconcileDeepResearch") as node_type,
                mock.patch.object(reconcile, "emit") as emit,
            ):
                node_type.return_value.run_with_result.return_value = (
                    {"status": "noop"},
                    mock.sentinel.receipt,
                )
                self.assertEqual(reconcile.main(["--db", str(db_path)]), 0)

            passed = node_type.call_args.kwargs["db"]
            self.assertIsInstance(passed, Db)
            self.assertEqual(passed.db_path, db_path)
            node_type.return_value.run_with_result.assert_called_once_with()
            emit.assert_called_once_with({"status": "noop"})


if __name__ == "__main__":
    unittest.main()
