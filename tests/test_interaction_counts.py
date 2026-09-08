"""Interaction-count propagation: schema helpers, source writers, merge rule,
index profile builders, and hydration probe."""

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS,
    latest_interaction,
    merge_interaction_counts,
    normalize_interaction_timestamp,
    parse_interaction_counts,
)
from packs.ingestion.schemas.message_contacts import MessageContact
from packs.ingestion.primitives.imports.messages.util import contact_to_person
from packs.indexing.lib.people import build_unified_profiles, flatten_people  # noqa: E402
from packs.indexing.lib.artifact_io import iter_artifact_rows  # noqa: E402
from packs.ingestion.primitives.imports import merge_people as merge_mod  # noqa: E402


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gmail_mod = load_module(
    "gmail_import_interactions", "packs/ingestion/primitives/discover/gmail/extract_gmail.py"
)


class SchemaHelperTests(unittest.TestCase):
    def test_schema_includes_interaction_columns(self):
        self.assertIn("interaction_counts", PEOPLE_SCHEMA_COLUMNS)
        self.assertIn("last_interaction", PEOPLE_SCHEMA_COLUMNS)

    def test_parse_interaction_counts_drops_junk(self):
        parsed = parse_interaction_counts('{"gmail": "12", "imessage": 0, "": 5, "whatsapp": "x"}')
        self.assertEqual(parsed, {"gmail": 12})
        self.assertEqual(parse_interaction_counts("not json"), {})
        self.assertEqual(parse_interaction_counts(""), {})

    def test_merge_is_channel_wise_max_not_sum(self):
        merged = merge_interaction_counts('{"gmail": 10, "imessage": 5}', {"gmail": 7, "whatsapp": 3})
        self.assertEqual(merged, {"gmail": 10, "imessage": 5, "whatsapp": 3})
        # idempotent: merging a value with itself changes nothing
        self.assertEqual(merge_interaction_counts(merged, merged), merged)

    def test_timestamp_normalization_handles_both_source_formats(self):
        self.assertEqual(normalize_interaction_timestamp("2024-01-01 23:44:00+00:00"), "2024-01-01T23:44:00+00:00")
        self.assertEqual(
            normalize_interaction_timestamp("2026-06-01T05:44:31.758167+00:00"), "2026-06-01T05:44:31+00:00"
        )
        self.assertEqual(normalize_interaction_timestamp(""), "")
        self.assertEqual(normalize_interaction_timestamp("garbage"), "")

    def test_latest_interaction_picks_most_recent(self):
        self.assertEqual(
            latest_interaction("2024-01-01 23:44:00+00:00", "2026-06-01T05:44:31.758167+00:00", ""),
            "2026-06-01T05:44:31+00:00",
        )
        self.assertEqual(latest_interaction("", None), "")


class MessagesWriterTests(unittest.TestCase):
    def contact_row(self, **overrides):
        row = {
            "phone": "+15550100123",
            "name": "Jordan Bravo",
            "source": "imessage",
            "imessage_message_count": "87",
            "whatsapp_message_count": "",
            "message_count": "87",
            "last_message": "2026-06-01T05:44:31.758167+00:00",
            "imessage_last_message": "2026-06-01T05:44:31.758167+00:00",
        }
        row.update(overrides)
        return MessageContact.from_csv_row(row)

    def test_contact_row_populates_interaction_columns(self):
        person = contact_to_person(self.contact_row(), Path("contacts.csv"))
        self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 87})
        self.assertEqual(person["last_interaction"], "2026-06-01T05:44:31+00:00")
        self.assertNotIn("messages_total=", person["summary"])



class GmailWriterTests(unittest.TestCase):
    def test_msgvault_rows_carry_gmail_counts(self):
        people = gmail_mod.people_rows_from_msgvault(
            [
                {
                    "email": "jane@example.com",
                    "display_name": "Jane Doe",
                    "total_messages": "142",
                    "last_interaction": "2024-01-01 23:44:00+00:00",
                },
                {"email": "ghost@example.com", "display_name": "", "total_messages": "0", "last_interaction": ""},
            ],
            ["artifact.csv"],
        )
        self.assertEqual(json.loads(people[0]["interaction_counts"]), {"gmail": 142})
        self.assertEqual(people[0]["last_interaction"], "2024-01-01T23:44:00+00:00")
        self.assertEqual(people[1]["interaction_counts"], "")
        self.assertEqual(people[1]["last_interaction"], "")


class MergeGroupTests(unittest.TestCase):
    def person_row(self, **overrides):
        row = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
        row.update(
            {
                "id": "person-1",
                "full_name": "Jane Doe",
                "linkedin_url": "https://www.linkedin.com/in/janedoe",
                "public_identifier": "janedoe",
            }
        )
        row.update(overrides)
        return row

    def test_merge_group_combines_channels_across_sources(self):
        gmail_row = self.person_row(
            source_channels="gmail_msgvault",
            interaction_counts='{"gmail": 142}',
            last_interaction="2024-01-01T23:44:00+00:00",
        )
        messages_row = self.person_row(
            source_channels="imessage",
            interaction_counts='{"imessage": 87}',
            last_interaction="2026-06-01T05:44:31+00:00",
        )
        merged = merge_mod.merge_group("linkedin:janedoe", [gmail_row, messages_row])
        self.assertEqual(json.loads(merged["interaction_counts"]), {"gmail": 142, "imessage": 87})
        self.assertEqual(merged["last_interaction"], "2026-06-01T05:44:31+00:00")

    def test_remerge_with_own_output_is_idempotent(self):
        rows = [
            self.person_row(source_channels="gmail_msgvault", interaction_counts='{"gmail": 142}'),
            self.person_row(source_channels="imessage", interaction_counts='{"imessage": 87}'),
        ]
        first = merge_mod.merge_group("linkedin:janedoe", rows)
        second = merge_mod.merge_group("linkedin:janedoe", [first, *rows])
        self.assertEqual(
            json.loads(second["interaction_counts"]), json.loads(first["interaction_counts"])
        )

    def test_merge_group_mints_the_durable_person_id_from_the_key(self):
        merged = merge_mod.merge_group("linkedin:janedoe", [self.person_row()])
        self.assertEqual(merged["id"], merge_mod.generate_person_id("janedoe"))
        contact = merge_mod.merge_group(
            "candidate:email:casey@example.com",
            [self.person_row(public_identifier="", linkedin_url="", primary_email="casey@example.com")],
        )
        self.assertEqual(contact["id"], "candidate:email:casey@example.com")


class IndexProfileTests(unittest.TestCase):
    def people_csv_rows(self):
        row = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
        row.update(
            {
                "id": "person-1",
                "full_name": "Jane Doe",
                "linkedin_url": "https://www.linkedin.com/in/janedoe",
                "public_identifier": "janedoe",
                "interaction_counts": '{"gmail": 142, "imessage": 87}',
                "last_interaction": "2026-06-01T05:44:31+00:00",
            }
        )
        return [row]

    def test_unified_profiles_populate_total_interactions(self):
        profiles = build_unified_profiles(flatten_people(self.people_csv_rows()))
        self.assertEqual(profiles[0]["total_interactions"], 229)
        self.assertEqual(profiles[0]["interaction_counts"], {"gmail": 142, "imessage": 87})
        self.assertEqual(profiles[0]["last_interaction"], "2026-06-01T05:44:31+00:00")

    def test_shim_profile_records_and_hydration_probe(self):
        import duckdb

        shim = load_module("build_local_duckdb_shim_interactions", "scripts/build-local-duckdb-shim.py")
        self.assertIn("total_interactions", shim.LOCAL_TABLE_CONTRACT["local_person_profiles"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            people_csv = tmp_path / "people.csv"
            with people_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                writer.writeheader()
                writer.writerows(self.people_csv_rows())
            record_path = shim.materialize_person_profiles_from_csv(people_csv, tmp_path, "local:user")
            record = next(iter_artifact_rows(record_path))
            self.assertEqual(record["total_interactions"], 229)
            self.assertEqual(json.loads(record["interaction_counts"]), {"gmail": 142, "imessage": 87})
            self.assertEqual(record["last_interaction"], "2026-06-01T05:44:31+00:00")

            # The hydration probe reads any table with person_id + total_interactions.
            sys.path.insert(0, str(ROOT / "packs/search/primitives/lib"))
            try:
                hydrate = load_module(
                    "hydrate_people_interactions", "packs/search/primitives/hydrate_people/hydrate_people.py"
                )
            finally:
                sys.path.pop(0)
            self.assertIn("local_person_profiles", hydrate.LOCAL_INTERACTION_SUMMARY_TABLES)
            conn = duckdb.connect(":memory:")
            conn.execute(
                "CREATE TABLE local_person_profiles AS SELECT * FROM read_parquet(?)",
                [str(record_path)],
            )
            counts = hydrate.local_interaction_counts(conn, [record["person_id"]])
            self.assertEqual(counts, {record["person_id"]: 229})


if __name__ == "__main__":
    unittest.main()
