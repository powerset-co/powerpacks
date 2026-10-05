"""The people merge contract: source IDs, metadata unions and output.

The merge is the whole fan-in — three per-source people.csv in, one
merged/people.csv + manifest.json out. It applies no human decisions and drops
nobody who has any keyable identity.
"""

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.imports.merge_people import (
    MERGE_SOURCES,
    PeopleMerge,
    group_key,
    merge_group,
)
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    generate_person_id,
)
from packs.shared.csv_io import CsvIO


def person(**fields) -> PeopleRow:
    return PeopleRow.model_validate(fields)


def write_people(path: Path, rows: list[dict[str, str]]) -> Path:
    CsvIO.write_dict_rows(path, PEOPLE_SCHEMA_COLUMNS, [person(**row).to_row() for row in rows])
    return path


class KeyAndIdTests(unittest.TestCase):
    def test_existing_source_ids_survive_profile_metadata(self):
        for source_id in ("source-jordan", "candidate:phone:+15550100", generate_person_id("jordan-bravo")):
            with self.subTest(source_id=source_id):
                row = person(id=source_id, public_identifier="wrong-profile", primary_email="casey@example.com")
                self.assertEqual(group_key(row), source_id)
                self.assertEqual(merge_group(group_key(row), [row])["id"], source_id)

    def test_missing_id_uses_source_endpoint(self):
        self.assertEqual(group_key(person(primary_email="Casey@Example.com", primary_phone="+15550100")),
                         "candidate:email:casey@example.com")
        self.assertEqual(group_key(person(primary_phone="+15550100")), "candidate:phone:+15550100")

    def test_profile_or_name_without_source_id_or_endpoint_is_unkeyable(self):
        self.assertEqual(group_key(person(full_name="Jordan Bravo", public_identifier="jordan-bravo")), "")


class MergeGroupTests(unittest.TestCase):
    def test_first_non_empty_wins_per_scalar_column(self) -> None:
        merged = merge_group("source-jordan", [
            person(public_identifier="jordan-bravo", full_name="Jordan Bravo", headline=""),
            person(public_identifier="jordan-bravo", full_name="J. Bravo", headline="Founder"),
        ])
        self.assertEqual(merged["full_name"], "Jordan Bravo")
        self.assertEqual(merged["headline"], "Founder")

    def test_alias_lists_channels_and_artifacts_set_union(self) -> None:
        merged = merge_group("source-jordan", [
            person(public_identifier="jordan-bravo", primary_email="work@example.com",
                   source_channels="linkedin_csv", source_artifacts="a.csv"),
            person(public_identifier="jordan-bravo", primary_email="home@example.com",
                   all_phones=["+15550100"], source_channels="gmail_msgvault", source_artifacts="b.csv"),
        ])
        self.assertEqual(json.loads(merged["all_emails"]), ["work@example.com", "home@example.com"])
        self.assertEqual(json.loads(merged["all_phones"]), ["+15550100"])
        self.assertEqual(merged["source_channels"], "linkedin_csv,gmail_msgvault")
        self.assertEqual(json.loads(merged["source_artifacts"]), ["a.csv", "b.csv"])

    def test_a_primary_is_promoted_from_the_alias_union(self) -> None:
        merged = merge_group("source-jordan", [
            person(public_identifier="jordan-bravo", all_emails=["only@example.com"],
                   all_phones=["+15550100"]),
        ])
        self.assertEqual(merged["primary_email"], "only@example.com")
        self.assertEqual(merged["primary_phone"], "+15550100")

    def test_interaction_counts_take_the_channel_wise_max_and_latest_activity(self) -> None:
        merged = merge_group("source-jordan", [
            person(public_identifier="jordan-bravo", interaction_counts={"gmail": 142},
                   last_interaction="2026-01-01T00:00:00+00:00"),
            person(public_identifier="jordan-bravo", interaction_counts={"gmail": 7, "imessage": 87},
                   last_interaction="2026-06-01T05:44:31+00:00"),
        ])
        self.assertEqual(json.loads(merged["interaction_counts"]), {"gmail": 142, "imessage": 87})
        self.assertEqual(merged["last_interaction"], "2026-06-01T05:44:31+00:00")


class MergeRunTests(unittest.TestCase):
    def _inputs(self, base: Path) -> list[Path]:
        write_people(base / "import/linkedin/people.csv", [
            {"id": generate_person_id("jordan-bravo"), "source_channels": "linkedin_csv",
             "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo",
             "linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
        ])
        write_people(base / "import/gmail/people.csv", [
            {"public_identifier": "jordan-bravo", "primary_email": "jordan@example.com",
             "linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
            {"full_name": "Casey Delta", "primary_email": "casey@example.com"},
            {"full_name": "No Identity At All"},
        ])
        write_people(base / "import/messages/people.csv", [
            {"full_name": "Rowan Echo", "primary_phone": "+15550100"},
        ])
        return [base / "import" / source / "people.csv" for source in MERGE_SOURCES]

    def test_one_output_file_pair_and_no_reader_less_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            out = base / "merged"
            payload = PeopleMerge(inputs=self._inputs(base), output_dir=out).run().to_payload()
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["manifest.json", "people.csv"])
            header = list(CsvIO.read_dict_rows(out / "people.csv")[0])
        self.assertEqual(header, PEOPLE_SCHEMA_COLUMNS)
        for retired in ("merge_key", "merge_confidence", "merge_sources", "merged_row_count",
                        "needs_review", "linkedin_verified", "linkedin_verified_confidence",
                        "linkedin_verified_reason"):
            self.assertNotIn(retired, header)
        self.assertEqual(payload["status"], "completed")

    def test_counts_and_the_one_unkeyable_drop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            payload = PeopleMerge(inputs=self._inputs(base), output_dir=base / "merged").run().to_payload()
        stats = payload["stats"]
        self.assertEqual(stats["input_rows_total"], 5)
        self.assertEqual(stats["rows"], 4)  # profile metadata does not join the Gmail contact
        self.assertEqual(stats["linkedin_ids"], 1)
        self.assertEqual(stats["candidate_ids"], 3)
        self.assertEqual(stats["dropped_unkeyable"], 1)  # the name-only row
        self.assertEqual(stats["groups_by_size"], {"1": 4})

    def test_contact_only_people_are_kept_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            PeopleMerge(inputs=self._inputs(base), output_dir=base / "merged").run().to_payload()
            rows = CsvIO.read_dict_rows(base / "merged" / "people.csv")
        by_id = {row["id"]: row for row in rows}
        self.assertIn("candidate:email:casey@example.com", by_id)
        self.assertIn("candidate:phone:+15550100", by_id)
        self.assertEqual(by_id["candidate:phone:+15550100"]["public_identifier"], "")

    def test_rerunning_rewrites_byte_identical_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            inputs = self._inputs(base)
            merge = PeopleMerge(inputs=inputs, output_dir=base / "merged")
            merge.run()
            first = (base / "merged" / "people.csv").read_bytes()
            merge.run()
            self.assertEqual((base / "merged" / "people.csv").read_bytes(), first)

    def test_the_merge_never_reads_the_overrides_dir(self) -> None:
        # Human decisions are NOT applied here: an overrides file that names a
        # person cannot add, drop, or redirect a merged row.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            overrides = base / "overrides"
            overrides.mkdir()
            (overrides / "review.csv").write_text(
                "public_identifier,person_id,network_worth\njordan-bravo,pid,no\n", encoding="utf-8")
            (overrides / "synthetic-people.csv").write_text(
                "id,public_identifier,approved\ncandidate:email:synth@example.com,synth,yes\n",
                encoding="utf-8")
            PeopleMerge(inputs=self._inputs(base), output_dir=base / "merged").run().to_payload()
            rows = CsvIO.read_dict_rows(base / "merged" / "people.csv")
        pubs = {row["public_identifier"] for row in rows}
        self.assertIn("jordan-bravo", pubs)   # the "no" mark did not drop them
        self.assertNotIn("synth", pubs)       # the approved synthetic did not enter

    def test_no_inputs_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            payload = PeopleMerge(inputs=[base / "missing.csv"], output_dir=base / "merged").run().to_payload()
        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["reason"], "missing_import_people_csvs")
        self.assertFalse((base / "merged" / "people.csv").exists())


if __name__ == "__main__":
    unittest.main()
