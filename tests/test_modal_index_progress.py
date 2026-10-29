"""The index run's human-readable progress and time estimates."""
import unittest

from packs.indexing.modal.run_indexing import progress_line
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import STEPS, STEP_WORDS


class ProgressLineTests(unittest.TestCase):
    def test_every_step_has_words(self):
        self.assertEqual(set(STEPS), set(STEP_WORDS))

    def test_completed_steps_name_the_next_step_and_count_down(self):
        steps = 0
        estimates = []
        for step in STEPS:
            message, done, total, steps, left = progress_line(f"[build-processing] completed {step} in 1.791s\n", steps)
            position = min(done + 1, len(STEPS))
            self.assertEqual(message, f"{STEP_WORDS[STEPS[position - 1]]} ({position} of 15)")
            self.assertEqual((done, total), (steps, 15))
            estimates.append(left)
        self.assertEqual(estimates, sorted(estimates, reverse=True))
        self.assertEqual(estimates[-1], 0)
        self.assertEqual(progress_line("[build-processing] completed build_company_corpus in 1s", 3)[0],
                         "Understanding companies (5 of 15)")

    def test_fetch_start_and_counts_use_the_logged_rate(self):
        self.assertEqual(progress_line("[enrich-companies] fetching 700 company profiles at 700 rpm\n", 3),
                         ("Company profiles: 0 of 700", 0, 700, 3, 180))
        self.assertEqual(progress_line("[enrich-companies] fetched 350/700 company profiles\n", 3, 700),
                         ("Company profiles: 350 of 700", 350, 700, 3, 150))
        self.assertEqual(progress_line("[enrich-companies] fetched 700/700 company profiles\n", 3, 700),
                         ("Company profiles: 700 of 700", 700, 700, 3, 120))
        self.assertEqual(progress_line("[enrich-companies] fetched 40/111 company profiles\n", 3, 600)[-1],
                         127.1)

    def test_disabled_rate_limit_has_only_the_remaining_build_estimate(self):
        self.assertEqual(progress_line("[enrich-companies] fetching 700 company profiles at 0 rpm", 3)[-1], 120)

    def test_other_lines_say_nothing(self):
        self.assertIsNone(progress_line("[build-processing] start build_roles\n", 3))
        self.assertIsNone(progress_line("[enrich-companies] fetching 111 new company profiles from RapidAPI\n", 3))


if __name__ == "__main__":
    unittest.main()
