from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from deep_context_sqlite_test_helpers import connect, seed_identity
from packs.ingestion.primitives.deep_context.db.share_views import person_labels, share_decisions
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import ACTIVE_P, share_decision
from packs.ingestion.primitives.share.models import HumanTags, LabelRow
from packs.ingestion.primitives.share.questions import build_questions
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.store import TagStore
from packs.shared.csv_io import CsvIO

PEOPLE_HEADER = ["id", "public_identifier", "full_name", "source_channels", "interaction_counts", "superseded_person_ids"]
UPDATED_AT = "2026-09-24T00:00:00Z"


def _label(**overrides) -> LabelRow:
    fields = {
        "person_id": "person-a",
        "public_identifier": "jordan-bravo",
        "is_owner": False,
        "worth": "yes",
        "flag": None,
        "probabilities": {},
    }
    fields.update(overrides)
    return LabelRow(**fields)


def _tags(*names: str) -> HumanTags:
    return HumanTags(person_id="person-a", tags=frozenset(names), note=None, updated_at=UPDATED_AT)


class ShareDecisionTests(unittest.TestCase):
    def test_every_rule_in_the_table_is_reachable_in_order(self) -> None:
        cases = [
            (_label(is_owner=True, worth="no"), _tags("share"), "no", "owner"),
            (_label(), _tags("private"), "no", "human_private"),
            (_label(worth="no", flag="family"), _tags("share"), "yes", "human_share"),
            (_label(worth="no", flag="family"), None, "no", "worth_no"),
            (_label(worth="maybe", flag="family"), None, "no", "worth_maybe"),
            (_label(flag="family"), None, "confirm", "family"),
            (_label(), None, "yes", "worth_yes"),
        ]
        for label, tags, expected_share, expected_reason in cases:
            row = share_decision(label, tags, updated_at=UPDATED_AT)
            self.assertEqual((row.share, row.reason), (expected_share, expected_reason), expected_reason)

    def test_an_unjudged_person_is_not_shared(self) -> None:
        row = share_decision(_label(worth=""), None, updated_at=UPDATED_AT)
        self.assertEqual((row.share, row.reason), ("no", "worth_maybe"))

    def test_a_human_private_tag_beats_a_human_share_tag(self) -> None:
        self.assertEqual(share_decision(_label(), _tags("private", "share"), updated_at=UPDATED_AT).reason, "human_private")

    def test_the_source_column_says_who_decided(self) -> None:
        self.assertEqual(share_decision(_label(), _tags("private"), updated_at=UPDATED_AT).source, "human")
        self.assertEqual(share_decision(_label(), None, updated_at=UPDATED_AT).source, "machine")

    def test_active_labels_carry_the_high_probability_names_and_the_flag(self) -> None:
        label = _label(
            flag="automated_sender",
            probabilities={"is_family": 0.95, "is_professional": 0.1, "is_personal": ACTIVE_P},
        )
        self.assertEqual(
            share_decision(label, None, updated_at=UPDATED_AT).labels,
            "is_family|is_personal|automated_sender",
        )

    def test_a_linkedin_only_worth_yes_row_shares_with_no_labels(self) -> None:
        row = share_decision(_label(), None, updated_at=UPDATED_AT)
        self.assertEqual((row.share, row.reason, row.labels), ("yes", "worth_yes", ""))


class ShareListTests(unittest.TestCase):
    """The node end to end on a synthetic store: three people with saved labels
    — worth yes, worth yes plus an automated-sender flag, worth maybe — and the
    flagged person's old candidate id superseded."""

    SUPERSEDED = "candidate:email:casey@example.com"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "share"
        self.db = Db(self.root / "deep-context.sqlite")
        people_csv = self.root / "people.csv"
        CsvIO.write_dict_rows(
            people_csv,
            PEOPLE_HEADER,
            [
                {"id": "person-a", "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo"},
                {
                    "id": "person-b",
                    "public_identifier": "casey-delta",
                    "full_name": "Casey Delta",
                    "superseded_person_ids": json.dumps([self.SUPERSEDED]),
                },
                {"id": "person-c", "public_identifier": "riley-echo", "full_name": "Riley Echo"},
            ],
        )
        for parent_id, person_id, row_key, name, slug, worth, automated in (
            ("parent-aaaa", "person-a", "jordan-bravo-aaaa", "Jordan Bravo", "jordan-bravo", "yes", 0.1),
            ("parent-bbbb", "person-b", "casey-delta-bbbb", "Casey Delta", "casey-delta", "yes", 0.9),
            ("parent-cccc", "person-c", "riley-echo-cccc", "Riley Echo", "riley-echo", "maybe", 0.1),
        ):
            seed_identity(
                self.db,
                parent_id=parent_id,
                person_id=person_id,
                row_key=row_key,
                name=name,
                machine_worth=worth,
                public_identifier=slug,
                labels=_saved_labels(is_automated_sender=automated),
            )
        self.evidence = ShareEvidence(self.db, people_csv=people_csv)

    def _run(self) -> dict:
        # The canonical inputs are declared external artifacts; the explicit db
        # and evidence point at the temp store, so only the readability precheck
        # needs the real files to stand in.
        return ShareList(db=self.db, out_dir=self.out, evidence=self.evidence).run().to_payload()

    def test_share_table_follows_people_csv_order_and_counts_by_reason(self) -> None:
        payload = self._run()
        rows = share_decisions(self.db)
        self.assertEqual([row.person_id for row in rows], ["person-a", "person-b", "person-c"])
        self.assertEqual([row.share for row in rows], ["yes", "confirm", "no"])
        self.assertEqual([row.reason for row in rows], ["worth_yes", "automated_sender", "worth_maybe"])
        self.assertEqual((payload["share_yes"], payload["share_no"], payload["confirm"]), (1, 1, 1))
        self.assertEqual(payload["by_reason"], {"worth_yes": 1, "automated_sender": 1, "worth_maybe": 1})

    def test_the_flag_that_fired_is_written_to_person_labels(self) -> None:
        self._run()
        labels = {row.person_id: row for row in person_labels(self.db)}
        self.assertEqual(labels["person-b"].flag, "automated_sender")
        self.assertIsNone(labels["person-a"].flag)
        self.assertEqual(labels["person-c"].worth, "maybe")
        self.assertEqual(labels["person-a"].worth, "yes")
        self.assertEqual(json.loads(labels["person-b"].labels_json)["is_automated_sender"], 0.9)

    def test_a_tag_on_a_superseded_id_decides_the_surviving_row(self) -> None:
        TagStore(self.db).apply(self.SUPERSEDED, add={"share"}, remove=set(), note=None)
        self._run()
        rows = {row.person_id: row for row in share_decisions(self.db)}
        # The human's `share` beats the flag that would have asked them to confirm.
        self.assertEqual(rows["person-b"].share, "yes")
        self.assertEqual(rows["person-b"].reason, "human_share")
        self.assertEqual(rows["person-b"].source, "human")

    def test_a_tag_on_the_surviving_id_wins_over_the_superseded_one(self) -> None:
        store = TagStore(self.db)
        store.apply(self.SUPERSEDED, add={"share"}, remove=set(), note=None)
        store.apply("person-b", add={"private"}, remove=set(), note=None)
        self._run()
        rows = {row.person_id: row for row in share_decisions(self.db)}
        self.assertEqual(rows["person-b"].reason, "human_private")

    def test_a_human_share_tag_lifts_a_worth_maybe_person(self) -> None:
        TagStore(self.db).apply("person-c", add={"share"}, remove=set(), note=None)
        self._run()
        rows = {row.person_id: row for row in share_decisions(self.db)}
        self.assertEqual((rows["person-c"].share, rows["person-c"].reason), ("yes", "human_share"))

    def test_the_manifest_is_the_node_manifest(self) -> None:
        self._run()
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["people"], 3)
        self.assertIn("fingerprints", manifest)


class ShareSchemaTests(unittest.TestCase):
    def test_an_unknown_share_value_is_rejected_by_the_store_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "deep-context.sqlite")
            seed_identity(
                db,
                parent_id="parent-a",
                person_id="person-a",
                row_key="jordan-a",
                name="Jordan Bravo",
                machine_worth="yes",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                with connect(db) as connection:
                    connection.execute(
                        "INSERT INTO share (person_id, public_identifier, share, reason, labels, source, updated_at)"
                        " VALUES ('person-a', 'jordan-bravo', 'yse', 'worth_yes', '', 'machine', '')"
                    )


def _saved_labels(**probabilities: float) -> dict:
    """Labels as synthesize saves them: every question answered, the given nouls overridden."""
    saved: dict = {}
    for name, question in build_questions().items():
        if question["type"] == "noul":
            saved[name] = probabilities.get(name, 0.0)
        elif question["type"] == "score":
            saved[name] = 0
        else:
            saved[name] = "unknown"
            saved[f"{name}_p"] = 1.0
    return saved


if __name__ == "__main__":
    unittest.main()
