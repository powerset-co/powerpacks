from __future__ import annotations

import unittest

from packs.indexing.lib.artifacts import build_summary_records
from packs.search.tech_skills import extract, extract_query, normalize, normalize_many


class TechSkillsTest(unittest.TestCase):
    def test_normalize_exact_display_and_aliases(self) -> None:
        self.assertEqual(normalize("python"), "python")
        self.assertEqual(normalize("PYTHON"), "python")
        self.assertEqual(normalize("Amazon Web Services"), "aws")
        self.assertEqual(normalize(" Kubernetes "), "kubernetes")
        self.assertIsNone(normalize("leadership"))

    def test_normalize_many_handles_linkedin_objects(self) -> None:
        self.assertEqual(
            normalize_many([
                {"name": "JavaScript", "passedSkillAssessment": False},
                "javascript",
                {"name": "Haskell"},
                {"other": "ignored"},
                "communication",
            ]),
            ["haskell", "javascript"],
        )

    def test_extract_uses_longest_exact_matches(self) -> None:
        self.assertEqual(
            extract("Built machine learning systems with React Native and Amazon Web Services"),
            ["aws", "machine_learning", "react_native"],
        )

    def test_extract_haskell_and_punctuation_skills(self) -> None:
        self.assertEqual(
            extract("Haskell engineer using C++, C#, Node.js, and .NET"),
            ["c_plus_plus", "c_sharp", "dotnet", "haskell", "node_js"],
        )

    def test_extract_preserves_query_stopword_precision(self) -> None:
        self.assertEqual(extract("GTM leader in cloud"), [])
        self.assertNotIn("go", extract("GTM leader in software engineering"))
        self.assertEqual(extract("Go engineer"), ["go"])
        self.assertEqual(extract("backend engineers with go experience"), ["go"])
        self.assertNotIn("go", extract("PRs go through review and candidates go deeper"))
        self.assertNotIn("go", extract("partners with go-to-market teams"))

    def test_query_extraction_requires_explicit_skill_intent(self) -> None:
        self.assertEqual(extract_query("AI companies hiring database architects"), [])
        self.assertEqual(extract_query("people who worked at Stripe"), [])
        self.assertEqual(extract_query("backend engineers with Go experience"), ["go"])
        self.assertEqual(extract_query("people who know Haskell"), ["haskell"])

    def test_normalize_many_accepts_existing_stringified_linkedin_skills(self) -> None:
        self.assertEqual(
            normalize_many(["{'endorsementsCount': 7, 'name': 'JavaScript', 'passedSkillAssessment': False}"]),
            ["javascript"],
        )

    def test_ambiguous_language_names_require_programming_context(self) -> None:
        self.assertNotIn("c", extract("Raised a Series C round"))
        self.assertNotIn("r", extract("Partner closely with R&D"))
        self.assertNotIn("rails", extract("Build safety rails for deployment"))
        self.assertNotIn("less_css", extract("Ship in less time"))
        self.assertIn("c", extract("Experience programming in C"))
        self.assertIn("r", extract("Experience using R for statistics"))
        self.assertIn("ruby_on_rails", extract("Build services with Ruby on Rails"))
        self.assertIn("less_css", extract("Maintain LESS stylesheets"))

    def test_summary_records_use_the_same_canonical_ids(self) -> None:
        record = build_summary_records([{
            "id": "person-1",
            "summary": "Builds Haskell services",
            "tech_skills": [{"name": "Amazon Web Services"}, {"name": "k8s"}],
        }])["summaries"][0]
        self.assertEqual(record["tech_skills"], ["aws", "kubernetes"])


if __name__ == "__main__":
    unittest.main()
