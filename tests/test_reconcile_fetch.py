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

from packs.ingestion.primitives.deep_context.research_reconcile import judging
from packs.ingestion.primitives.deep_context.db.models import LinkRow, ParentRow, PersonRow, RowKind
from packs.ingestion.primitives.deep_context.db.store import Db
import packs.ingestion.primitives.deep_context.identity_reconcile.queue as queue
import packs.ingestion.primitives.deep_context.reconcile_linkedin as reconcile
from packs.ingestion.primitives.deep_context.reconcile_linkedin import ReconcileLinkedin
from packs.ingestion.primitives.enrich import rapidapi_client as rapid
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path


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
        wanted = queue.profile_fetch_candidates(rows)
        self.assertEqual(len(wanted), 1)
        self.assertIs(wanted[0], rows[0])


class FetchMissingProfilesTests(unittest.TestCase):
    def test_keyless_install_skips_cleanly(self):
        with mock.patch.object(queue.RapidApiClient, "resolve_key", return_value=""):
            counts = queue.fetch_missing_profiles([task()], Path("unused"))
        self.assertEqual(counts["fetch_skipped_no_key"], 1)
        self.assertEqual(counts["fetch_ok"], 0)

    def test_fetch_hydrates_cache_and_rebuilds_view(self):
        with TemporaryDirectory() as d:
            cache_dir = Path(d)
            t = task()

            def fake_fetch(self, pub, url, *, cache_dir=None, **kw):
                path = profile_cache_path(cache_dir, pub)
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
                return {"state": rapid.PROFILE_CONTENT,
                        "status_code": 200, "normalized_profile": {"success": True}}

            with mock.patch.object(queue.RapidApiClient, "resolve_key", return_value="k"), \
                 mock.patch.object(queue.RapidApiClient, "__init__", return_value=None), \
                 mock.patch.object(queue.RapidApiClient, "get_profile", fake_fetch):
                counts = queue.fetch_missing_profiles([t], cache_dir)

        self.assertEqual(counts["fetch_ok"], 1)
        self.assertEqual(counts["fetch_failed"], 0)
        self.assertTrue(t["linkedin"]["has_profile"])       # view rebuilt from cache
        self.assertEqual(t["linkedin"]["source"], "cache")
        self.assertIn("Bravo Robotics", " ".join(t["linkedin"]["experiences"]))

    def test_failed_fetch_counts_and_leaves_task_unjudgeable(self):
        t = task()
        with mock.patch.object(queue.RapidApiClient, "resolve_key", return_value="k"), \
             mock.patch.object(queue.RapidApiClient, "__init__", return_value=None), \
             mock.patch.object(queue.RapidApiClient, "get_profile",
                               return_value={"state": rapid.PROFILE_EMPTY,
                                             "status_code": 404, "normalized_profile": {}}):
            counts = queue.fetch_missing_profiles([t], Path("unused"))
        self.assertEqual(counts["fetch_failed"], 1)
        self.assertFalse(t["linkedin"]["has_profile"])


class SqliteReconcileTests(unittest.TestCase):
    def test_cli_dry_run_estimates_without_running_the_stage(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "deep-context.sqlite"
            Db(db_path)
            with (
                mock.patch.object(reconcile, "dry_run_estimate", return_value={"status": "dry_run"}) as estimate,
                mock.patch.object(reconcile, "ReconcileLinkedin") as node,
                mock.patch.object(reconcile, "emit") as emit,
            ):
                self.assertEqual(reconcile.main(["--db", str(db_path), "--dry-run"]), 0)
            estimate.assert_called_once()
            node.assert_not_called()
            emit.assert_called_once_with({"status": "dry_run"})

    def test_attached_link_judgment_is_file_first_and_sqlite_projected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-1", "jordan-bravo", "Jordan Bravo", "jordan-bravo-p"),
                PersonRow("person-1", "parent-1", display_name="Jordan Bravo"),
                LinkRow(
                    "jordan-bravo", "parent-1", "jordan-bravo", RowKind.PUB.value,
                    "https://www.linkedin.com/in/jordan-bravo", "Jordan Bravo",
                ),
            ))
            facts, raw, cache, output = (
                root / "facts", root / "raw", root / "cache", root / "reconcile"
            )
            for path in (facts, raw, cache):
                path.mkdir()
            profile_cache_path(cache, "jordan-bravo").write_text(json.dumps({
                "raw_response": {},
                "normalized_profile": {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [],
                    "education": [],
                },
            }), encoding="utf-8")
            verdicts = output / "verdicts.jsonl"
            payload = ReconcileLinkedin(
                db=db,
                people_csv=root / "must-not-be-read.csv",
                facts_dir=facts,
                raw_dir=raw,
                profile_cache_dir=cache,
                verdicts_jsonl=verdicts,
                verdicts_csv=output / "verdicts.csv",
                parents_dir=root / "parents",
                overrides_csv=root / "review.csv",
                consolidate_people_csv=root / "consolidate.csv",
                no_llm=True,
            ).run()

            link = db.query("SELECT * FROM links WHERE row_key='jordan-bravo'")[0]
            self.assertEqual((link["machine_action"], link["machine_approved"]), ("verify", "auto"))
            self.assertEqual(link["judgment_artifact_path"], str(verdicts))
            self.assertTrue(verdicts.exists())
            expected = {
                "parent_slug": "jordan-bravo-p",
                "parent_id": "parent-1",
                "name": "Jordan Bravo",
                "candidate_key": "jordan-bravo",
                "person_ids": ["person-1"],
                "conflict": False,
                "linkedin": {
                    "public_identifier": "jordan-bravo",
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "profile_pic_url": "",
                    "experiences": [],
                    "education": [],
                    "location": "",
                    "source": "cache",
                    "has_profile": True,
                },
                "verdict": {
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "supporting_evidence": ["attached profile (offline stub)"],
                    "contradicting_evidence": [],
                    "linkedin_plausibly_absent": False,
                    "recommend_deep_research": False,
                    "reason": "offline stub trusts the attached profile",
                },
                "error": "",
            }
            self.assertEqual(
                verdicts.read_bytes(),
                (json.dumps(expected, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
            )
            self.assertEqual(payload.tasks, 1)
            self.assertFalse((output / "verdicts.csv").exists())
            self.assertFalse((output / "summary.md").exists())
            self.assertFalse((root / "consolidate.csv").exists())


if __name__ == "__main__":
    unittest.main()


class HydrateProfilesTests(unittest.TestCase):
    """The one home for prefer-cache-always-retrieve."""

    def test_keyless_skips_without_fetching(self):
        with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""):
            counts = rapid.hydrate_profiles([("jordan-bravo", "https://x")], Path("unused"))
        self.assertEqual(counts, {"wanted": 1, "ok": 0, "failed": 0, "skipped_no_key": 1})

    def test_counts_ok_and_failed(self):
        calls = []

        def fake(self, pub, url, *, cache_dir=None, **kw):
            calls.append(pub)
            state = rapid.PROFILE_CONTENT if pub == "good" else rapid.PROFILE_EMPTY
            return {"state": state, "normalized_profile": {}}

        with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value="k"), \
             mock.patch.object(rapid.RapidApiClient, "__init__", return_value=None), \
             mock.patch.object(rapid.RapidApiClient, "get_profile", fake):
            counts = rapid.hydrate_profiles(
                [("good", "https://a"), ("bad", "https://b"), ("", "https://c")], Path("unused"))
        self.assertEqual(counts["wanted"], 2)          # the empty public_identifier is dropped
        self.assertEqual((counts["ok"], counts["failed"]), (1, 1))
        self.assertEqual(sorted(calls), ["bad", "good"])


class RetargetProposalHydrationTests(unittest.TestCase):
    """The retarget judge must see the REAL profile, not Parallel's payload."""

    def test_cached_profile_replaces_the_research_view(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, cache = base/"research", base/"facts", base/"raw", base/"cache"
            for p in (out, facts, raw, cache):
                p.mkdir(parents=True, exist_ok=True)
            (out/"jordan-bravo-p").mkdir()
            # Parallel found the URL but returned NO positions — the bug's shape.
            (out/"jordan-bravo-p"/"01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Jordan Bravo", "confidence": 0.9, "notes": "found via web"},
                "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
                "positions": [], "education": [],
                "metadata": {"research_notes": "confirmed by employer page"},
            }))
            (facts/"pid-1.jsonl").write_text(json.dumps(
                {"chunk_index": 0, "usage": {}, "facts": {"canonical_name": "Jordan Bravo"}}) + "\n")
            # The real profile IS in the shared cache.
            profile_cache_path(cache, "jordan-bravo").write_text(json.dumps({
                "raw_response": {}, "normalized_profile": {
                    "success": True, "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                    "education": [], "city": "SF", "state": "", "country": "",
                }}))
            subset = [{"parent_slug": "jordan-bravo-p", "name": "Jordan Bravo",
                       "person_ids": ["pid-1"], "candidate_key": "jordan-old",
                       "linkedin": {"linkedin_url": "https://www.linkedin.com/in/jordan-old"},
                       "match_emails": [], "match_phones": []}]
            seen = {}
            db = Db(base / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-1", "jordan-old", "Jordan Bravo"),
                PersonRow("pid-1", "parent-1", display_name="Jordan Bravo"),
                LinkRow(
                    "jordan-old", "parent-1", "jordan-old", "pub",
                    "https://www.linkedin.com/in/jordan-old", "Jordan Bravo",
                ),
            ))

            def capture(task, **kw):
                seen.update(task.get("linkedin") or {})
                return {"verdict": "confirmed", "confidence": 0.9, "reason": "ok"}

            with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""), \
                 mock.patch.object(judging, "judge_research_proposal", capture):
                judging.propose_retargets_from_output(
                    out, subset, base/"review.csv", db=db, facts_dir=facts, raw_dir=raw,
                    use_llm=True, profile_cache_dir=cache)

        # The judge saw the cached profile's experiences, not Parallel's empty positions.
        self.assertTrue(seen.get("has_profile"))
        self.assertIn("Bravo Robotics", " ".join(seen.get("experiences") or []))
