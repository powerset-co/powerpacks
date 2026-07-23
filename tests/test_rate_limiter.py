"""Regression: one shared StartRateLimiter, reused by every paced caller.

Created: 2026-06-19

Context: profile enrichment (enrich_people) and company-detail fetches
(rapidapi_company) both pace RapidAPI request starts to avoid bursting into
429 backoff. The pacer used to be duplicated; it now lives once in
packs.shared.rate_limiter so a single fix applies everywhere. These tests pin
the shared class's behavior and that both call sites import that one class.
"""
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.shared.rate_limiter import StartRateLimiter  # noqa: E402
from packs.indexing.primitives.enrich_companies_checkpointed import rapidapi_company as rc  # noqa: E402
from packs.ingestion.primitives.enrich import enrich_people as ep  # noqa: E402


class StartRateLimiterTests(unittest.TestCase):
    def test_interval_math(self) -> None:
        self.assertAlmostEqual(StartRateLimiter(300).interval, 0.2, places=6)
        self.assertAlmostEqual(StartRateLimiter(600).interval, 0.1, places=6)

    def test_extra_sleep_takes_the_larger_gap(self) -> None:
        # 600 rpm -> 0.1s, but the 0.5s extra-sleep floor dominates.
        self.assertAlmostEqual(StartRateLimiter(600, extra_sleep_seconds=0.5).interval, 0.5, places=6)

    def test_zero_rpm_disables_pacing(self) -> None:
        limiter = StartRateLimiter(0)
        self.assertEqual(limiter.interval, 0.0)
        start = time.monotonic()
        for _ in range(5):
            limiter.wait()
        self.assertLess(time.monotonic() - start, 0.05)

    def test_wait_enforces_minimum_spacing(self) -> None:
        # 600 rpm -> 0.1s between starts; 3 starts span >= 2 intervals. Lower
        # bound only (a busy machine makes it slower, never faster).
        limiter = StartRateLimiter(600)
        start = time.monotonic()
        for _ in range(3):
            limiter.wait()
        self.assertGreaterEqual(time.monotonic() - start, 0.18)


class SharedAcrossCallSitesTests(unittest.TestCase):
    def test_company_fetch_uses_shared_limiter_at_300_rpm(self) -> None:
        self.assertIs(rc.StartRateLimiter, StartRateLimiter)
        self.assertIsInstance(rc._RATE_LIMITER, StartRateLimiter)
        if "POWERPACKS_RAPIDAPI_COMPANY_MAX_RPM" not in os.environ:
            self.assertEqual(rc.DEFAULT_COMPANY_MAX_RPM, 300.0)
            self.assertAlmostEqual(rc._RATE_LIMITER.interval, 0.2, places=6)

    def test_profile_enrichment_uses_shared_limiter(self) -> None:
        self.assertIs(ep.StartRateLimiter, StartRateLimiter)


if __name__ == "__main__":
    unittest.main()
