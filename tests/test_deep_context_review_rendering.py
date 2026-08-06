"""Focused tests for the non-paginated worth decision table."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.review_web.rendering import (
    render_decision_table,
)


class ReviewRenderingTest(unittest.TestCase):
    def test_decision_table_renders_every_matching_row_without_truncation(self) -> None:
        parents = [
            {
                "name": f"Jordan {index:02d}",
                "slug": f"jordan-{index:02d}",
                "worth_row": {
                    "key": f"parent-worth:{index:02d}",
                    "effective": "yes",
                },
            }
            for index in range(45)
        ]
        parents.append({
            "name": "Excluded No",
            "slug": "excluded-no",
            "worth_row": {"key": "parent-worth:no", "effective": "no"},
        })

        rendered = render_decision_table(list(reversed(parents)), "yes")

        self.assertEqual(rendered.count("class='decision-row'"), 45)
        self.assertNotIn("Excluded No", rendered)
        self.assertLess(rendered.index("Jordan 00"), rendered.index("Jordan 44"))


if __name__ == "__main__":
    unittest.main()
