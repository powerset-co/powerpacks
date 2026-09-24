from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.share.labels import ACTIVE_P, share_decision
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.models import HumanTags, LabelRow
from packs.ingestion.primitives.share.questions import build_questions
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.tags import TagStore
from packs.ingestion.schemas.share_schema import ShareRow
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
            ("is_family", "is_personal", "automated_sender"),
        )

    def test_a_linkedin_only_worth_yes_row_shares_with_no_labels(self) -> None:
        row = share_decision(_label(), None, updated_at=UPDATED_AT)
        self.assertEqual((row.share, row.reason, row.labels), ("yes", "worth_yes", ()))


class ShareListTests(unittest.TestCase):
    """The node end to end on a synthetic install: three people with saved labels
    — worth yes, worth yes plus an automated-sender flag, worth maybe — and the
    flagged person's old candidate id superseded."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "share"
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
                    "superseded_person_ids": '["candidate:email:casey@example.com"]',
                },
                {"id": "person-c", "public_identifier": "riley-echo", "full_name": "Riley Echo"},
            ],
        )
        (self.root / "index.json").write_text(json.dumps({
            "slugs": {
                "jordan-bravo-aaaa": {"person_id": "person-a"},
                "casey-delta-bbbb": {"person_id": "person-b"},
                "riley-echo-cccc": {"person_id": "person-c"},
            },
            "parents": {
                "jordan-bravo-aaaa": {"parent_id": "parent-aaaa", "children": ["jordan-bravo-aaaa"]},
                "casey-delta-bbbb": {"parent_id": "parent-bbbb", "children": ["casey-delta-bbbb"]},
                "riley-echo-cccc": {"parent_id": "parent-cccc", "children": ["riley-echo-cccc"]},
            },
        }), encoding="utf-8")
        (self.root / "facts").mkdir()
        for parent_id, worth, automated in (
            ("parent-aaaa", "yes", 0.1),
            ("parent-bbbb", "yes", 0.9),
            ("parent-cccc", "maybe", 0.1),
        ):
            (self.root / "facts" / f"{parent_id}.jsonl").write_text(
                json.dumps({"facts": {
                    "network_worth": {"decision": worth},
                    "labels": _saved_labels(is_automated_sender=automated),
                }}) + "\n",
                encoding="utf-8",
            )
        for name in ("raw", "dossiers", "parents"):
            (self.root / name).mkdir()
        (self.root / "review.csv").write_text("public_identifier,network_worth,llm_worth\n", encoding="utf-8")
        self.evidence = ShareEvidence(
            people_csv=people_csv,
            index_json=self.root / "index.json",
            facts_dir=self.root / "facts",
            raw_dir=self.root / "raw",
            dossier_dir=self.root / "dossiers",
            parents_dir=self.root / "parents",
            overrides_csv=self.root / "review.csv",
        )

    def _run(self) -> dict:
        return ShareList(out_dir=self.out, evidence=self.evidence).run().to_payload()

    def test_share_csv_follows_people_csv_order_and_counts_by_reason(self) -> None:
        payload = self._run()
        rows = CsvIO.read_dict_rows_normalized(self.out / "share.csv")
        self.assertEqual([row["person_id"] for row in rows], ["person-a", "person-b", "person-c"])
        self.assertEqual([row["share"] for row in rows], ["yes", "confirm", "no"])
        self.assertEqual([row["reason"] for row in rows], ["worth_yes", "automated_sender", "worth_maybe"])
        self.assertEqual((payload["share_yes"], payload["share_no"], payload["confirm"]), (1, 1, 1))
        self.assertEqual(payload["by_reason"], {"worth_yes": 1, "automated_sender": 1, "worth_maybe": 1})

    def test_the_flag_that_fired_is_written_to_labels_csv(self) -> None:
        self._run()
        labels = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "labels.csv")}
        self.assertEqual(labels["person-b"]["flag"], "automated_sender")
        self.assertEqual(labels["person-a"]["flag"], "")
        self.assertEqual(labels["person-c"]["network_worth"], "maybe")

    def test_a_tag_on_a_superseded_id_decides_the_surviving_row(self) -> None:
        TagStore(self.out).apply(
            "candidate:email:casey@example.com", add={"share"}, remove=set(), note=None
        )
        self._run()
        rows = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "share.csv")}
        # The human's `share` beats the flag that would have asked them to confirm.
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

    def test_a_human_share_tag_lifts_a_worth_maybe_person(self) -> None:
        TagStore(self.out).apply("person-c", add={"share"}, remove=set(), note=None)
        self._run()
        rows = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "share.csv")}
        self.assertEqual((rows["person-c"]["share"], rows["person-c"]["reason"]), ("yes", "human_share"))

    def test_the_manifest_is_the_node_manifest(self) -> None:
        self._run()
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["people"], 3)
        self.assertIn("fingerprints", manifest)


class ShareSchemaTests(unittest.TestCase):
    def test_an_unknown_share_value_stops_the_parse(self) -> None:
        row = {"person_id": "person-a", "public_identifier": "jordan-bravo", "share": "yse",
               "reason": "worth_yes", "labels": "", "source": "machine", "updated_at": ""}
        with self.assertRaises(ValueError):
            ShareRow.from_csv_row(row)


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
