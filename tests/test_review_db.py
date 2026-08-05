"""review_db (sqlite P0): schema enums, strict import, byte-identical export."""
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dataclasses import fields

from packs.ingestion.primitives.deep_context.review_db import (
    DecisionKind,
    DecisionRow,
    LinkRow,
    ParentRow,
    ReviewDb,
    ReviewDbImportError,
    RowKind,
    classify_review_key,
)
from packs.ingestion.primitives.deep_context.review_store import (
    HEAL_DETACH_SOURCE,
    HUMAN_DECISION_SOURCES,
    OVERRIDE_COLUMNS,
    ReviewSource,
    load_override_rows,
    write_override_rows,
)


def blank_row(**cells) -> dict[str, str]:
    row = {column: "" for column in OVERRIDE_COLUMNS}
    row.update(cells)
    return row


def write_fixture(path: Path, rows: dict[str, dict[str, str]]) -> None:
    write_override_rows(path, rows)


class ClassifyReviewKeyTests(unittest.TestCase):
    def test_all_namespaces(self):
        cases = {
            "jordan-bravo-1": RowKind.PUB,
            "candidate:email:casey@example.com": RowKind.CANDIDATE_EMAIL,
            "candidate:phone:15550100": RowKind.CANDIDATE_PHONE,
            "message-linkedin:0123456789abcdef": RowKind.MESSAGE_LINKEDIN,
            "parent-worth:11111111-2222-3333-4444-555555555555": RowKind.PARENT,
            "11111111-2222-3333-4444-555555555555": RowKind.PERSON_UUID,
            # 36 chars with 4 hyphens but not hex-shaped stays a pub
            "jordan-bravo-the-third-of-exampleton": RowKind.PUB,
        }
        for key, want in cases.items():
            self.assertIs(classify_review_key(key), want, key)


class SourceEnumPinTests(unittest.TestCase):
    def test_every_writer_stamp_is_a_member(self):
        # The full stamp vocabulary measured from real stores + every literal
        # a writer module stamps. A new writer must extend ReviewSource.
        self.assertEqual(
            {member.value for member in ReviewSource},
            {
                "deep-context-review", "user-guidance", "deep-context-reconcile",
                "deep-research", "deep-context-synthesis", "deep-context-parent-worth",
                "deep-context-heal", "deep-context-name-match",
                "dossier-self-reported", "legacy-sibling-settle", "legacy-migration",
            },
        )
        self.assertEqual(HEAL_DETACH_SOURCE, "deep-context-heal")
        self.assertEqual(HUMAN_DECISION_SOURCES, {"deep-context-review", "user-guidance"})


class ReviewDbRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.review_csv = self.dir / "review.csv"
        self.synthetic_csv = self.dir / "synthetic-people.csv"

    def fixture_rows(self) -> dict[str, dict[str, str]]:
        return {
            "jordan-bravo-1": blank_row(
                public_identifier="jordan-bravo-1",
                action="verify", approved="auto",
                linkedin_url="https://www.linkedin.com/in/jordan-bravo-1",
                confidence="0.910", reason="strong match:\nname + employer line up",
                person_id="11111111-2222-3333-4444-555555555555",
                source="deep-context-reconcile", updated_at="2026-08-01T00:00:00Z",
                llm_worth="yes", llm_worth_reason="active founder",
            ),
            "candidate:email:casey@example.com": blank_row(
                public_identifier="candidate:email:casey@example.com",
                action="retarget", new_linkedin_url="https://www.linkedin.com/in/casey-example",
                new_public_identifier="casey-example",
                person_id="candidate:email:casey@example.com",
                source="deep-research", updated_at="2026-08-02T00:00:00Z",
                llm_reject="yes", llm_reject_confidence="0.990",
                llm_reject_reason="different named person",
                llm_judge_fingerprint="abc123", llm_worth="maybe",
            ),
            "message-linkedin:0123456789abcdef": blank_row(
                public_identifier="message-linkedin:0123456789abcdef",
                action="retarget", approved="yes",
                new_public_identifier="jordan-bravo-1",
                source="user-guidance", updated_at="2026-08-03T00:00:00Z",
            ),
            "11111111-2222-3333-4444-555555555555": blank_row(
                public_identifier="11111111-2222-3333-4444-555555555555",
                person_id="11111111-2222-3333-4444-555555555555",
                source="deep-context-synthesis", llm_worth="yes",
                llm_worth_reason="mirrored", updated_at="2026-08-01T00:00:00Z",
            ),
            "parent-worth:99999999-8888-7777-6666-555555555555": blank_row(
                public_identifier="parent-worth:99999999-8888-7777-6666-555555555555",
                worth_person_ids="11111111-2222-3333-4444-555555555555|candidate:email:casey@example.com",
                llm_worth="yes", llm_worth_reason="aggregated",
                network_worth="no", user_worth_note="not my network",
                source="deep-context-parent-worth", updated_at="2026-08-04T00:00:00Z",
            ),
        }

    def write_synthetic(self, approved: str = "yes") -> None:
        with self.synthetic_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["id", "public_identifier", "full_name", "approved"])
            writer.writeheader()
            writer.writerow({"id": "s1", "public_identifier": "synth-jordan-bravo", "full_name": "Jordan Bravo", "approved": approved})
            writer.writerow({"id": "s2", "public_identifier": "synth-casey", "full_name": "Casey Example", "approved": ""})

    def test_live_vocabulary_from_snapshot_sweep_round_trips(self):
        # The 2026-08-05 all-snapshots sweep caught two LIVE values the
        # two-store census missed: action='review' (name-match proposals)
        # and synthetic approved='auto' (completeness gate). Both must
        # import and round-trip byte-identically.
        rows = self.fixture_rows()
        rows["jordan-bravo-2"] = blank_row(
            public_identifier="jordan-bravo-2", action="review",
            linkedin_url="https://www.linkedin.com/in/jordan-bravo-2",
            new_linkedin_url="https://www.linkedin.com/in/jordan-bravo-2",
            new_public_identifier="jordan-bravo-2", confidence="0.970",
            reason="unique first-degree name match",
            person_id="candidate:email:casey@example.com",
            source="deep-context-name-match", updated_at="2026-07-28T00:00:00Z")
        write_fixture(self.review_csv, rows)
        self.write_synthetic(approved="auto")
        before = self.review_csv.read_bytes()
        syn_before = self.synthetic_csv.read_bytes()
        db = ReviewDb(self.dir / "review.sqlite")
        db.import_stores(self.review_csv, self.synthetic_csv)
        db.export_review_csv(self.dir / "export.csv")
        db.export_synthetic_gates(self.synthetic_csv)
        self.assertEqual((self.dir / "export.csv").read_bytes(), before)
        self.assertEqual(self.synthetic_csv.read_bytes(), syn_before)
        gate = db.query(
            "SELECT value, approved FROM decisions WHERE kind='synthetic_gate' AND target='synth-jordan-bravo'")[0]
        self.assertEqual((gate["value"], gate["approved"]), ("yes", "auto"))

    def test_schema_version_mismatch_rebuilds(self):
        write_fixture(self.review_csv, self.fixture_rows())
        db = ReviewDb(self.dir / "review.sqlite")
        db.import_stores(self.review_csv)
        with db.connect() as conn:
            conn.execute(
                "UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        db2 = ReviewDb(self.dir / "review.sqlite")
        self.assertEqual(db2.query("SELECT COUNT(*) AS n FROM links")[0]["n"], 0)
        self.assertTrue(db2.needs_import(self.review_csv))

    def test_round_trip_is_byte_identical(self):
        write_fixture(self.review_csv, self.fixture_rows())
        self.write_synthetic()
        before = self.review_csv.read_bytes()
        syn_before = self.synthetic_csv.read_bytes()
        db = ReviewDb(self.dir / "review.sqlite")
        stats = db.import_stores(self.review_csv, self.synthetic_csv)
        db.export_review_csv(self.dir / "export.csv")
        db.export_synthetic_gates(self.synthetic_csv)
        self.assertEqual((self.dir / "export.csv").read_bytes(), before)
        self.assertEqual(self.synthetic_csv.read_bytes(), syn_before)
        self.assertEqual(stats, {"links": 4, "parents": 1, "decisions": 3, "synthetic_gates": 1})

    def test_decisions_derived_and_absence_means_pending(self):
        write_fixture(self.review_csv, self.fixture_rows())
        db = ReviewDb(self.dir / "review.sqlite")
        db.import_stores(self.review_csv)
        decided = {
            (row["kind"], row["target"]): row
            for row in db.query("SELECT * FROM decisions")
        }
        self.assertEqual(decided[("identity", "jordan-bravo-1")]["approved"], "auto")
        self.assertEqual(decided[("identity", "message-linkedin:0123456789abcdef")]["value"], "retarget")
        worth = decided[("worth", "99999999-8888-7777-6666-555555555555")]
        self.assertEqual((worth["value"], worth["note"]), ("no", "not my network"))
        # the un-approved research candidate is pending: no decision row at all
        self.assertNotIn(("identity", "candidate:email:casey@example.com"), decided)

    def test_import_refuses_unrepresentable_rows(self):
        rows = self.fixture_rows()
        rows["jordan-bravo-1"]["source"] = "totally-new-writer"
        rows["candidate:email:casey@example.com"]["approved"] = "yes"
        rows["candidate:email:casey@example.com"]["action"] = ""
        rows["11111111-2222-3333-4444-555555555555"]["worth_person_ids"] = "a|b"
        rows["parent-worth:99999999-8888-7777-6666-555555555555"]["linkedin_url"] = "https://example.com"
        write_fixture(self.review_csv, rows)
        db = ReviewDb(self.dir / "review.sqlite")
        with self.assertRaises(ReviewDbImportError) as ctx:
            db.import_stores(self.review_csv)
        message = str(ctx.exception)
        for fragment in ("unknown source", "approved without an action",
                         "worth_person_ids outside", "identity cells"):
            self.assertIn(fragment, message)

    def test_schema_checks_hold_without_python_validation(self):
        db = ReviewDb(self.dir / "review.sqlite")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO decisions (kind, target, value, approved) VALUES ('identity', 'x', 'nonsense', 'auto')"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO links (row_key, public_identifier, kind) VALUES ('candidate:email:x', 'candidate:email:x', 'pub')"
                )

    def test_dataclasses_match_schema_columns(self):
        # The row dataclasses are the one home for each table's writable
        # columns; a drift from the DDL is caught here, not at 2am.
        db = ReviewDb(self.dir / "review.sqlite")
        for table, row_type in (("links", LinkRow), ("parents", ParentRow), ("decisions", DecisionRow)):
                info = db.query(f"PRAGMA table_info({table})")
                writable = {r["name"] for r in info} - {"confidence_num", "llm_reject_confidence_num"}
                self.assertEqual({f.name for f in fields(row_type)}, writable, table)

    def test_generated_confidence_num(self):
        write_fixture(self.review_csv, self.fixture_rows())
        db = ReviewDb(self.dir / "review.sqlite")
        db.import_stores(self.review_csv)
        row = db.query(
            "SELECT confidence_num FROM links WHERE row_key = 'jordan-bravo-1'"
        )[0]
        self.assertAlmostEqual(row["confidence_num"], 0.91)

    def test_needs_import_tracks_csv_stat(self):
        write_fixture(self.review_csv, self.fixture_rows())
        db = ReviewDb(self.dir / "review.sqlite")
        self.assertTrue(db.needs_import(self.review_csv))
        db.import_stores(self.review_csv)
        self.assertFalse(db.needs_import(self.review_csv))
        rows = load_override_rows(self.review_csv)
        rows["jordan-bravo-1"]["reason"] = "changed"
        write_override_rows(self.review_csv, rows)
        self.assertTrue(db.needs_import(self.review_csv))

    def test_synthetic_gate_export_touches_only_approved(self):
        write_fixture(self.review_csv, self.fixture_rows())
        self.write_synthetic(approved="yes")
        db = ReviewDb(self.dir / "review.sqlite")
        db.import_stores(self.review_csv, self.synthetic_csv)
        with db.connect() as conn:
            conn.execute(
                "UPDATE decisions SET value = 'no' WHERE kind = ? AND target = 'synth-jordan-bravo'",
                (DecisionKind.SYNTHETIC_GATE.value,),
            )
        changed = db.export_synthetic_gates(self.synthetic_csv)
        self.assertEqual(changed, 1)
        with self.synthetic_csv.open(newline="", encoding="utf-8") as fh:
            rows = {row["public_identifier"]: row for row in csv.DictReader(fh)}
        self.assertEqual(rows["synth-jordan-bravo"]["approved"], "no")
        self.assertEqual(rows["synth-jordan-bravo"]["full_name"], "Jordan Bravo")
        self.assertEqual(rows["synth-casey"]["approved"], "")


class ApplyRowsCommitDoorTests(ReviewDbRoundTripTests):
    """Phase-2 commit door: the transaction is durability, the CSV an export."""

    def _seeded_db(self) -> ReviewDb:
        write_fixture(self.review_csv, self.fixture_rows())
        db = ReviewDb(self.dir / "review.sqlite")
        db.import_stores(self.review_csv)
        return db

    def test_apply_rows_commits_and_exports(self):
        db = self._seeded_db()
        rows = load_override_rows(self.review_csv)
        rows["jordan-bravo-1"].update(
            {"approved": "yes", "source": "deep-context-review",
             "updated_at": "2026-08-05T00:00:00Z"})
        db.apply_rows(rows, self.review_csv)
        decided = db.query(
            "SELECT approved, source FROM decisions WHERE kind='identity' AND target='jordan-bravo-1'")
        self.assertEqual((decided[0]["approved"], decided[0]["source"]),
                         ("yes", "deep-context-review"))
        exported = load_override_rows(self.review_csv)
        self.assertEqual(exported["jordan-bravo-1"]["approved"], "yes")
        self.assertFalse(db.needs_import(self.review_csv))
        self.assertEqual(db.query(
            "SELECT value FROM meta WHERE key='pending_export'")[0]["value"], "0")

    def test_apply_rows_refuses_bad_state_atomically(self):
        db = self._seeded_db()
        before = db.query("SELECT COUNT(*) AS n FROM decisions")[0]["n"]
        csv_before = self.review_csv.read_bytes()
        rows = load_override_rows(self.review_csv)
        rows["jordan-bravo-1"]["source"] = "not-a-writer"
        with self.assertRaises(ReviewDbImportError):
            db.apply_rows(rows, self.review_csv)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM decisions")[0]["n"], before)
        self.assertEqual(self.review_csv.read_bytes(), csv_before)

    def test_recover_pending_export_finishes_the_flush(self):
        db = self._seeded_db()
        rows = load_override_rows(self.review_csv)
        rows["jordan-bravo-1"].update(
            {"approved": "yes", "source": "deep-context-review"})
        # simulate the crash window: commit happened, export did not
        links, parents, decisions, _ = db._derive_tables(rows, None)
        with db.connect() as conn:
            db._replace_tables(conn, links, parents, decisions)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('pending_export', '1')")
        self.assertNotEqual(
            load_override_rows(self.review_csv)["jordan-bravo-1"]["approved"], "yes")
        self.assertTrue(db.recover_pending_export(self.review_csv))
        self.assertEqual(
            load_override_rows(self.review_csv)["jordan-bravo-1"]["approved"], "yes")
        self.assertFalse(db.recover_pending_export(self.review_csv))

    def test_recovery_refuses_when_csv_also_changed(self):
        db = self._seeded_db()
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('pending_export', '1')")
        rows = load_override_rows(self.review_csv)
        rows["jordan-bravo-1"]["reason"] = "raced the recovery window"
        write_override_rows(self.review_csv, rows)
        with self.assertRaises(ReviewDbImportError):
            db.recover_pending_export(self.review_csv)


if __name__ == "__main__":
    unittest.main()
