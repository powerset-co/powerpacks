"""The people merge contract: directory stamping, keying, grouping, ids, output.

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
    directory_slug_for,
    directory_slug_lookups,
    group_key,
    merge_group,
    person_id_for,
)
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    generate_person_id,
    normalize_people_row,
)
from packs.shared.csv_io import CsvIO

DIRECTORY_COLUMNS = ["source", "source_key", "status", "email", "phone", "name",
                     "linkedin_url", "public_identifier", "confidence"]


def person(**fields) -> dict[str, str]:
    return normalize_people_row(fields)


def write_people(path: Path, rows: list[dict[str, str]]) -> Path:
    CsvIO.write_dict_rows(path, PEOPLE_SCHEMA_COLUMNS, [person(**row) for row in rows])
    return path


def write_directory(path: Path, rows: list[dict[str, str]]) -> Path:
    CsvIO.write_dict_rows(path, DIRECTORY_COLUMNS, rows)
    return path


def directory_row(**fields) -> dict[str, str]:
    row = {"source": "gmail_msgvault", "source_key": "k", "status": "found", "confidence": "1.00"}
    row.update(fields)
    row.setdefault("public_identifier", "")
    return row


class KeyAndIdTests(unittest.TestCase):
    def test_a_slug_keys_on_linkedin_and_mints_the_canonical_uuid5(self) -> None:
        row = person(public_identifier="jordan-bravo", primary_email="jordan@example.com")
        self.assertEqual(group_key(row), "linkedin:jordan-bravo")
        self.assertEqual(person_id_for("linkedin:jordan-bravo"), generate_person_id("jordan-bravo"))

    def test_no_slug_keys_on_the_contact_key_and_the_key_IS_the_id(self) -> None:
        # The candidate:<key> id namespace is preserved verbatim so artifacts
        # already written under it keep addressing the same human.
        email_row = person(primary_email="Casey@Example.com")
        self.assertEqual(group_key(email_row), "candidate:email:casey@example.com")
        self.assertEqual(person_id_for(group_key(email_row)), "candidate:email:casey@example.com")
        phone_row = person(primary_phone="+15550100")
        self.assertEqual(group_key(phone_row), "candidate:phone:+15550100")

    def test_email_wins_over_phone_in_the_contact_key(self) -> None:
        row = person(primary_email="casey@example.com", primary_phone="+15550100")
        self.assertEqual(group_key(row), "candidate:email:casey@example.com")

    def test_an_existing_candidate_id_is_kept_verbatim_not_recomputed(self) -> None:
        # A row already addressed as candidate:phone:... must not be re-keyed to
        # candidate:email:... the moment it gains an email — that silently changes
        # its person_id and strands its facts/ file and review row.
        row = person(id="candidate:phone:+15550100",
                     primary_email="casey@example.com", primary_phone="+15550100")
        self.assertEqual(group_key(row), "candidate:phone:+15550100")
        self.assertEqual(person_id_for(group_key(row)), "candidate:phone:+15550100")
        # A slug still outranks the carried id: promotion to LinkedIn is the
        # one legitimate re-key.
        promoted = person(id="candidate:phone:+15550100", public_identifier="jordan-bravo")
        self.assertEqual(group_key(promoted), "linkedin:jordan-bravo")

    def test_a_row_with_no_slug_email_or_phone_is_unkeyable(self) -> None:
        self.assertEqual(group_key(person(full_name="Jordan Bravo")), "")

    def test_the_slug_is_read_from_linkedin_url_when_the_column_is_blank(self) -> None:
        row = person(linkedin_url="https://www.linkedin.com/in/Jordan-Bravo/")
        self.assertEqual(group_key(row), "linkedin:jordan-bravo")


class DirectoryStampTests(unittest.TestCase):
    def test_confident_found_rows_build_email_and_phone_lookups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = write_directory(Path(td) / "directory.csv", [
                directory_row(email="casey@example.com", linkedin_url="https://www.linkedin.com/in/casey-delta"),
                directory_row(phone="+15550100", linkedin_url="https://www.linkedin.com/in/jordan-bravo"),
                # excluded: not found / low confidence / no slug
                directory_row(email="ghost@example.com", status="not_found",
                              linkedin_url="https://www.linkedin.com/in/ghost"),
                directory_row(email="weak@example.com", confidence="0.40",
                              linkedin_url="https://www.linkedin.com/in/weak"),
                directory_row(email="bare@example.com", linkedin_url=""),
            ])
            emails, phones = directory_slug_lookups(path)
        self.assertEqual(emails, {"casey@example.com": "casey-delta"})
        self.assertEqual(phones, {"+15550100": "jordan-bravo"})

    def test_a_missing_directory_yields_empty_lookups(self) -> None:
        emails, phones = directory_slug_lookups(Path("/nonexistent/directory.csv"))
        self.assertEqual((emails, phones), ({}, {}))

    def test_lookup_checks_alias_emails_then_phones(self) -> None:
        emails = {"alias@example.com": "casey-delta"}
        phones = {"+15550100": "jordan-bravo"}
        by_alias = person(primary_email="primary@example.com", all_emails=["alias@example.com"])
        self.assertEqual(directory_slug_for(by_alias, emails, phones), "casey-delta")
        by_phone = person(primary_phone="+1 555 0100")
        self.assertEqual(directory_slug_for(by_phone, emails, phones), "jordan-bravo")
        self.assertEqual(directory_slug_for(person(primary_email="x@example.com"), emails, phones), "")

    def test_the_stamp_promotes_a_contact_row_into_a_linkedin_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            write_directory(base / "directory.csv", [
                directory_row(email="casey@example.com",
                              linkedin_url="https://www.linkedin.com/in/casey-delta"),
            ])
            write_people(base / "linkedin.csv", [
                {"public_identifier": "casey-delta", "full_name": "Casey Delta",
                 "linkedin_url": "https://www.linkedin.com/in/casey-delta"},
            ])
            write_people(base / "gmail.csv", [
                {"full_name": "C. Delta", "primary_email": "casey@example.com",
                 "interaction_counts": {"gmail": 12}},
            ])
            payload = PeopleMerge(
                inputs=[base / "linkedin.csv", base / "gmail.csv"],
                output_dir=base / "out",
                directory_csv=base / "directory.csv",
            ).run().to_payload()
            rows = CsvIO.read_dict_rows(base / "out" / "people.csv")
        self.assertEqual(payload["stats"]["directory_stamped"], 1)
        self.assertEqual(payload["stats"]["rows"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], generate_person_id("casey-delta"))
        self.assertEqual(rows[0]["full_name"], "Casey Delta")          # linkedin precedence
        self.assertEqual(rows[0]["primary_email"], "casey@example.com")  # gained from gmail
        self.assertEqual(json.loads(rows[0]["interaction_counts"]), {"gmail": 12})

    def test_approved_deep_context_mapping_retargets_an_attached_source_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            write_directory(base / "directory.csv", [
                directory_row(source="deep_context_review", email="casey@example.com",
                              linkedin_url="https://www.linkedin.com/in/casey-correct"),
            ])
            write_people(base / "gmail.csv", [
                {"public_identifier": "casey-wrong", "full_name": "Casey Delta",
                 "primary_email": "casey@example.com", "linkedin_url": "https://www.linkedin.com/in/casey-wrong"},
            ])
            PeopleMerge(inputs=[base / "gmail.csv"], output_dir=base / "out",
                        directory_csv=base / "directory.csv").run()
            (row,) = CsvIO.read_dict_rows(base / "out" / "people.csv")
        self.assertEqual(row["public_identifier"], "casey-correct")


class MergeGroupTests(unittest.TestCase):
    def test_first_non_empty_wins_per_scalar_column(self) -> None:
        merged = merge_group("linkedin:jordan-bravo", [
            person(public_identifier="jordan-bravo", full_name="Jordan Bravo", headline=""),
            person(public_identifier="jordan-bravo", full_name="J. Bravo", headline="Founder"),
        ])
        self.assertEqual(merged["full_name"], "Jordan Bravo")
        self.assertEqual(merged["headline"], "Founder")

    def test_alias_lists_channels_and_artifacts_set_union(self) -> None:
        merged = merge_group("linkedin:jordan-bravo", [
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
        merged = merge_group("linkedin:jordan-bravo", [
            person(public_identifier="jordan-bravo", all_emails=["only@example.com"],
                   all_phones=["+15550100"]),
        ])
        self.assertEqual(merged["primary_email"], "only@example.com")
        self.assertEqual(merged["primary_phone"], "+15550100")

    def test_interaction_counts_take_the_channel_wise_max_and_latest_activity(self) -> None:
        merged = merge_group("linkedin:jordan-bravo", [
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
            {"public_identifier": "jordan-bravo", "full_name": "Jordan Bravo",
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
            payload = PeopleMerge(inputs=self._inputs(base), output_dir=out,
                                  directory_csv=base / "directory.csv").run().to_payload()
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
            payload = PeopleMerge(inputs=self._inputs(base), output_dir=base / "merged",
                                  directory_csv=base / "directory.csv").run().to_payload()
        stats = payload["stats"]
        self.assertEqual(stats["input_rows_total"], 5)
        self.assertEqual(stats["rows"], 3)               # jordan (x2 rows), casey, rowan
        self.assertEqual(stats["linkedin_ids"], 1)
        self.assertEqual(stats["candidate_ids"], 2)
        self.assertEqual(stats["dropped_unkeyable"], 1)  # the name-only row
        self.assertEqual(stats["groups_by_size"], {"1": 2, "2": 1})

    def test_contact_only_people_are_kept_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            PeopleMerge(inputs=self._inputs(base), output_dir=base / "merged",
                        directory_csv=base / "directory.csv").run().to_payload()
            rows = CsvIO.read_dict_rows(base / "merged" / "people.csv")
        by_id = {row["id"]: row for row in rows}
        self.assertIn("candidate:email:casey@example.com", by_id)
        self.assertIn("candidate:phone:+15550100", by_id)
        self.assertEqual(by_id["candidate:phone:+15550100"]["public_identifier"], "")

    def test_rerunning_rewrites_byte_identical_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            inputs = self._inputs(base)
            merge = PeopleMerge(inputs=inputs, output_dir=base / "merged",
                                directory_csv=base / "directory.csv")
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
            PeopleMerge(inputs=self._inputs(base), output_dir=base / "merged",
                        directory_csv=base / "directory.csv").run().to_payload()
            rows = CsvIO.read_dict_rows(base / "merged" / "people.csv")
        pubs = {row["public_identifier"] for row in rows}
        self.assertIn("jordan-bravo", pubs)   # the "no" mark did not drop them
        self.assertNotIn("synth", pubs)       # the approved synthetic did not enter

    def test_no_inputs_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            payload = PeopleMerge(inputs=[base / "missing.csv"], output_dir=base / "merged",
                                  directory_csv=base / "directory.csv").run().to_payload()
        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["reason"], "missing_import_people_csvs")
        self.assertFalse((base / "merged" / "people.csv").exists())


if __name__ == "__main__":
    unittest.main()
