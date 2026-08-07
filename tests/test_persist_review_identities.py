"""Approved Deep Context identity decisions persist through directory.csv."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.realize.persist_review_identities import PersistReviewIdentities
from packs.ingestion.primitives.deep_context.db.models import (
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.shared.csv_io import CsvIO


class PersistReviewIdentitiesTests(unittest.TestCase):
    def test_approved_real_identities_are_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            directory = base / "directory.csv"
            db = Db(base / "deep-context.sqlite")
            db.project_rows((
                ParentRow("jordan", "jordan", "Jordan Bravo"),
                ParentRow("casey", "casey", "Casey Delta"),
                ParentRow("robin", "robin", "Robin Echo"),
                PersonRow("p-verify", "jordan"),
                PersonRow("p-retarget", "casey"),
                PersonRow("p-fold", "robin"),
                PersonIdentifiersProjection("p-verify", (
                    PersonIdentifierRow("p-verify", "email", "jordan@example.test"),
                    PersonIdentifierRow("p-verify", "email", "jordan.alt@example.test"),
                    PersonIdentifierRow("p-verify", "phone", "+15550100"),
                )),
                PersonIdentifiersProjection("p-retarget", (
                    PersonIdentifierRow("p-retarget", "email", "casey@example.test"),
                    PersonIdentifierRow("p-retarget", "phone", "+15550101"),
                )),
                PersonIdentifiersProjection("p-fold", (
                    PersonIdentifierRow("p-fold", "email", "robin@example.test"),
                )),
                LinkRow(
                    "jordan-old", "jordan", "jordan-old", "pub",
                    "https://www.linkedin.com/in/jordan-bravo",
                    machine_action="verify", machine_approved="auto",
                ),
                LinkRow(
                    "casey-old", "casey", "casey-old", "pub",
                ),
                LinkRow(
                    "robin-old", "robin", "robin-old", "pub",
                    "https://www.linkedin.com/in/robin-echo",
                    machine_action="verify", machine_approved="auto",
                ),
            ))
            db.decide_identity(
                "casey-old", "retarget",
                replacement_url="https://www.linkedin.com/in/casey-delta",
                replacement_public_identifier="casey-delta",
            )

            def persist(dry_run: bool) -> dict:
                return PersistReviewIdentities(
                    directory_csv=directory,
                    dry_run=dry_run,
                    db=db,
                ).run()

            dry_payload = persist(dry_run=True)
            self.assertEqual(dry_payload["status"], "dry_run")
            self.assertFalse(directory.exists())
            with patch(
                "packs.ingestion.primitives.common.jsonio.now_iso",
                return_value="2026-08-06T12:00:00Z",
            ):
                payload = persist(dry_run=False)
                first = directory.read_bytes()
                persist(dry_run=False)
                second = directory.read_bytes()
            rows = CsvIO.read_dict_rows(directory)
        self.assertEqual(payload["review_persisted"], 3)
        self.assertEqual(second, first)
        by_email = {row["email"]: row["public_identifier"] for row in rows if row["email"]}
        by_phone = {row["phone"]: row["public_identifier"] for row in rows if row["phone"]}
        self.assertEqual(by_email["jordan@example.test"], "jordan-bravo")
        self.assertEqual(by_email["jordan.alt@example.test"], "jordan-bravo")
        self.assertEqual(by_email["casey@example.test"], "casey-delta")
        self.assertEqual(by_email["robin@example.test"], "robin-echo")
        self.assertEqual(by_phone["+15550101"], "casey-delta")
        self.assertNotIn("not-persisted", set(by_email.values()) | set(by_phone.values()))


if __name__ == "__main__":
    unittest.main()
