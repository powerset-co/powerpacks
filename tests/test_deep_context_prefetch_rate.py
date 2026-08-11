"""Shared profile hydration owns prefetch pacing before worker submission."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.enrich import prefetch_profiles
from packs.ingestion.primitives.deep_context.enrich.profile_models import ProfileTarget
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
            "normalized_profile": {"success": True},
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
            counts = prefetch_profiles.prefetch(
                links,
                Path(directory),
                concurrency=3,
                rpm=2,
            )

        self.assertEqual(
            counts,
            {"fetched": 3, "from_cache": 0, "failed": 0, "network_calls": 3, "attempted": 3},
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
            "normalized_profile": {"success": True},
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
            counts = prefetch_profiles.prefetch([link], Path(directory), rpm=0)

        self.assertEqual(
            counts,
            {"fetched": 0, "from_cache": 1, "failed": 0, "network_calls": 0, "attempted": 1},
        )
        monotonic.assert_not_called()

    def test_limit_zero_fetches_nothing(self) -> None:
        """--limit 0 is a defined no-spend probe, never "no limit".

        The falsy collapse (`if limit`) used to turn 0 into the FULL paid
        fetch — the expensive direction of the shipped-twice numeric-falsy
        family (--detach-threshold 0, machine_confidence 0.0).
        """
        link = ProfileTarget(
            "casey-delta",
            "https://www.linkedin.com/in/casey-delta",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(rapidapi_client.RapidApiClient, "get_profile") as fetch,
        ):
            counts = prefetch_profiles.prefetch([link], Path(directory), limit=0)

        self.assertEqual(
            counts,
            {"fetched": 0, "from_cache": 0, "failed": 0, "network_calls": 0, "attempted": 0},
        )
        fetch.assert_not_called()

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
            counts = prefetch_profiles.prefetch([link], Path(directory), rpm=0)

        self.assertEqual(
            counts,
            {"fetched": 0, "from_cache": 0, "failed": 1, "network_calls": 1, "attempted": 1},
        )


if __name__ == "__main__":
    unittest.main()
