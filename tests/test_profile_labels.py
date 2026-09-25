"""Persisted JEV labels reach every review profile surface."""

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
from packs.ingestion.primitives.deep_context.review import rendering
from deep_context_sqlite_test_helpers import seed_identity


def _labels(**values: float | str) -> tuple[tuple[str, float | str], ...]:
    return tuple(sorted(values.items()))


def _parent(
    name: str = "Jordan Bravo",
    *,
    labels: tuple[tuple[str, float | str], ...] = (),
    machine_reason: str = "",
) -> ParentViewRow:
    machine = WorthMachineRow("yes", machine_reason, "llm")
    worth = WorthRow("parent-worth:jordan", "jordan", "jordan", (), name, machine, None, "yes", "llm")
    return ParentViewRow(
        "parent-jordan", "jordan", "", "", name, ("person-a",), (), (),
        worth, WorthSummary("yes", "llm"), machine, (), labels,
    )


class LabelBadgeTests(unittest.TestCase):
    def test_badges_limit_three_and_reveal_remaining_on_focus(self) -> None:
        parent = _parent(labels=_labels(
            is_founder=0.9, is_professional=0.9, is_investor=0.86, is_classmate=0.85,
        ))
        markup = rendering._label_badges(parent)
        self.assertEqual(markup.count("class='person-label'"), 3)
        self.assertIn("tabindex=", markup)
        self.assertIn("role=", markup)
        self.assertIn("Work-related", markup)
        self.assertIn(">+1<", markup)
        self.assertEqual(rendering._label_badges(_parent()), "")

    def test_badges_hide_scores_below_85_percent(self) -> None:
        parent = _parent(labels=_labels(
            relationship_kind="unknown", relationship_kind_p=0.95,
            evidence_incomplete=0.88, real_relationship=0.35,
            is_personal=0.24, is_founder=0.04,
        ))
        markup = rendering._label_badges(parent)
        visible = markup.split("class='person-label-more'")[0]
        self.assertEqual(visible.count("class='person-label'"), 1)
        self.assertIn("Limited context", visible)
        self.assertNotIn("%", visible)
        for hidden in ("Direct contact", "Personal", "Founder", "Unknown"):
            self.assertNotIn(hidden, markup)
        self.assertNotIn("class='person-label-more'", markup)

    def test_badges_escape_label_values(self) -> None:
        parent = _parent(labels=_labels(
            relationship_kind="<script>alert(1)</script>", relationship_kind_p=0.95,
        ))
        self.assertNotIn("<script>", rendering._label_badges(parent))

    def test_the_number_of_labels_is_capped_on_every_profile_surface(self) -> None:
        parent = _parent(labels=_labels(is_founder=0.9, is_professional=0.9))
        for markup in (rendering.render_worth_card(parent), rendering.render_person_detail(parent)):
            self.assertIn("class='person-label'", markup)
            self.assertLess(markup.index("<h2>"), markup.index("class='person-label'"))
        rows = rendering.decision_rows_html([parent], "yes")
        self.assertIn("person-name-line", rows)
        self.assertIn("decision-expanded-profile", rows)
        self.assertIn("class='person-label'", rows)

    def test_yes_no_rows_omit_percentages_but_keep_the_machine_reason(self) -> None:
        parent = _parent(labels=_labels(is_founder=0.9), machine_reason="Limited relationship evidence")
        rows = rendering.decision_rows_html([parent], "yes")
        self.assertIn("Founder", rows)
        self.assertNotIn("90%", rows)
        summary = rows.split("</summary>")[0]
        self.assertNotIn("Limited relationship evidence", summary)
        self.assertIn("Limited relationship evidence", rows)


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
