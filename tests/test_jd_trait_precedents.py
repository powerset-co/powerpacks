from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from packs.search.primitives.deep_search import extract_jd_traits


class JdTraitPrecedentTests(unittest.TestCase):
    def test_traits_response_is_checkpointed_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jd = root / "jd.txt"
            raw = root / "traits.raw.json"
            jd.write_text("Synthetic job description", encoding="utf-8")
            client = mock.Mock()
            client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{malformed"))])

            with mock.patch.object(extract_jd_traits, "make_openai_client",
                                   return_value=client), \
                 self.assertRaises(json.JSONDecodeError):
                extract_jd_traits.extract_traits(
                    jd_file=jd,
                    brief={
                        "job_title": "Synthetic Role",
                        "normalized_archetype": "synthetic role",
                        "target_level": "senior_ic",
                        "pond_prompt_family": "general",
                    },
                    pond_traits=[{
                        "value": "Synthetic Role", "temporal": "current", "meaning": "role",
                    }],
                    model="test", api_key="test", raw_response_path=raw,
                )

            self.assertEqual(raw.read_text(encoding="utf-8"), "{malformed")


    def test_request_preserves_raw_pond_query_and_compiled_traits(self) -> None:
        pond_query = "Backend Engineers with distributed systems experience in Europe"
        pond_traits = [{"value": "Distributed systems", "meaning": "experience", "temporal": "all"}]
        request = extract_jd_traits.traits_request(
            jd="Build distributed systems and developer APIs.",
            brief={"job_title": "Backend Engineer", "normalized_archetype": "Backend Engineer",
                   "target_level": "senior_ic"},
            model="gpt-5.6-sol", system_prompt="Synthetic trait policy",
            pond_query=pond_query, pond_traits=pond_traits,
        )
        content = request["messages"][1]["content"]
        self.assertIn("Initial pond query:\n" + pond_query, content)
        self.assertIn(json.dumps(pond_traits, indent=2), content)

    def test_production_trait_cards_are_positive_examples(self) -> None:
        cards = extract_jd_traits.precedents._read(extract_jd_traits.precedents.SEED_PATH)["trait_cards"]
        self.assertTrue(cards)
        for card in cards:
            with self.subTest(card=card.get("job")):
                self.assertNotIn("excludes", card)
                self.assertNotRegex(json.dumps(card).casefold(), r"\b(?:do not|never|must not)\b")

    def test_traits_messages_retrieve_from_jd_and_brief_and_keep_pond_context(self) -> None:
        jd = "Build product onboarding and activation experiments."
        brief = {
            "job_title": "Growth Engineer",
            "normalized_archetype": "product software engineer",
            "target_level": "senior_ic",
        }
        cards = [{"job": "Growth Engineer", "lesson": "Distinguish product work from marketing."}]
        pond_traits = [{"trait": "production software engineering"}]
        with mock.patch.object(
            extract_jd_traits.precedents, "retrieve_jd_precedents", return_value=cards,
        ) as retrieve:
            messages = extract_jd_traits.build_traits_messages(
                jd, brief, "Synthetic trait policy", pond_traits,
            )

        retrieve.assert_called_once_with(jd, brief, collection="traits")
        self.assertEqual(messages[0]["content"], "Synthetic trait policy")
        content = messages[1]["content"]
        self.assertIn(jd, content)
        self.assertIn(json.dumps(cards, indent=2), content)
        self.assertIn(json.dumps(pond_traits, indent=2), content)
        self.assertIn("Return only additional qualifications beyond what these pond traits explicitly cover", content)
        self.assertIn("Use the JD to identify and group the experience that would distinguish better-fit candidates", content)
        self.assertIn("JD work matches", content)
        self.assertIn("not a checklist", content)
        self.assertIn("Do not import requirements", content)
        self.assertIn("actual JD evidence", content)

    def test_no_retrieved_cards_adds_no_precedent_context(self) -> None:
        brief = {
            "job_title": "Pastry Chef", "normalized_archetype": "pastry chef",
            "target_level": "senior_ic",
        }
        with mock.patch.object(
            extract_jd_traits.precedents, "retrieve_jd_precedents", return_value=[],
        ):
            content = extract_jd_traits.build_traits_messages(
                "Bake and decorate pastries.", brief, "Synthetic trait policy",
            )[1]["content"]

        self.assertEqual(content, (
            f"Role:\n{json.dumps(brief, indent=2)}\n\nJob description:\n\n"
            "Bake and decorate pastries."
        ))

    def test_existing_growth_pond_cards_do_not_enter_trait_prompts(self) -> None:
        jd = (
            "Growth Engineer. Write production frontend and full-stack code for signup, "
            "onboarding, activation, and conversion. Instrument experiments and own funnel outcomes."
        )
        brief = {
            "job_title": "Growth Engineer",
            "normalized_archetype": "product software engineer",
            "target_level": "senior_ic",
        }
        policy = {
            "move_cards": [{"job": brief["job_title"], "family": brief["normalized_archetype"],
                            "defining_capability": jd, "reason": "Synthetic pond-only lesson."}],
            "trait_cards": [], "taste_cards": [],
        }
        with mock.patch.object(extract_jd_traits.precedents, "_read", return_value=policy):
            self.assertTrue(extract_jd_traits.precedents.retrieve_jd_precedents(jd, brief, collection="pond"))
            for pond_traits in ([], [{"trait": "paid acquisition campaign management"}]):
                with self.subTest(pond_traits=pond_traits):
                    content = extract_jd_traits.build_traits_messages(
                        jd, brief, "Synthetic trait policy", pond_traits,
                    )[1]["content"]
                    self.assertNotIn("Synthetic pond-only lesson.", content)
                    self.assertNotIn("Retrieved JD precedents:", content)

    def test_trait_collection_is_independent_of_pond_and_taste_lessons_and_pond_traits(self) -> None:
        jd = "Write production frontend code for onboarding and activation experiments."
        brief = {
            "job_title": "Synthetic Growth Engineer",
            "normalized_archetype": "product software engineer",
            "target_level": "senior_ic",
        }
        signature = {
            "job": brief["job_title"], "family": brief["normalized_archetype"],
            "defining_capability": jd, "excludes": "Bake cakes and pastries.",
        }
        policy = {
            "move_cards": [{**signature, "reason": "Synthetic pond lesson."}],
            "trait_cards": [{**signature, "reason": "Synthetic trait lesson."}],
            "taste_cards": [{**signature, "dimension": "role_fit",
                             "reason": "Synthetic taste lesson."}],
        }
        contexts = []
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "precedents.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.object(extract_jd_traits.precedents, "SEED_PATH", path):
                for pond_traits in ([], [{"trait": "paid acquisition campaign management"}]):
                    content = extract_jd_traits.build_traits_messages(
                        jd, brief, "Synthetic trait policy", pond_traits,
                    )[1]["content"]
                    self.assertIn("Synthetic trait lesson.", content)
                    self.assertNotIn("Synthetic pond lesson.", content)
                    self.assertNotIn("Synthetic taste lesson.", content)
                    contexts.append(content.split("\n\nPond traits already scored:")[0])

        self.assertEqual(contexts[0], contexts[1])

    def test_unrelated_jd_does_not_retrieve_from_pond_traits(self) -> None:
        content = extract_jd_traits.build_traits_messages(
            "Pastry Chef. Bake sourdough bread and decorate wedding cakes.",
            {"job_title": "Pastry Chef", "normalized_archetype": "pastry chef",
             "target_level": "senior_ic"},
            "Synthetic trait policy",
            [{"trait": "product growth engineering onboarding activation conversion"}],
        )[1]["content"]

        self.assertNotIn("Retrieved JD precedents:", content)
        self.assertIn("Pond traits already scored:", content)


if __name__ == "__main__":
    unittest.main()
