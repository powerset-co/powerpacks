"""Offline tests for the web_resolve tend primitive — no codex spawns, no LLM.

The engine call (`resolve_one`) is patched where it is defined; everything else
(selection, queue, skip/TTL, profile writes, the deterministic retarget
proposal path, the manifest) runs for real inside a chdir'd temp store —
deep-context paths are cwd-relative by design, so a temp cwd is a hermetic
empty install. Fixtures are obviously synthetic per the repo privacy contract.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import web_resolve as wr


def verdict_row(slug="jordan-bravo-ab12cd34", key="jordan-old", pid="pid-1"):
    return {
        "parent_slug": slug,
        "name": "Jordan Bravo",
        "person_ids": [pid],
        "candidate_key": key,
        "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{key}"},
        "verdict": {"verdict": "wrong_person", "confidence": 0.95,
                    "reason": "employer mismatch", "recommend_deep_research": True},
        "match_emails": ["casey@example.com"],
        "match_phones": ["+15550100"],
    }


FOUND_PROFILE = {
    "status": "found",
    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo-new",
    "person": {"real_name": "Jordan Bravo", "confidence": 0.92,
               "notes": "employer and school corroborated"},
    "research_notes": "confirmed employer match at Bravo Robotics and matching school era",
    "evidence_urls": ["https://www.linkedin.com/in/jordan-bravo-new"],
}


class WebResolveBase(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.reconcile = Path(".powerpacks/deep-context/reconcile")
        self.reconcile.mkdir(parents=True)
        self.overrides = Path("review.csv")

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write_verdicts(self, rows):
        (self.reconcile / "verdicts.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def node(self, **kw):
        return wr.WebResolve(overrides_csv=self.overrides, **kw)


class ParseAndPromptTests(unittest.TestCase):
    def test_parse_response_variants(self):
        self.assertEqual(wr._parse_response('{"status": "found"}'), {"status": "found"})
        self.assertEqual(wr._parse_response('note:\n{"a": 1}\ntrailer'), {"a": 1})
        self.assertEqual(wr._parse_response(""), {})
        self.assertEqual(wr._parse_response("no json"), {})

    def test_render_prompt_carries_person_and_rules(self):
        prompt = wr.render_prompt({
            "display_name": "Jordan Bravo", "primary_email": "casey@example.com",
            "phone_e164": "+15550100", "retarget_hint": "previous link judged WRONG",
            "bio": "robotics founder, Example Ventures seed", "known_info": "owner context",
        })
        for needle in ("Jordan Bravo", "casey@example.com", "+15550100",
                       "previous link judged WRONG", "robotics founder",
                       'return status "not_found" rather', "web search"):
            self.assertIn(needle, prompt)


class SkipReasonTests(WebResolveBase):
    def _profile(self, body):
        path = Path("out") / "h" / wr.WEB_PROFILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def test_found_is_final(self):
        path = self._profile({"status": "found", "linkedin_url": "https://x"})
        self.assertEqual(wr._skip_reason(path, 30), "resolved")

    def test_recent_not_found_waits_out_the_ttl(self):
        path = self._profile({"status": "not_found"})
        self.assertEqual(wr._skip_reason(path, 30), "recent_not_found")
        old = time.time() - 31 * 86400
        os.utime(path, (old, old))
        self.assertEqual(wr._skip_reason(path, 30), "")

    def test_corrupt_and_missing_reattempt(self):
        path = self._profile({})
        path.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(wr._skip_reason(path, 30), "")
        self.assertEqual(wr._skip_reason(Path("out/absent") / wr.WEB_PROFILE_NAME, 30), "")


class EstimateTests(WebResolveBase):
    def test_estimate_counts_and_writes_nothing(self):
        self.write_verdicts([verdict_row()])
        node = self.node(out_dir=Path("out"))
        result = node.estimate()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["would_attempt"], 1)
        self.assertEqual(result["handles"], ["jordan-bravo-ab12cd34"])
        self.assertFalse(Path("out").exists())

    def test_estimate_skips_resolved_on_disk(self):
        self.write_verdicts([verdict_row()])
        done = Path("out/jordan-bravo-ab12cd34") / wr.WEB_PROFILE_NAME
        done.parent.mkdir(parents=True)
        done.write_text(json.dumps(FOUND_PROFILE), encoding="utf-8")
        result = self.node(out_dir=Path("out")).estimate()
        self.assertEqual(result["skipped_resolved"], 1)
        self.assertEqual(result["would_attempt"], 0)


class ExecuteTests(WebResolveBase):
    def test_research_writes_profile_and_never_touches_review(self):
        self.write_verdicts([verdict_row()])
        with mock.patch.object(wr, "resolve_one",
                               return_value=(dict(FOUND_PROFILE), None)) as spawn:
            payload = self.node(out_dir=Path("out")).run()
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(payload.status, "completed")
        self.assertEqual(payload.mode, "research")
        self.assertEqual(payload.counts["found"], 1)
        self.assertEqual(payload.counts["proposable"], 1)
        profile = json.loads(
            (Path("out/jordan-bravo-ab12cd34") / wr.WEB_PROFILE_NAME).read_text())
        self.assertEqual(profile["source"], "web_resolve")
        self.assertFalse(self.overrides.exists())  # $0 pass leaves review.csv alone
        self.assertTrue((Path("out") / "manifest.json").exists())

    def test_second_research_run_skips_the_find_on_disk(self):
        self.write_verdicts([verdict_row()])
        with mock.patch.object(wr, "resolve_one",
                               return_value=(dict(FOUND_PROFILE), None)) as spawn:
            self.node(out_dir=Path("out")).run()
            second = self.node(out_dir=Path("out")).run()
        self.assertEqual(spawn.call_count, 1)  # a found profile is final on disk
        self.assertEqual(second.counts["attempted"], 0)
        self.assertEqual(second.counts["skipped_resolved"], 1)
        self.assertEqual(second.counts["proposable"], 1)

    def test_propose_judges_finds_and_upserts_pending_retarget(self):
        self.write_verdicts([verdict_row()])
        with mock.patch.object(wr, "resolve_one",
                               return_value=(dict(FOUND_PROFILE), None)):
            self.node(out_dir=Path("out")).run()
        confirming = {"verdict": "confirmed", "confidence": 0.9,
                      "reason": "employer corroborated"}
        import packs.ingestion.primitives.deep_context.reconcile_deep_research as dresearch
        with mock.patch.object(dresearch, "judge_research_proposal",
                               return_value=confirming) as judge:
            payload = self.node(out_dir=Path("out"), propose=True).run()
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(payload.mode, "propose")
        self.assertEqual(payload.retarget_upsert["proposed"], 1)
        review = self.overrides.read_text()
        self.assertIn("jordan-old", review)
        self.assertIn("jordan-bravo-new", review)
        # The judged retarget removes the person from the eligible pool — the
        # same rule that shrinks the paid Parallel queue.
        self.assertEqual(self.node(out_dir=Path("out")).estimate()["eligible"], 0)

    def test_not_found_is_recorded_but_never_proposable(self):
        self.write_verdicts([verdict_row()])
        not_found = {"status": "not_found", "linkedin_url": "",
                     "person": {"real_name": "", "confidence": 0.3, "notes": ""},
                     "research_notes": "could not verify the email linkage",
                     "evidence_urls": []}
        with mock.patch.object(wr, "resolve_one", return_value=(not_found, None)):
            payload = self.node(out_dir=Path("out")).run()
        self.assertEqual(payload.counts["not_found"], 1)
        self.assertEqual(payload.counts["proposable"], 0)

    def test_all_engine_failures_fail_the_pass(self):
        self.write_verdicts([verdict_row()])
        with mock.patch.object(wr, "resolve_one", return_value=({}, "timeout")):
            payload = self.node(out_dir=Path("out")).run()
        self.assertEqual(payload.status, "failed")
        self.assertEqual(list(payload.errors), ["timeout"])
        self.assertFalse((Path("out") / "jordan-bravo-ab12cd34" / wr.WEB_PROFILE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
