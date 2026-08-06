"""Profile prefetch pacing stays owned by the submitting thread."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import prefetch_profiles


class PrefetchRateTest(unittest.TestCase):
    def test_rpm_spacing_happens_before_worker_submission(self) -> None:
        links = [
            {
                "public_identifier": f"jordan-{index}",
                "linkedin_url": f"https://www.linkedin.com/in/jordan-{index}",
            }
            for index in range(3)
        ]
        response = {"normalized_profile": {"success": True}, "from_cache": False}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(prefetch_profiles, "rapidapi_profile", return_value=response) as fetch,
            mock.patch.object(prefetch_profiles.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 60.0]),
            mock.patch.object(prefetch_profiles.time, "sleep") as sleep,
        ):
            counts = prefetch_profiles.prefetch(
                links,
                Path(directory),
                concurrency=3,
                rpm=2,
            )

        self.assertEqual(counts, {"fetched": 3, "from_cache": 0, "failed": 0, "attempted": 3})
        self.assertEqual(fetch.call_count, 3)
        sleep.assert_called_once_with(60.0)

    def test_zero_rpm_disables_pacing(self) -> None:
        link = {
            "public_identifier": "casey-delta",
            "linkedin_url": "https://www.linkedin.com/in/casey-delta",
        }
        response = {"normalized_profile": {"success": True}, "from_cache": True}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(prefetch_profiles, "rapidapi_profile", return_value=response),
            mock.patch.object(prefetch_profiles.time, "monotonic") as monotonic,
        ):
            counts = prefetch_profiles.prefetch([link], Path(directory), rpm=0)

        self.assertEqual(counts, {"fetched": 0, "from_cache": 1, "failed": 0, "attempted": 1})
        monotonic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
