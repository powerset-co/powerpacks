"""Approved Deep Context identity decisions persist through directory.csv."""
import csv
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from packs.ingestion.primitives.deep_context.persist_review_identities import run
from packs.ingestion.primitives.deep_context.review_store import OVERRIDE_COLUMNS
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


class PersistReviewIdentitiesTests(unittest.TestCase):
    def test_approved_real_identities_are_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            people, review = base / "people.csv", base / "review.csv"
            consolidated, retargeted, directory = base / "consolidated.csv", base / "retargeted.csv", base / "directory.csv"
            write_csv(people, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "p-verify", "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "all_emails": json.dumps(["jordan.alt@example.test"]), "primary_phone": "+15550100"},
                {"id": "p-retarget", "full_name": "Casey Delta", "primary_email": "casey@example.test"},
            ])
            write_csv(review, OVERRIDE_COLUMNS, [
                {"public_identifier": "jordan-old", "person_id": "p-verify", "action": "verify", "approved": "auto",
                 "linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
                {"public_identifier": "casey-old", "person_id": "p-retarget", "action": "retarget", "approved": "yes",
                 "new_linkedin_url": "https://www.linkedin.com/in/casey-delta"},
                {"public_identifier": "pending", "person_id": "p-retarget", "action": "retarget", "approved": "",
                 "new_linkedin_url": "https://www.linkedin.com/in/not-persisted"},
                {"public_identifier": "detached", "person_id": "p-verify", "action": "detach", "approved": "auto",
                 "linkedin_url": "https://www.linkedin.com/in/not-persisted"},
            ])
            write_csv(consolidated, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "p-fold", "full_name": "Robin Echo", "primary_email": "robin@example.test",
                 "linkedin_url": "https://www.linkedin.com/in/robin-echo"},
            ])
            write_csv(retargeted, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "p-retarget", "full_name": "Casey Delta", "primary_phone": "+15550101",
                 "linkedin_url": "https://www.linkedin.com/in/casey-delta"},
            ])
            args = Namespace(review_csv=str(review), people_csv=str(people), consolidate_people_csv=str(consolidated),
                             retarget_people_csv=str(retargeted), directory_csv=str(directory), dry_run=False)
            dry_run_args = Namespace(**{**vars(args), "dry_run": True})
            dry_payload = run(dry_run_args)
            self.assertEqual(dry_payload["status"], "dry_run")
            self.assertFalse(directory.exists())
            payload = run(args)
            first = directory.read_bytes()
            run(args)
            second = directory.read_bytes()
            rows = CsvIO.read_dict_rows(directory)
        self.assertEqual(payload["review_persisted"], 2)
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
