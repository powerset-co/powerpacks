"""Shared profile hydration owns prefetch pacing before worker submission."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.enrich.profiles import prefetch
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import (
    ProfilePrefetchCounts,
)
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileTarget
from packs.ingestion.primitives.enrich import rapidapi_client


class PrefetchRateTest(unittest.TestCase):
    def test_rpm_spacing_happens_before_worker_submission(self) -> None:
        links = [
            ProfileTarget(
                f"jordan-{index}",
                f"https://www.linkedin.com/in/jordan-{index}",
            )
            for index in range(3)
        ]
        response = {
            "state": rapidapi_client.PROFILE_CONTENT,
            "normalized_profile": {
                "success": True,
                "full_name": "Fixture Profile",
                "experiences": [{"title": "Founder", "company_name": "Example"}],
            },
            "from_cache": False,
            "fetched": True,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(rapidapi_client.RapidApiClient, "resolve_key", return_value="key"),
            mock.patch.object(rapidapi_client.RapidApiClient, "__init__", return_value=None),
            mock.patch.object(rapidapi_client.RapidApiClient, "get_profile", return_value=response) as fetch,
            mock.patch.object(rapidapi_client.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 60.0]),
            mock.patch.object(rapidapi_client.time, "sleep") as sleep,
        ):
            counts = prefetch.prefetch(
                links,
                Path(directory),
                concurrency=3,
                rpm=2,
            )

        self.assertEqual(
            counts,
            ProfilePrefetchCounts(3, 3, 0, 0, 0, 3),
        )
        self.assertEqual(fetch.call_count, 3)
        sleep.assert_called_once_with(60.0)

    def test_zero_rpm_disables_pacing(self) -> None:
        link = ProfileTarget(
            "casey-delta",
            "https://www.linkedin.com/in/casey-delta",
        )
        response = {
            "state": rapidapi_client.PROFILE_CONTENT,
            "normalized_profile": {
                "success": True,
                "full_name": "Fixture Profile",
                "experiences": [{"title": "Founder", "company_name": "Example"}],
            },
            "from_cache": True,
            "fetched": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(rapidapi_client.RapidApiClient, "resolve_key", return_value="key"),
            mock.patch.object(rapidapi_client.RapidApiClient, "__init__", return_value=None),
            mock.patch.object(rapidapi_client.RapidApiClient, "get_profile", return_value=response),
            mock.patch.object(rapidapi_client.time, "monotonic") as monotonic,
        ):
            counts = prefetch.prefetch([link], Path(directory), rpm=0)

        self.assertEqual(
            counts,
            ProfilePrefetchCounts(1, 0, 1, 0, 0, 0),
        )
        monotonic.assert_not_called()

    def test_billed_empty_fetch_still_counts_as_a_network_call(self) -> None:
        """A live fetch that comes back empty is money spent: the receipt's
        network signal must come from the client's fetched flag, not from
        successes — every miss here lands in `failed`, and the old
        successes-only signal would report a fully billed run as offline."""
        link = ProfileTarget(
            "casey-delta",
            "https://www.linkedin.com/in/casey-delta",
        )
        response = {
            "state": rapidapi_client.PROFILE_EMPTY,
            "normalized_profile": {"success": False},
            "from_cache": False,
            "fetched": True,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(rapidapi_client.RapidApiClient, "resolve_key", return_value="key"),
            mock.patch.object(rapidapi_client.RapidApiClient, "__init__", return_value=None),
            mock.patch.object(rapidapi_client.RapidApiClient, "get_profile", return_value=response),
        ):
            counts = prefetch.prefetch([link], Path(directory), rpm=0)

        self.assertEqual(
            counts,
            ProfilePrefetchCounts(1, 0, 0, 1, 0, 1),
        )


if __name__ == "__main__":
    unittest.main()
