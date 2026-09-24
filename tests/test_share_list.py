from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.share.labels import ACTIVE_P, share_decision
from packs.ingestion.primitives.share.models import LABEL_COLUMNS, HumanTags, LabelRow
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.tags import TagStore
from packs.shared.csv_io import CsvIO

PEOPLE_HEADER = ["id", "public_identifier", "full_name", "superseded_person_ids"]


def _label(**overrides) -> LabelRow:
    fields = {
        "person_id": "person-a",
        "public_identifier": "jordan-bravo",
        "is_owner": False,
        "private_suggested": False,
        "probabilities": {},
    }
    fields.update(overrides)
    return LabelRow(**fields)


def _tags(*names: str) -> HumanTags:
    return HumanTags(person_id="person-a", tags=frozenset(names), note=None, updated_at="2026-09-24T00:00:00Z")


class ShareDecisionTests(unittest.TestCase):
    def test_every_rule_in_the_table_is_reachable(self) -> None:
        cases = [
            (_label(is_owner=True), _tags("share"), "no", "owner"),
            (_label(), _tags("private"), "no", "human_private"),
            (_label(private_suggested=True), _tags("share"), "yes", "human_share"),
            (_label(private_suggested=True), None, "no", "private_suggested"),
            (_label(probabilities={"is_automated_sender": ACTIVE_P}), None, "no", "automated_sender"),
            (_label(probabilities={"is_stranger": ACTIVE_P}), None, "no", "stranger"),
            (_label(), None, "yes", "default"),
        ]
        for label, tags, expected_share, expected_reason in cases:
            row = share_decision(label, tags, updated_at="2026-09-24T00:00:00Z")
            self.assertEqual((row.to_csv_row()["share"], row.reason), (expected_share, expected_reason), expected_reason)

    def test_a_human_private_tag_beats_a_human_share_tag(self) -> None:
        self.assertEqual(share_decision(_label(), _tags("private", "share"), updated_at="2026-09-24T00:00:00Z").reason, "human_private")

    def test_the_source_column_says_who_decided(self) -> None:
        self.assertEqual(share_decision(_label(), _tags("private"), updated_at="2026-09-24T00:00:00Z").source, "human")
        self.assertEqual(share_decision(_label(), None, updated_at="2026-09-24T00:00:00Z").source, "machine")

    def test_active_labels_carry_the_high_probability_names_and_the_suggestion(self) -> None:
        label = _label(
            private_suggested=True,
            probabilities={"is_family": 0.95, "is_professional": 0.1, "is_personal": ACTIVE_P},
        )
        self.assertEqual(share_decision(label, None, updated_at="2026-09-24T00:00:00Z").labels, ("is_family", "is_personal", "private_suggested"))

    def test_a_linkedin_only_row_defaults_to_yes_with_no_labels(self) -> None:
        row = share_decision(_label(), None, updated_at="2026-09-24T00:00:00Z")
        self.assertEqual((row.share, row.reason, row.labels), (True, "default", ()))


class ShareListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "share"
        self.people_csv = self.root / "people.csv"
        CsvIO.write_dict_rows(
            self.people_csv,
            PEOPLE_HEADER,
            [
                {"id": "person-a", "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo"},
                {
                    "id": "person-b",
                    "public_identifier": "casey-delta",
                    "full_name": "Casey Delta",
                    "superseded_person_ids": '["candidate:email:casey@example.com"]',
                },
            ],
        )
        CsvIO.write_dict_rows(
            self.out / "labels.csv",
            list(LABEL_COLUMNS),
            [
                {"person_id": "person-a", "public_identifier": "jordan-bravo", "is_automated_sender": "0.100"},
                {"person_id": "person-b", "public_identifier": "casey-delta", "is_automated_sender": "0.900"},
            ],
        )

    def _run(self) -> dict:
        return ShareList(out_dir=self.out, people_csv=self.people_csv).run()

    def test_share_csv_follows_people_csv_order_and_counts_by_reason(self) -> None:
        payload = self._run()
        rows = CsvIO.read_dict_rows_normalized(self.out / "share.csv")
        self.assertEqual([row["person_id"] for row in rows], ["person-a", "person-b"])
        self.assertEqual([row["reason"] for row in rows], ["default", "automated_sender"])
        self.assertEqual(payload["counts"]["share_yes"], 1)
        self.assertEqual(payload["counts"]["share_no"], 1)
        self.assertEqual(payload["counts"]["by_reason"], {"default": 1, "automated_sender": 1})

    def test_a_partial_label_set_is_refused_not_shared(self) -> None:
        CsvIO.write_dict_rows(
            self.out / "labels.csv",
            list(LABEL_COLUMNS),
            [{"person_id": "person-a", "public_identifier": "jordan-bravo", "is_automated_sender": "0.100"}],
        )
        payload = self._run()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("1 of 2 people have no label row", payload["error"])
        self.assertFalse((self.out / "share.csv").exists())

    def test_a_tag_on_a_superseded_id_decides_the_surviving_row(self) -> None:
        TagStore(self.out).apply(
            "candidate:email:casey@example.com", add={"share"}, remove=set(), note=None
        )
        self._run()
        rows = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "share.csv")}
        self.assertEqual(rows["person-b"]["share"], "yes")
        self.assertEqual(rows["person-b"]["reason"], "human_share")
        self.assertEqual(rows["person-b"]["source"], "human")

    def test_a_tag_on_the_surviving_id_wins_over_the_superseded_one(self) -> None:
        store = TagStore(self.out)
        store.apply("candidate:email:casey@example.com", add={"share"}, remove=set(), note=None)
        store.apply("person-b", add={"private"}, remove=set(), note=None)
        self._run()
        rows = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "share.csv")}
        self.assertEqual(rows["person-b"]["reason"], "human_private")

    def test_the_manifest_keeps_the_share_counts_beside_the_label_counts(self) -> None:
        (self.out / "manifest.json").write_text(json.dumps({"labels": {"counts": {"people": 2}}}), encoding="utf-8")
        self._run()
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["labels"]["counts"]["people"], 2)
        self.assertEqual(manifest["share"]["counts"]["share_yes"], 1)


if __name__ == "__main__":
    unittest.main()
