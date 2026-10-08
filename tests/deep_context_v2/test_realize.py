"""Export actual tiny store families through the people.csv contract."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_realize
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize, by_most_messages, fill_profile, index_command, main, message_total
from packs.ingestion.schemas.people_schema import JSON_LIST_COLUMNS, JSON_OBJECT_COLUMNS, LIST_VALUE_COLUMNS, PEOPLE_SCHEMA_COLUMNS, normalize_people_row, parse_interaction_counts
from packs.indexing.lib.io import read_csv
from packs.shared.csv_io import CsvIO

NOW = "2026-10-07T22:00:00Z"
URL = "https://www.linkedin.com/in/jordan-bravo"


class RealizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = open_store(self.root / "deep-context" / "deep-context-v2.sqlite")
        self.addCleanup(self.conn.close)
        self.node = Realize(self.conn, self.root)

    def candidate(self, key: str, counts: object = "", *, parent: str = "p:1", name: str = "Jordan Bravo", owner: int = 0, facts: bool = False, last: str = "", channel: str = "imessage") -> None:
        payload = {"last_interaction": last}
        if counts is not None:
            payload["interaction_counts"] = counts
        queries.upsert_candidates(self.conn, [(key, name, owner, json.dumps(payload), NOW)])
        queries.insert_candidate_identifiers(self.conn, [(key, "email", f"{key}@example.com", f"{key}@example.com")])
        queries.insert_candidate_sources(self.conn, [(key, channel)])
        self.conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES (?,?,'singleton',?)", (key, parent, NOW))
        if facts:
            queries.upsert_facts(self.conn, key, "{}", "fp", "fake", "none", NOW)
        self.conn.commit()

    def profile(self, key: str, member: str, *, url: str = URL, origin: str = "research", decider: str = "machine") -> None:
        self.conn.execute("INSERT INTO candidate_linkedins(candidate_id,linkedin_url,member_id,origin,verdict,decided_by,judgment_fingerprint,reason,created_at) VALUES (?,?,?,?,'confirmed',?,'fp','',?)", (key, url, member, origin, decider, NOW))
        self.conn.commit()

    def test_blank_missing_and_object_counts_follow_people_contract(self) -> None:
        for key, counts in [("a", ""), ("b", None), ("c", '{" Gmail ":"3","imessage":0,"bad":"x"}'), ("d", {"whatsapp": 2}), ("e", "not-json")]:
            self.candidate(key, counts, facts=key == "a")
        members = queries_realize.members_of_talked_to_parents(self.conn)
        self.assertEqual([m.interaction_counts for m in members], [{}, {}, {"gmail": 3}, {"whatsapp": 2}, {}])
        rows, counts = self.node.build()
        self.assertEqual(json.loads(rows[0]["interaction_counts"]), {"gmail": 3, "whatsapp": 2})
        self.assertEqual(counts["rows"], 1)

    def test_family_export_unions_members_and_uses_message_order(self) -> None:
        self.candidate("a", '{"imessage":2}', facts=True, last="2026-01-01T00:00:00Z")
        self.candidate("b", '{"imessage":3,"whatsapp":4}', name="Jordan Preferred", last="2026-02-01T00:00:00Z", channel="whatsapp")
        queries.insert_candidate_identifiers(self.conn, [("a", "phone", "+15550100000", "+1 555 010 0000"), ("b", "phone", "+15550100000", "+1 555 010 0000")])
        rows, counts = self.node.build()
        row = rows[0]
        self.assertEqual(set(row), set(PEOPLE_SCHEMA_COLUMNS))
        self.assertEqual(row["full_name"], "Jordan Preferred")
        self.assertEqual(row["primary_email"], "b@example.com")
        self.assertEqual(json.loads(row["all_emails"]), ["b@example.com", "a@example.com"])
        self.assertEqual(json.loads(row["all_phones"]), ["+1 555 010 0000"])
        self.assertEqual(row["primary_phone"], "+1 555 010 0000")
        self.assertEqual(row["source_channels"], "imessage,whatsapp")
        self.assertEqual(json.loads(row["interaction_counts"]), {"imessage": 5, "whatsapp": 4})
        self.assertEqual(row["last_interaction"], "2026-02-01T00:00:00Z")
        self.assertEqual(counts["worth_undecided"], 1)
        self.assertEqual(counts["no_profile_key"], 1)

    def test_stable_tie_order_and_message_total(self) -> None:
        Member = queries_realize.Member
        a = Member("p:1", "a", "Jordan", {"imessage": 2}, "")
        b = Member("p:1", "b", "Casey", {"gmail": 1, "whatsapp": 1}, "")
        self.assertEqual(message_total(b), 2)
        self.assertEqual(by_most_messages([b, a]), [a, b])
        self.assertEqual(by_most_messages([]), [])

    def test_owner_and_family_without_facts_are_not_exported(self) -> None:
        self.candidate("owner", facts=True, owner=1, parent="p:owner")
        self.candidate("untalked", parent="p:other")
        self.assertEqual(self.node.build()[0], [])

    def test_synthetic_and_worth_no_still_export_without_url(self) -> None:
        self.candidate("a", facts=True)
        self.profile("a", "synthetic:h", url="synthetic:h", origin="synthetic", decider="human")
        self.conn.execute("INSERT INTO worth(candidate_id,worth,decided_by,reason,created_at) VALUES ('a','no','human','skip',?)", (NOW,))
        rows, counts = self.node.build()
        self.assertEqual(rows[0]["linkedin_url"], "")
        self.assertEqual(counts["synthetic"], 1)
        self.assertEqual(counts["worth_no"], 1)

    def test_confirmed_profile_missing_cache_is_counted(self) -> None:
        self.candidate("a", facts=True, parent="li:42")
        self.profile("a", "42")
        rows, counts = self.node.build()
        self.assertEqual(rows[0]["linkedin_url"], URL)
        self.assertEqual(rows[0]["public_identifier"], "jordan-bravo")
        self.assertEqual(counts["profiles_missing"], 1)
        self.assertEqual(counts["with_url"], 1)

    def test_fill_profile_uses_actual_transform_and_serializes_lists(self) -> None:
        row = {column: "" for column in PEOPLE_SCHEMA_COLUMNS}
        row.update(linkedin_url=URL, public_identifier="jordan-bravo")
        fill_profile(row, {"raw_response": {"firstName": "Jordan", "lastName": "Bravo", "headline": "Founder", "experiences": [], "fullName": "Jordan Bravo"}})
        self.assertEqual(row["full_name"], "Jordan Bravo")
        self.assertEqual(row["headline"], "Founder")
        self.assertIsInstance(json.loads(row["work_experiences"]), list)
        self.assertIsInstance(json.loads(row["education"]), list)

    def test_cached_profile_fills_export(self) -> None:
        self.candidate("a", facts=True, parent="li:42")
        self.profile("a", "42")
        # Only the cache reader is substituted; the profile-to-people transform is real.
        record = {"raw_response": {"firstName": "Jordan", "lastName": "Cached", "headline": "Founder", "experiences": []}}
        with patch("packs.ingestion.primitives.deep_context_v2.realize.realize.read_usable_cached_profile", return_value=record):
            rows, counts = self.node.build()
        self.assertEqual(rows[0]["full_name"], "Jordan Cached")
        self.assertEqual(counts["profiles_filled"], 1)
        self.assertEqual(counts["profiles_missing"], 0)

    def test_node_run_exports_csv_and_rerun_is_byte_identical(self) -> None:
        self.candidate("a", '{"imessage":3}', facts=True)
        self.assertEqual(self.node.run().status, "completed")
        first = self.node.people_csv().read_bytes()
        self.assertEqual(self.node.run().status, "completed")
        self.assertEqual(self.node.people_csv().read_bytes(), first)
        rows = CsvIO.read_dict_rows(self.node.people_csv())
        self.assertEqual(rows[0]["id"], "p:1")
        self.assertEqual(json.loads(rows[0]["interaction_counts"]), {"imessage": 3})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)

    def test_produced_csv_satisfies_downstream_field_contract(self) -> None:
        self.candidate("a", '{"imessage":3}', facts=True, parent="li:42")
        self.profile("a", "42")
        record = {"raw_response": {"firstName": "Jordan", "lastName": "Bravo", "headline": "Founder", "experiences": []}}
        with patch("packs.ingestion.primitives.deep_context_v2.realize.realize.read_usable_cached_profile", return_value=record):
            self.node.run()
        # Use indexing's real CSV reader, not a second invented interpretation of the output.
        row = read_csv(self.node.people_csv())[0]
        self.assertEqual(list(row), PEOPLE_SCHEMA_COLUMNS)
        self.assertEqual(normalize_people_row(row), row)
        self.assertTrue(all(isinstance(value, str) for value in row.values()))
        for column in JSON_LIST_COLUMNS | LIST_VALUE_COLUMNS:
            if row[column]:
                self.assertIsInstance(json.loads(row[column]), list, column)
        for column in JSON_OBJECT_COLUMNS:
            if row[column]:
                self.assertIsInstance(json.loads(row[column]), dict, column)
        self.assertEqual(parse_interaction_counts(row["interaction_counts"]), {"imessage": 3})
        self.assertEqual(json.loads(row["all_emails"]), [row["primary_email"]])
        self.assertEqual(row["public_identifier"], "jordan-bravo")

    def test_dry_run_cli_does_not_write_export_or_manifest(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--data-root", str(self.root), "--dry-run"]), 0)
        self.assertEqual(json.loads(out.getvalue())["rows"], 0)
        self.assertFalse(self.node.people_csv().exists())
        self.assertFalse((self.root / "deep-context" / "v2-manifests").exists())
        command = index_command(self.node.people_csv(), self.root)
        self.assertIn("linkedin_modal_pipeline.py index-people", command)
        self.assertNotIn("--dry-run", command)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--data-root", str(self.root)]), 0)
        self.assertIn("people.csv:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
