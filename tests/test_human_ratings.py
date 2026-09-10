import unittest

from packs.search.primitives.shared.human_ratings import RUBRIC, SCORES, convert_rating


class HumanRatingsTests(unittest.TestCase):
    def test_five_choices_and_legacy_conversion_preserve_notes(self):
        self.assertEqual(SCORES, (1, 2, 3, 4, 5))
        self.assertEqual(tuple(RUBRIC), SCORES)
        for old, new in {1: 1, 2: 2, 3: 2, 4: 2, 7: 3, 8: 4, 9: 5, 10: 5}.items():
            original = {"score": old, "note": "Original feedback"}
            converted = convert_rating(original)
            self.assertEqual(converted, {"score": new, "note": "Original feedback", "scale": 5})
            self.assertEqual(convert_rating(converted), converted)
            self.assertEqual(original["score"], old)

    def test_current_scores_and_missing_evidence(self):
        for score in (*SCORES, None):
            value = {"score": score, "scale": 5}
            self.assertEqual(convert_rating(value), value)
        self.assertEqual(convert_rating({"score": None, "unclear": True}),
                         {"score": None, "unclear": True, "scale": 5})

    def test_invalid_values_are_not_guessed(self):
        for value in ({"score": 5}, {"score": 6}, {"score": True}, {"score": "4"},
                      {"score": 7, "scale": 5}, {"score": 4, "scale": 7}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert_rating(value)
