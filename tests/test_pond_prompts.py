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

    def test_every_family_traits_prompt_shares_the_core_rules_and_fluff_list(self) -> None:
        general = pond_prompts.load_pond_prompt({"pond_prompt_family": "general"}, "traits")
        core = general.split("ANY FAMILY", 1)[0]
        self.assertIn("KINDS", core)
        self.assertIn("NEVER A TRAIT", core)
        self.assertIn("FLUFF", core)
        self.assertIn("No quote, no trait", core)
        self.assertIn("PROFILE TEST", core)
        self.assertIn("is this a technical capability the job needs", core)
        self.assertIn("admired, not required", core)
        self.assertIn("written as past", core)
        self.assertNotIn('"has founded a company"', core)
        # General rules only: no eval JD's own wording may be baked into the shared core.
        for baked in ("pushing LLMs", "coding as teenagers", "database of humanity",
                      "goes to root cause", "OpenRouter", "how people deliberate"):
            self.assertNotIn(baked, core)
        for family in sorted(pond_prompts.POND_PROMPT_FAMILIES):
            prompt = pond_prompts.load_pond_prompt({"pond_prompt_family": family}, "traits")
            with self.subTest(family=family):
                self.assertTrue(prompt.startswith(core))
                self.assertIn(
                    "If you cannot name the profile line that would prove it, it is not a trait.",
                    " ".join(prompt.split()),
                )
                self.assertIn('"kind":"capability|background|tool"', prompt)
                for bucket in ("must_have", "nice_to_have", "core_groups", '"tier"'):
                    self.assertNotIn(bucket, prompt)
                self.assertLessEqual(len(prompt.splitlines()), 110)
