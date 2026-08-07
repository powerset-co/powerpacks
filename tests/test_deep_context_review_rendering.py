"""Focused tests for the non-paginated worth decision table."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.db.people_views import ParentViewRow
from packs.ingestion.primitives.deep_context.db.view_models import (
    WorthMachineRow,
    WorthRow,
    WorthSummary,
)
from packs.ingestion.primitives.deep_context.review.rendering import (
    render_decision_table,
)


class ReviewRenderingTest(unittest.TestCase):
    @staticmethod
    def parent(name: str, slug: str, effective: str) -> ParentViewRow:
        machine = WorthMachineRow(effective, "fixture", "llm")
        worth = WorthRow(
            f"parent-worth:{slug}", slug, slug, (), name, machine, None,
            effective, "llm",
        )
        return ParentViewRow(
            slug, slug, "", "", name, (), (), (), worth,
            WorthSummary(effective, "llm"), machine,
        )

    def test_decision_table_renders_every_matching_row_without_truncation(self) -> None:
        parents = [
            self.parent(f"Jordan {index:02d}", f"jordan-{index:02d}", "yes")
            for index in range(45)
        ]
        parents.append(self.parent("Excluded No", "excluded-no", "no"))

        rendered = render_decision_table(list(reversed(parents)), "yes")

        self.assertEqual(rendered.count("class='decision-row'"), 45)
        self.assertNotIn("Excluded No", rendered)
        self.assertLess(rendered.index("Jordan 00"), rendered.index("Jordan 44"))


if __name__ == "__main__":
    unittest.main()
