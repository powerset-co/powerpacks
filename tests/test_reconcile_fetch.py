"""Offline tests for reconcile's prefer-cache-always-retrieve profile fetch.

The RapidAPI client is mocked where reconcile_linkedin binds it; everything else
(candidate selection, view rebuild from the cache, keyless skip, counts) runs
for real against synthetic fixtures.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from packs.ingestion.primitives.deep_context import reconcile_linkedin as rl


def task(pub="jordan-bravo", url="https://www.linkedin.com/in/jordan-bravo",
         has_profile=False, no_link=False, from_connections=False, pid="pid-1"):
    return {
        "parent_slug": "jordan-bravo-ab12cd34", "name": "Jordan Bravo",
        "candidate_key": pub, "person_ids": [pid],
        "no_link": no_link, "from_connections": from_connections,
        "linkedin": {"public_identifier": pub, "linkedin_url": url,
                     "has_profile": has_profile, "source": "people_csv"},
    }


class FetchCandidateTests(unittest.TestCase):
    def test_selects_only_urled_profileless_judge_targets(self):
        rows = [
            task(),                                    # wanted
            task(has_profile=True),                    # already judgeable
            task(no_link=True, url=""),                # nothing attached
            task(from_connections=True),               # ground truth, never judged
            {**task(), "linkedin": {"linkedin_url": "", "has_profile": False}},  # no URL
        ]
        wanted = rl.profile_fetch_candidates(rows)
        self.assertEqual(len(wanted), 1)
        self.assertIs(wanted[0], rows[0])


class FetchMissingProfilesTests(unittest.TestCase):
    def test_keyless_install_skips_cleanly(self):
        with mock.patch.object(rl.RapidApiClient, "resolve_key", return_value=""):
            counts = rl.fetch_missing_profiles([task()], {}, Path("unused"))
        self.assertEqual(counts["fetch_skipped_no_key"], 1)
        self.assertEqual(counts["fetch_ok"], 0)

    def test_fetch_hydrates_cache_and_rebuilds_view(self):
        with TemporaryDirectory() as d:
            cache_dir = Path(d)
            t = task()
            people = {"pid-1": {"linkedin_url": t["linkedin"]["linkedin_url"],
                                "full_name": "Jordan Bravo"}}

            def fake_fetch(self, pub, url, *, cache_dir=None, **kw):
                path = rl.profile_cache_path(cache_dir, pub)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "raw_response": {},
                    "normalized_profile": {
                        "success": True, "full_name": "Jordan Bravo",
                        "headline": "Founder at Bravo Robotics",
                        "experiences": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                        "education": [], "city": "SF", "state": "", "country": "",
                    },
                }))
                return {"status_code": 200, "normalized_profile": {"success": True}}

            with mock.patch.object(rl.RapidApiClient, "resolve_key", return_value="k"), \
                 mock.patch.object(rl.RapidApiClient, "__init__", return_value=None), \
                 mock.patch.object(rl.RapidApiClient, "fetch_profile", fake_fetch):
                counts = rl.fetch_missing_profiles([t], people, cache_dir)

        self.assertEqual(counts["fetch_ok"], 1)
        self.assertEqual(counts["fetch_failed"], 0)
        self.assertTrue(t["linkedin"]["has_profile"])       # view rebuilt from cache
        self.assertEqual(t["linkedin"]["source"], "cache")
        self.assertIn("Bravo Robotics", " ".join(t["linkedin"]["experiences"]))

    def test_failed_fetch_counts_and_leaves_task_unjudgeable(self):
        t = task()
        with mock.patch.object(rl.RapidApiClient, "resolve_key", return_value="k"), \
             mock.patch.object(rl.RapidApiClient, "__init__", return_value=None), \
             mock.patch.object(rl.RapidApiClient, "fetch_profile",
                               return_value={"status_code": 404, "normalized_profile": None}):
            counts = rl.fetch_missing_profiles([t], {}, Path("unused"))
        self.assertEqual(counts["fetch_failed"], 1)
        self.assertFalse(t["linkedin"]["has_profile"])


if __name__ == "__main__":
    unittest.main()
