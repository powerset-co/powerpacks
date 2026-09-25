"""A fresh install (no legacy artifacts, no SQLite) reaches a working store.

Changelog:
- 2026-09-25: created with the fix that routes a missing store to migrate-sqlite.
- 2026-09-25: check routes an empty store to ensure-parents and a missing owner
  to the owner command; owner-required errors name the real command.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError, open_existing_db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.migration import migrate_sqlite
from packs.ingestion.primitives.deep_context.shared.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.shared.readiness_models import ReadinessReport
from packs.ingestion.primitives.deep_context.synthesis.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.synthesis.selection import build_system_prompt
from packs.shared.csv_io import CsvIO

MIGRATE_COMMAND = "bin/deep-context migrate-sqlite"
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

    def readiness(self) -> ReadinessReport:
        check = "packs.ingestion.primitives.deep_context.shared.check_readiness"
        with (
            mock.patch(f"{check}.load_env"),
            mock.patch(
                f"{check}.context_sources.probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}),
        ):
            return CheckReadiness(
                db_path=self.db_path,
                people_csv=self.people_csv,
                msgvault_db=self.root / "missing-msgvault.db",
                chat_db=self.root / "missing-chat.db",
                wacli_db=self.wacli,
            ).run()

    def migrate(self) -> tuple[int, dict[str, object]]:
        out = StringIO()
        with redirect_stdout(out):
            code = migrate_sqlite.main([])
        return code, json.loads(out.getvalue())

    def test_check_routes_a_fresh_install_to_migrate_sqlite(self) -> None:
        result = self.readiness()

        self.assertEqual(result.checks.canonical_sqlite.status, "missing")
        self.assertEqual(result.next_command, MIGRATE_COMMAND)
        self.assertFalse(result.ready)
        self.assertFalse(self.db_path.exists())

    def test_migrate_creates_an_empty_store_that_ensure_parents_fills(self) -> None:
        code, payload = self.migrate()

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(self.db_path.is_file())
        self.assertEqual({key: value for key, value in payload["counts"].items() if value}, {})
        # A repeat on the still-empty store completes instead of tripping on meta.
        self.assertEqual(self.migrate()[0], 0)

        result = EnsureParents(db=open_existing_db(self.db_path), people_csv=self.people_csv).run()

        self.assertEqual(result.people_projected, 2)
        after = self.readiness()
        self.assertEqual(after.checks.canonical_sqlite.status, "ok")
        self.assertTrue(after.ready)

    def test_check_routes_an_empty_store_to_ensure_parents(self) -> None:
        self.migrate()

        result = self.readiness()

        self.assertEqual(result.checks.canonical_sqlite.status, "empty")
        self.assertEqual(result.next_command, ENSURE_PARENTS_COMMAND)

    def test_check_routes_projected_people_without_an_owner_to_the_owner_command(self) -> None:
        self.migrate()
        EnsureParents(db=open_existing_db(self.db_path), people_csv=self.people_csv).run()

        result = self.readiness()

        self.assertEqual(result.checks.owner_json.status, "absent")
        self.assertEqual(result.next_command, OWNER_COMMAND)
        self.assertTrue(any(OWNER_COMMAND in line for line in result.advice))

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
