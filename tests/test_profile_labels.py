"""Which label badges a person shows, and that labels saved with the facts reach the view."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import (
    ParentViewRow,
    WorthMachineRow,
    WorthRow,
    WorthSummary,
)
from packs.ingestion.primitives.deep_context.review.label_titles import label_titles
from deep_context_sqlite_test_helpers import seed_identity


def _parent(**labels: float | str) -> ParentViewRow:
    machine = WorthMachineRow("yes", "", "llm")
    worth = WorthRow("parent-worth:jordan", "jordan", "jordan", (), "Jordan Bravo", machine, None, "yes", "llm")
    return ParentViewRow(
        "parent-jordan", "jordan", "", "", "Jordan Bravo", ("person-a",), (), (),
        worth, WorthSummary("yes", "llm"), machine, (), tuple(sorted(labels.items())),
    )


class LabelTitleTests(unittest.TestCase):
    def test_titles_are_the_labels_over_85_percent_strongest_first(self) -> None:
        parent = _parent(
            is_founder=0.99, is_investor=0.86, is_classmate=0.85, is_family=0.95, is_client=0.84, is_personal=0.24,
        )
        self.assertEqual(label_titles(parent), ("Founder", "Family", "Investor", "Classmate"))
        self.assertEqual(label_titles(_parent()), ())

    def test_relationship_kind_is_a_title_unless_unknown(self) -> None:
        known = _parent(relationship_kind="college_friend", relationship_kind_p=0.97, is_founder=0.9)
        self.assertEqual(label_titles(known), ("College friend", "Founder"))
        for hidden in (
            _parent(relationship_kind="unknown", relationship_kind_p=0.95),
            _parent(relationship_kind="college_friend", relationship_kind_p=0.5),
            _parent(relationship_kind="college_friend"),
        ):
            self.assertEqual(label_titles(hidden), ())

    def test_two_labels_with_one_title_show_once_at_the_stronger_score(self) -> None:
        # is_professional and work_signal are both "Work-related"; either may be the stronger.
        for scores in ({"is_professional": 0.5, "work_signal": 0.9}, {"is_professional": 0.9, "work_signal": 0.5}):
            with self.subTest(scores=scores):
                parent = _parent(**scores, is_founder=0.88, evidence_incomplete=0.87)
                self.assertEqual(label_titles(parent), ("Work-related", "Founder", "Limited context"))

    def test_titles_never_carry_the_score(self) -> None:
        self.assertEqual(label_titles(_parent(is_founder=0.9)), ("Founder",))


class PersistedLabelTests(unittest.TestCase):
    """The one seam that matters: labels saved with the facts reach the view."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")

    def test_labels_saved_with_the_facts_reach_the_profile_row(self) -> None:
        seed_identity(
            self.db,
            parent_id="parent-jordan",
            person_id="person-a",
            row_key="jordan-bravo",
            name="Jordan Bravo",
            machine_worth="yes",
            labels={"is_founder": 0.9, "relationship_kind": "colleague"},
        )
        parent = person_detail(self.db, "parent-jordan")
        assert parent is not None
        self.assertIn(("is_founder", 0.9), parent.labels)
        self.assertIn(("relationship_kind", "colleague"), parent.labels)


if __name__ == "__main__":
    unittest.main()
