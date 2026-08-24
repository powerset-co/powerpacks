from __future__ import annotations

import unittest

from packs.search.primitives.turbopuffer import turbopuffer_resolve_education as resolve_education


class ResolveEducationTests(unittest.TestCase):
    def test_affiliated_school_queries_for_root_university(self) -> None:
        self.assertEqual(resolve_education.affiliated_school_queries("Stanford University"), ["stanford"])
        self.assertEqual(resolve_education.affiliated_school_queries("Harvard University"), ["harvard"])

    def test_affiliated_school_queries_keeps_specific_school_specific(self) -> None:
        self.assertEqual(resolve_education.affiliated_school_queries("Stanford Graduate School of Business"), [])
        self.assertEqual(resolve_education.affiliated_school_queries("University of Pennsylvania"), [])

    def test_affiliated_candidate_requires_same_leading_token(self) -> None:
        self.assertTrue(resolve_education.is_affiliated_candidate(["stanford"], "Stanford Graduate School of Business"))
        self.assertTrue(resolve_education.is_affiliated_candidate(["stanford"], "Stanford Continuing Studies"))
        self.assertFalse(resolve_education.is_affiliated_candidate(["stanford"], "Samford University"))


if __name__ == "__main__":
    unittest.main()
