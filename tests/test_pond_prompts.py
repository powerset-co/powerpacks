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

    def test_software_pond_1_takes_experience_only_from_candidate_language_or_the_work(self) -> None:
        for family in ("general", "engineering"):
            prompt = _pond_1_prompts()[family]
            with self.subTest(family=family):
                self.assertIn("candidate-background language", prompt)
                self.assertIn("recurring work", prompt)
                self.assertIn("never X", prompt)
                self.assertIn("plain occupation with no X", prompt)
