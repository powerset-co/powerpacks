"""A fresh install (no legacy artifacts, no SQLite) reaches a working store.

Changelog:
- 2026-09-25: created with the fix that routes a missing store to migrate-sqlite.
- 2026-09-25: a missing store routes to ensure-parents, which creates it; migrate-sqlite is unrouted.
- 2026-09-25: check routes an empty store to ensure-parents and a missing owner
  to the owner command; owner-required errors name the real command.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.db.models import OwnerContextRow
from packs.ingestion.primitives.deep_context.db.store import Db, SchemaVersionError, StoreError, open_existing_db
from packs.ingestion.primitives.deep_context.ensure_parents import ensure_parents
from packs.ingestion.primitives.deep_context.shared.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.shared.readiness_models import ReadinessReport
from packs.ingestion.primitives.deep_context.synthesis.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.synthesis.selection import build_system_prompt
from packs.shared.csv_io import CsvIO

ENSURE_PARENTS_COMMAND = "bin/deep-context ensure-parents"
OWNER_COMMAND = "bin/deep-context owner --linkedin-url <url> --email <email>"


class FreshInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)

        self.deep_context = self.root / ".powerpacks/deep-context"
        self.db_path = self.deep_context / "deep-context.sqlite"
        self.people_csv = self.root / ".powerpacks/network-import/merged/people.csv"
        CsvIO.write_dict_rows(
            self.people_csv,
            ["id", "full_name", "primary_email", "source_channels"],
            [
                {
                    "id": "person-jordan",
                    "full_name": "Jordan Bravo",
                    "primary_email": "jordan@example.com",
                    "source_channels": "gmail_msgvault",
                },
                {
                    "id": "person-casey",
                    "full_name": "Casey Alpha",
                    "primary_email": "casey@example.com",
                    "source_channels": "gmail_msgvault",
                },
            ],
        )
        self.wacli = self.root / "wacli.db"
        self.wacli.touch()

    def readiness(self, env: dict[str, str] | None = None) -> ReadinessReport:
        check = "packs.ingestion.primitives.deep_context.shared.check_readiness"
        keys = {"OPENAI_API_KEY": "synthetic-key", "TYPESAFE_API_KEY": "synthetic-key"}
        with (
            mock.patch(f"{check}.load_env"),
            mock.patch(
                f"{check}.context_sources.probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.dict(os.environ, keys if env is None else env, clear=False),
        ):
            return CheckReadiness(
                db_path=self.db_path,
                people_csv=self.people_csv,
                msgvault_db=self.root / "missing-msgvault.db",
                chat_db=self.root / "missing-chat.db",
                wacli_db=self.wacli,
            ).run()

    def project_owner(self) -> None:
        owner = self.deep_context / "owner.json"
        owner.write_text('{"name": "Jordan Bravo"}', encoding="utf-8")
        open_existing_db(self.db_path).project_rows(
            (OwnerContextRow("owner", owner.read_text(encoding="utf-8"), str(owner), "fp-owner"),)
        )

    def project_owner(self) -> None:
        owner = self.deep_context / "owner.json"
        owner.write_text('{"name": "Jordan Bravo"}', encoding="utf-8")
        open_existing_db(self.db_path).project_rows(
            (OwnerContextRow("owner", owner.read_text(encoding="utf-8"), str(owner), "fp-owner"),)
        )

    def ensure_parents(self) -> tuple[int, dict[str, object]]:
        out = StringIO()
        with redirect_stdout(out):
            code = ensure_parents.main(["--db", str(self.db_path), "--people-csv", str(self.people_csv)])
        return code, json.loads(out.getvalue())

    def test_check_routes_a_fresh_install_to_ensure_parents(self) -> None:
        result = self.readiness()

        self.assertEqual(result.checks.canonical_sqlite.status, "missing")
        self.assertEqual(result.next_command, ENSURE_PARENTS_COMMAND)
        self.assertFalse(result.ready)
        self.assertFalse(self.db_path.exists())

    def test_ensure_parents_creates_the_store_and_fills_it(self) -> None:
        code, payload = self.ensure_parents()

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(self.db_path.is_file())
        self.assertEqual(payload["people_projected"], 2)
        # A repeat on the filled store is idempotent.
        self.assertEqual(self.ensure_parents()[1]["people_projected"], 2)

        after = self.readiness()
        self.assertEqual(after.checks.canonical_sqlite.status, "ok")
        self.assertFalse(after.ready)

        self.project_owner()
        with_owner = self.readiness()
        self.assertIsNone(with_owner.next_command)
        self.assertTrue(with_owner.ready)

    def test_check_requires_the_typesafe_key_for_worth_labels_and_the_merge_judge(self) -> None:
        self.ensure_parents()
        self.project_owner()

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            result = self.readiness({"OPENAI_API_KEY": "synthetic-key"})

        self.assertEqual(result.checks.typesafe_api_key.status, "missing")
        self.assertFalse(result.ready)
        self.assertTrue(any("TYPESAFE_API_KEY" in line for line in result.advice))

    def test_check_routes_an_empty_store_to_ensure_parents(self) -> None:
        Db(self.db_path)

        result = self.readiness()

        self.assertEqual(result.checks.canonical_sqlite.status, "empty")
        self.assertEqual(result.next_command, ENSURE_PARENTS_COMMAND)

    def test_check_routes_projected_people_without_an_owner_to_the_owner_command(self) -> None:
        self.ensure_parents()

        result = self.readiness()

        self.assertEqual(result.checks.owner_json.status, "absent")
        self.assertEqual(result.next_command, OWNER_COMMAND)
        self.assertFalse(result.ready)
        self.assertTrue(any(OWNER_COMMAND in line for line in result.advice))

    def test_check_projects_existing_owner_file_with_bare_owner_advice(self) -> None:
        self.ensure_parents()
        (self.deep_context / "owner.json").write_text('{"name": "Jordan Bravo"}', encoding="utf-8")

        result = self.readiness()

        self.assertEqual(result.checks.owner_json.status, "absent")
        self.assertEqual(result.next_command, "bin/deep-context owner")
        self.assertIn("No owner profile — synthesis requires one: run bin/deep-context owner.", result.advice)
        self.assertFalse(any("--linkedin-url" in line for line in result.advice))

    def _make_august_store(self) -> bytes:
        Db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO meta VALUES ('legacy_imported_at', '2026-08-19T00:00:00Z')")
            for table in ("person_labels", "person_tags", "share"):
                conn.execute(f"DROP TABLE {table}")
        return self.db_path.read_bytes()

    def test_check_sets_aside_august_store_without_changing_its_bytes(self) -> None:
        before = self._make_august_store()
        err = StringIO()
        with redirect_stderr(err):
            result = self.readiness()

        self.assertEqual(result.checks.canonical_sqlite.status, "missing")
        self.assertEqual(result.next_command, ENSURE_PARENTS_COMMAND)
        self.assertFalse(self.db_path.exists())
        backups = list(self.deep_context.glob("deep-context.sqlite.bkup-schema-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn(str(backups[0]), err.getvalue())

    def test_ensure_parents_sets_aside_august_store_before_opening_db(self) -> None:
        before = self._make_august_store()
        with redirect_stderr(StringIO()):
            code, payload = self.ensure_parents()

        self.assertEqual(code, 0)
        self.assertEqual(payload["people_projected"], 2)
        backups = list(self.deep_context.glob("deep-context.sqlite.bkup-schema-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(self.readiness().checks.canonical_sqlite.status, "ok")

    def test_check_leaves_current_store_untouched(self) -> None:
        self.ensure_parents()
        before = self.db_path.read_bytes()
        err = StringIO()
        with redirect_stderr(err):
            self.readiness()

        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(list(self.deep_context.glob("*.bkup-schema-*")), [])
        self.assertEqual(err.getvalue(), "")

    def test_unknown_layouts_still_raise_without_backup(self) -> None:
        self._make_august_store()
        # Even the three missing tables plus legacy metadata do not authorize
        # setting aside a store with an additional, unknown layout failure.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE guidance")
        before = self.db_path.read_bytes()
        for run in (self.readiness, self.ensure_parents):
            with self.subTest(stage=run.__name__):
                with self.assertRaisesRegex(SchemaVersionError, "layout does not match schema version 1"):
                    run()
                self.assertEqual(self.db_path.read_bytes(), before)
                self.assertEqual(list(self.deep_context.glob("*.bkup-schema-*")), [])

    def test_missing_legacy_metadata_still_raises_without_backup(self) -> None:
        self._make_august_store()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM meta WHERE key='legacy_imported_at'")
        with self.assertRaises(SchemaVersionError):
            self.readiness()
        self.assertTrue(self.db_path.exists())
        self.assertEqual(list(self.deep_context.glob("*.bkup-schema-*")), [])

    def test_owner_required_errors_name_the_owner_command(self) -> None:
        db = Db(self.db_path)

        with self.assertRaisesRegex(StoreError, "run bin/deep-context owner"):
            build_system_prompt(db)
        with self.assertRaisesRegex(StoreError, "run bin/deep-context owner"):
            ComposeDossier(db=db, dossier_dir=self.root / "dossiers").execute()

    def test_owner_json_is_reported_present_before_the_store_exists(self) -> None:
        self.deep_context.mkdir(parents=True)
        (self.deep_context / "owner.json").write_text('{"name": "Jordan Bravo"}', encoding="utf-8")

        result = self.readiness()

        self.assertEqual(result.checks.owner_json.status, "present")
        self.assertFalse(any("owner.json" in line for line in result.advice))


if __name__ == "__main__":
    unittest.main()
