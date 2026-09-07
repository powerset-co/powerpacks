import unittest

from packs.search.primitives.deep_search import pond_prompts


def _pond_1_prompts() -> dict[str, str]:
    return {family: pond_prompts.load_pond_prompt({"pond_prompt_family": family}, "pond-1")
            for family in sorted(pond_prompts.POND_PROMPT_FAMILIES)}


class PondPromptTests(unittest.TestCase):
    def test_pond_1_never_derives_experience_from_the_customer_industry(self) -> None:
        for family, prompt in _pond_1_prompts().items():
            with self.subTest(family=family):
                self.assertNotIn("customer industry or product category", prompt)
                self.assertNotIn("use that vertical as X", prompt)

    def test_every_pond_1_takes_experience_only_from_candidate_language_or_the_work(self) -> None:
        for family, prompt in _pond_1_prompts().items():
            with self.subTest(family=family):
                self.assertIn("candidate-background language", prompt)
                self.assertIn("recurring work", prompt)
                self.assertIn(
                    "What the company builds or sells, whom it serves, its industry, "
                    "and its company overview are never X.", prompt)
                for leak in ("customer industry", "product category", "product context",
                             "what customers buy"):
                    self.assertNotIn(leak, prompt)
                self.assertIn(
                    "A role or title people hold, such as founder, co-founder, CEO, or "
                    "manager, is never X", prompt)

    def test_every_family_uses_the_same_short_traits_prompt(self) -> None:
        general = pond_prompts.load_pond_prompt({"pond_prompt_family": "general"}, "traits")
        for family in sorted(pond_prompts.POND_PROMPT_FAMILIES):
            prompt = pond_prompts.load_pond_prompt({"pond_prompt_family": family}, "traits")
            with self.subTest(family=family):
                self.assertEqual(prompt, general)
                self.assertIn("candidate", prompt.casefold())
                self.assertIn("evidence_quote", prompt)
                self.assertIn('"kind":"capability|background|tool"', prompt)
                for bucket in ("must_have", "nice_to_have", "core_groups", '"tier"'):
                    self.assertNotIn(bucket, prompt)
                self.assertLessEqual(len(prompt.splitlines()), 40)

    def test_shared_traits_prompt_preserves_requirements_without_literalizing_them(self) -> None:
        prompt = " ".join(pond_prompts.load_pond_prompt(
            {"pond_prompt_family": "general"}, "traits",
        ).split())
        self.assertTrue(prompt.startswith("You are a recruiter reviewing candidates from a broad search pond."))
        self.assertIn("A broad occupation covers its routine working skills, not every specialty", prompt)
        self.assertIn("a strong candidate may match a subset", prompt)
        self.assertIn("merge broad and narrow descriptions of the same experience", prompt)
        self.assertIn("A requirement already represented by a retained qualifier needs no additional trait", prompt)
        self.assertIn("Do not create redundant technology or language traits", prompt)
        self.assertIn("remove overlapping mentions from other trait labels", prompt)
        self.assertIn("Keep explicit background qualifications", prompt)
        self.assertIn("Preserve accepted alternatives", prompt)
        self.assertIn("work authorization", prompt)
        self.assertIn("positive examples of useful groupings, not required outputs or exclusion rules", prompt)
        self.assertIn("The current JD and pond determine which qualifiers belong", prompt)
        self.assertIn("Name experience as a recruiter would say it aloud", prompt)
        self.assertIn("a short label, usually 2-5 words", prompt)
        self.assertIn("routine tool names in the evidence and reason, not the label", prompt)
        self.assertIn("Simplify the label without broadening or changing the qualification", prompt)
        for example in ("AI-powered lifecycle marketing", "Measuring campaign lift",
                        "Wet-lab and computational recruiting"):
            self.assertIn(example, prompt)
        self.assertIn("exact contiguous evidence_quote", prompt)
        self.assertIn("at most six traits", prompt)
        self.assertIn("An empty list is valid when the pond covers it", prompt)
        self.assertIn("selection_reason", prompt)
        for baked in ("AgentMail", "Braintrust", "Firecrawl", "Icarus", "Latch",
                      "Lovable", "Maybern", "Modal", "Pylon", "pushing LLMs",
                      "coding as teenagers", "database of humanity", "OpenRouter"):
            self.assertNotIn(baked, prompt)
