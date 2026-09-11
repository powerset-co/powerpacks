from __future__ import annotations

import hashlib
import unittest

from packs.indexing.lib.job_descriptions import clean_description, focused_description


class JobDescriptionFocusTest(unittest.TestCase):
    def test_no_plan_preserves_full_content_without_classifying_sections(self) -> None:
        source = (
            "<p>Benefits</p><p>Manage payroll and employee benefits.</p>"
            "<p>Free lunch &amp; dental insurance.</p>"
            "<p>Location: San Francisco.</p>"
            "<p>No degree required. Rust preferred, not required.</p>"
        )
        result = focused_description(source)
        self.assertEqual(result, clean_description(source))
        self.assertIn("Free lunch & dental insurance.", result)
        self.assertIn("No degree required. Rust preferred, not required.", result)

    def test_reviewed_plan_preserves_mixed_section_job_evidence(self) -> None:
        source = (
            "Benefits\nManage payroll and employee benefits.\nFree lunch.\n"
            "How to apply\nPlease apply online. English is our working language.\n"
            "Location: San Francisco.\nKnowledge of California employment law.\n"
            "Travel to field test sites. No degree required."
        )
        quotes = ["Free lunch.", "Please apply online. ", "Location: San Francisco.\n"]
        result = focused_description(source, removal_quotes=quotes,
            source_sha256=hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(result, (
            "Benefits\nManage payroll and employee benefits.\n\n"
            "How to apply\nEnglish is our working language.\n"
            "Knowledge of California employment law.\n"
            "Travel to field test sites. No degree required."
        ))

    def test_plan_is_bound_to_normalized_html_not_raw_markup(self) -> None:
        source = "<p>Build storage systems.</p><p>Free lunch.</p>"
        digest = hashlib.sha256(clean_description(source).encode()).hexdigest()
        self.assertEqual(focused_description(source, removal_quotes=["Free lunch."],
                                           source_sha256=digest), "Build storage systems.")


class SemanticRemovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = "A once-in-a-lifetime adventure.\nRequirements\nExperience building storage systems."
        self.digest = hashlib.sha256(focused_description(self.source).encode()).hexdigest()

    def test_reviewed_quote_removal_preserves_requirement(self) -> None:
        result = focused_description(self.source, source_sha256=self.digest,
                                     removal_quotes=["A once-in-a-lifetime adventure."])
        self.assertEqual(result, "Requirements\nExperience building storage systems.")
        self.assertEqual(focused_description(self.source, source_sha256=self.digest,
                                           removal_quotes=[]), self.source)

    def test_stale_or_missing_source_digest_rejected(self) -> None:
        for digest in (None, "old-source"):
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, "match normalized source"):
                focused_description(self.source, source_sha256=digest,
                                    removal_quotes=["A once-in-a-lifetime adventure."])

    def test_absent_empty_or_duplicate_quotes_rejected(self) -> None:
        for quotes in (["invented requirement"], [""], [" "], ["adventure", "adventure"], "adventure"):
            with self.subTest(quotes=quotes), self.assertRaises(ValueError):
                focused_description(self.source, source_sha256=self.digest, removal_quotes=quotes)

    def test_ambiguous_occurrence_rejected(self) -> None:
        source = "Repeated. Repeated.\nRequirements\nExperience building storage systems."
        digest = hashlib.sha256(focused_description(source).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            focused_description(source, source_sha256=digest, removal_quotes=["Repeated."])

    def test_self_overlapping_occurrences_are_ambiguous(self) -> None:
        source = "ABABA\nBuild firmware."
        with self.assertRaisesRegex(ValueError, "exactly one"):
            focused_description(source, removal_quotes=["ABA"],
                                source_sha256=hashlib.sha256(source.encode()).hexdigest())

    def test_long_source_plan_validated_before_output_length_limit(self) -> None:
        quote = "x" * 24000
        source = "Build firmware.\n" + quote
        self.assertEqual(focused_description(source), "")
        self.assertEqual(focused_description(source, removal_quotes=[quote],
            source_sha256=hashlib.sha256(source.encode()).hexdigest()), "Build firmware.")

    def test_overlapping_quotes_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlap"):
            focused_description(self.source, source_sha256=self.digest,
                                removal_quotes=["once-in-a-lifetime", "lifetime adventure"])

    def test_entire_description_cannot_be_removed(self) -> None:
        with self.assertRaisesRegex(ValueError, "entire description"):
            focused_description(self.source, source_sha256=self.digest, removal_quotes=[self.source])


if __name__ == "__main__":
    unittest.main()
