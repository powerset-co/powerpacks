"""Interaction-count propagation: schema helpers, source writers, merge rule,
index profile builders, hydration probe, and tier-0 identifier matching."""

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
from packs.indexing.lib.people import build_unified_profiles, flatten_people  # noqa: E402
from packs.indexing.lib.artifact_io import iter_artifact_rows  # noqa: E402


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


merge_mod = load_module(
    "merge_network_sources_interactions", "packs/ingestion/primitives/imports/merge_network_sources.py"
)
gmail_mod = load_module(
    "gmail_import_interactions", "packs/ingestion/primitives/discover/gmail/extract_gmail.py"
)
match_mod = load_module(
    "match_local_candidates_interactions", "packs/ingestion/primitives/imports/messages/match_local_candidates.py"
)
messages_import_mod = load_module(
    "import_messages_interactions", "packs/ingestion/primitives/imports/messages/importer.py"
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
            "phone": "+14155550123",
            "name": "Jane Doe",
            "source": "imessage",
            "match_status": "matched",
            "matched_person_id": "person-1",
            "matched_name": "Jane Doe",
            "matched_linkedin_url": "https://www.linkedin.com/in/janedoe",
            "imessage_message_count": "87",
            "whatsapp_message_count": "",
            "message_count": "87",
            "last_message": "2026-06-01T05:44:31.758167+00:00",
            "imessage_last_message": "2026-06-01T05:44:31.758167+00:00",
        }
        row.update(overrides)
        return row

    def test_contact_row_populates_interaction_columns(self):
        person = messages_import_mod.contact_row_to_messages_people(self.contact_row(), Path("contacts.csv"))
        self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 87})
        self.assertEqual(person["last_interaction"], "2026-06-01T05:44:31+00:00")
        self.assertNotIn("messages_total=", person["summary"])

    def test_candidate_merge_takes_channel_max_and_latest(self):
        left = messages_import_mod.contact_row_to_messages_people(self.contact_row(), Path("contacts.csv"))
        right = messages_import_mod.contact_row_to_messages_people(
            self.contact_row(
                imessage_message_count="40",
                whatsapp_message_count="9",
                imessage_last_message="2026-06-05T00:00:00+00:00",
                last_message="2026-06-05T00:00:00+00:00",
            ),
            Path("contacts.csv"),
        )
        merged = messages_import_mod.merge_matched_people_rows(left, right)
        self.assertEqual(json.loads(merged["interaction_counts"]), {"imessage": 87, "whatsapp": 9})
        self.assertEqual(merged["last_interaction"], "2026-06-05T00:00:00+00:00")


class ImportSchemaStalenessTests(unittest.TestCase):
    def test_pre_interaction_people_csv_invalidates_import(self):
        """A people.csv written before the interaction columns existed must be
        treated as stale even though its input fingerprints still match —
        otherwise the import no-ops forever and counts never materialize."""
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.csv"
            old.write_text("id,full_name\nx,y\n")
            new = Path(tmp) / "new.csv"
            new.write_text("id,interaction_counts,last_interaction\nx,,\n")
            self.assertTrue(messages_import_mod.people_csv_schema_stale(old))
            self.assertFalse(messages_import_mod.people_csv_schema_stale(new))
            self.assertFalse(messages_import_mod.people_csv_schema_stale(Path(tmp) / "absent.csv"))

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

    def test_message_row_to_people_carries_no_counts_without_approval_state(self):
        """Raw contacts.csv has no approval concept, so the direct merge path
        must not attribute interaction data; counts only enter through the
        user-approved review rows."""
        person = merge_mod.message_row_to_people(
            {
                "phone": "+14155550123",
                "name": "Jane Doe",
                "matched_person_id": "person-1",
                "message_count": "96",
                "imessage_message_count": "87",
                "whatsapp_message_count": "9",
                "last_message": "2026-06-01T05:44:31.758167+00:00",
            },
            Path("contacts.csv"),
        )
        self.assertEqual(person["interaction_counts"], "")
        self.assertEqual(person["last_interaction"], "")


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


class TierZeroMatchingTests(unittest.TestCase):
    def contact(self, phone: str, name: str = "") -> dict:
        row = {key: "" for key in match_mod.CSV_HEADERS}
        row.update({"phone": phone, "name": name})
        return row

    def test_phone_exact_matches_approved_nameless_contact(self):
        candidates = [match_mod.Candidate(id="c1", name="Jane Doe", phones=["+1 (415) 555-0123"])]
        rows = [self.contact("4155550123")]
        stats = match_mod.apply_matching(rows, candidates, approvals={"4155550123": True})
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(rows[0]["match_method"], "phone_exact")
        self.assertEqual(rows[0]["matched_person_id"], "c1")

    def test_unreviewed_identifier_match_is_only_suggested(self):
        candidates = [match_mod.Candidate(id="c1", name="Jane Doe", phones=["+14155550123"])]
        for approvals in (None, {}):
            rows = [self.contact("4155550123")]
            stats = match_mod.apply_matching(rows, candidates, approvals=approvals)
            self.assertEqual(stats["suggested"], 1, approvals)
            self.assertEqual(rows[0]["match_status"], "suggested")
            self.assertIn("awaiting approval", rows[0]["match_reason"])

    def test_reviewed_unapproved_contact_is_never_identifier_matched(self):
        candidates = [match_mod.Candidate(id="c1", name="Jane Doe", phones=["+14155550123"])]
        rows = [self.contact("4155550123")]
        stats = match_mod.apply_matching(rows, candidates, approvals={"4155550123": False})
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(rows[0]["match_status"], "unmatched")

    def test_email_handle_matches_approved_candidate_email(self):
        candidates = [match_mod.Candidate(id="c2", name="Jane Doe", emails=["jane@example.com"])]
        rows = [self.contact("Jane@Example.com")]
        stats = match_mod.apply_matching(rows, candidates, approvals={"jane@example.com": True})
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(rows[0]["match_method"], "email_exact")

    def test_ambiguous_phone_is_suggested_not_matched(self):
        candidates = [
            match_mod.Candidate(id="c1", name="Jane Doe", phones=["+14155550123"]),
            match_mod.Candidate(id="c2", name="June Doe", phones=["4155550123"]),
        ]
        rows = [self.contact("+14155550123", name="J Doe")]
        stats = match_mod.apply_matching(rows, candidates, approvals={"4155550123": True})
        self.assertEqual(stats["suggested"], 1)
        self.assertEqual(rows[0]["match_method"], "phone_exact_ambiguous")

    def test_phone_tier_precedes_name_tiers(self):
        candidates = [
            match_mod.Candidate(id="by-phone", name="Janet Doe", phones=["+14155550123"]),
            match_mod.Candidate(id="by-name", name="Jane Doe"),
        ]
        rows = [self.contact("+14155550123", name="Jane Doe")]
        match_mod.apply_matching(rows, candidates, approvals={"4155550123": True})
        self.assertEqual(rows[0]["matched_person_id"], "by-phone")

    def test_short_or_junk_phone_never_keys(self):
        self.assertEqual(match_mod.phone_match_key("911"), "")
        self.assertEqual(match_mod.phone_match_key(""), "")
        self.assertEqual(match_mod.phone_match_key("+14155550123"), "4155550123")
        self.assertEqual(match_mod.phone_match_key("14155550123"), "4155550123")

    def test_local_people_candidates_union_skips_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            people_csv = Path(tmp) / "people.csv"
            rows = []
            base = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
            known = dict(base, id="known-1", full_name="Known Person", public_identifier="knownperson")
            fresh = dict(
                base,
                id="local-1",
                full_name="Local Only",
                public_identifier="localonly",
                all_phones='["+14155559999"]',
                all_emails='["local@example.com"]',
            )
            rows.extend([known, fresh])
            with people_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            loaded = match_mod.load_people_candidates(people_csv, {"known-1"}, {"knownperson"})
        self.assertEqual([c.id for c in loaded], ["local-1"])
        self.assertEqual(loaded[0].phones, ["+14155559999"])
        self.assertEqual(loaded[0].emails, ["local@example.com"])

    def test_name_tier_match_demotes_to_suggested_without_approval(self):
        """With a review present, even name-exact matches outside the approved
        set must not carry `matched` (matched auto-derives in_network=true
        downstream, which would silently expand the user's approved set)."""
        candidates = [match_mod.Candidate(id="c1", name="Jane Doe")]
        rows = [self.contact("+14155550199", name="Jane Doe")]
        stats = match_mod.apply_matching(rows, candidates, approvals={"4155550100": True})
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(stats["suggested"], 1)
        self.assertEqual(rows[0]["match_method"], "name_exact_linkedin")
        self.assertIn("awaiting approval", rows[0]["match_reason"])

    def test_name_tier_match_stays_matched_for_approved_contact(self):
        candidates = [match_mod.Candidate(id="c1", name="Jane Doe")]
        rows = [self.contact("+14155550123", name="Jane Doe")]
        stats = match_mod.apply_matching(rows, candidates, approvals={"4155550123": True})
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(rows[0]["match_status"], "matched")

    def test_no_review_keeps_first_run_name_matching_intact(self):
        candidates = [match_mod.Candidate(id="c1", name="Jane Doe")]
        rows = [self.contact("+14155550199", name="Jane Doe")]
        stats = match_mod.apply_matching(rows, candidates, approvals=None)
        self.assertEqual(stats["matched"], 1)

    def test_load_review_approvals_maps_identifier_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_csv = Path(tmp) / "research_review.csv"
            with review_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["handle", "phone_e164", "in_network"])
                writer.writeheader()
                writer.writerows([
                    {"handle": "+14155550123", "phone_e164": "+14155550123", "in_network": "true"},
                    {"handle": "jane@example.com", "phone_e164": "", "in_network": "false"},
                ])
            approvals = match_mod.load_review_approvals(review_csv)
        self.assertEqual(approvals, {"4155550123": True, "jane@example.com": False})
        self.assertIsNone(match_mod.load_review_approvals(Path("/nonexistent/review.csv")))


if __name__ == "__main__":
    unittest.main()
