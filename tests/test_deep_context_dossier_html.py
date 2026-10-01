"""A dossier's markdown as the HTML the Review and People pages show."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.review.dossier_html import markdown_to_html

DOSSIER = """---
type: person
---

# Jordan Bravo

## Contact
- jordan@example.com

## Summary

Product Manager at Example Corp. Their network worth: a lot.
{worth}
## Relationship & cadence

Former colleague; stays in touch.
<!-- parent-link: jordan-bravo -->
"""
SUMMARY = "<h4>Summary</h4>\n<p>Product Manager at Example Corp. Their network worth: a lot.</p>\n"
RELATIONSHIP = "<h4>Relationship &amp; cadence</h4>\n<p>Former colleague; stays in touch.</p>\n"
# The composer's line, with and without a reason, and as the page would draw it.
WORTH_LINES = {
    "**Network worth:** yes — strong technical network": (
        "<p><strong>Network worth:</strong> yes — strong technical network</p>\n"
    ),
    "**Network worth:** maybe": "<p><strong>Network worth:</strong> maybe</p>\n",
}


class DossierHtmlTests(unittest.TestCase):
    def test_a_review_card_shows_the_dossier_without_name_contact_or_worth(self) -> None:
        for line in WORTH_LINES:
            with self.subTest(line=line):
                dossier = DOSSIER.format(worth=f"\n{line}\n\n")
                # The Summary above the line and the section below it are whole, with
                # nothing left where the line was.
                self.assertEqual(markdown_to_html(dossier, for_review_card=True), SUMMARY + RELATIONSHIP)

        without_line = DOSSIER.format(worth="\n")
        self.assertEqual(markdown_to_html(without_line, for_review_card=True), SUMMARY + RELATIONSHIP)

    def test_the_whole_dossier_keeps_its_name_contact_and_worth(self) -> None:
        opening = "<h3>Jordan Bravo</h3>\n<h4>Contact</h4>\n<ul>\n<li>jordan@example.com</li>\n</ul>\n"
        for line, drawn in WORTH_LINES.items():
            with self.subTest(line=line):
                dossier = DOSSIER.format(worth=f"\n{line}\n\n")
                self.assertEqual(markdown_to_html(dossier), opening + SUMMARY + drawn + RELATIONSHIP)

    def test_a_worth_line_in_every_section_of_a_merged_dossier_is_left_out(self) -> None:
        dossier = "## Summary\n\nFirst.\n\n**Network worth:** yes — a\n\n## Also\n\nSecond.\n\n**Network worth:** no\n"
        self.assertEqual(
            markdown_to_html(dossier, for_review_card=True),
            "<h4>Summary</h4>\n<p>First.</p>\n<h4>Also</h4>\n<p>Second.</p>\n",
        )


if __name__ == "__main__":
    unittest.main()
