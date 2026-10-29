"""The index run's progress lines (packs/indexing/modal/run_indexing.py progress_line)."""
import unittest

from packs.indexing.modal.run_indexing import progress_line
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import STEPS


class ProgressLineTests(unittest.TestCase):
    def test_completed_steps_count_up_across_the_build(self):
        message, done, total, steps = progress_line("[build-processing] completed flatten_people in 1.791s\n", 0)
        self.assertEqual((message, done, total, steps), (f"Building search records: 1 of {len(STEPS)} steps", 1, len(STEPS), 1))
        message, done, total, steps = progress_line("[build-processing] completed build_roles in 109.602s\n", steps)
        self.assertEqual((done, steps), (2, 2))

    def test_company_profiles_report_their_own_count_without_moving_the_steps(self):
        self.assertEqual(progress_line("[enrich-companies] fetched 40/111 company profiles\n", 3),
                         ("Company profiles: 40 of 111", 40, 111, 3))

    def test_other_lines_say_nothing(self):
        self.assertIsNone(progress_line("[build-processing] start build_roles\n", 3))
        self.assertIsNone(progress_line("[enrich-companies] fetching 111 new company profiles from RapidAPI\n", 3))


if __name__ == "__main__":
    unittest.main()
