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

    def test_decision_table_renders_given_page_and_offers_more(self) -> None:
        parents = [
            self.parent(f"Jordan {index:02d}", f"jordan-{index:02d}", "yes")
            for index in range(45)
        ]

        rendered = render_decision_table(list(reversed(parents)), "yes", total=100)

        self.assertEqual(rendered.count("class='decision-row'"), 45)
        self.assertLess(rendered.index("Jordan 00"), rendered.index("Jordan 44"))
        self.assertIn("data-table-more", rendered)
        self.assertIn("55 left", rendered)
        self.assertIn("data-offset='45'", rendered)

        last = render_decision_table(parents, "yes", total=45)
        self.assertNotIn("data-table-more", last)

    def test_contact_values_are_escaped_by_the_template_boundary(self) -> None:
        rendered = render_decision_table(
            [self.parent("<Jordan & Bravo>", "jordan", "yes")], "yes",
        )

        self.assertIn("&lt;Jordan &amp; Bravo&gt;", rendered)
        self.assertNotIn("<Jordan & Bravo>", rendered)


if __name__ == "__main__":
    unittest.main()
