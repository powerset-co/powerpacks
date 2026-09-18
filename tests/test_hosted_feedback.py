"""The hosted viewer uses the existing feedback dialog through its authenticated parent."""

import importlib.util
import json
import tempfile
import unittest

from packs.search.primitives.deep_search.results_web.feedback import build_feedback_request
from packs.search.primitives.deep_search.results_web.feedback_payload import build_feedback_payload
from packs.search.primitives.deep_search.results_web.snapshot import (
    export_snapshot, render_snapshot, search_from_snapshot,
)
from tests import test_deep_search_results_web as fixtures
from tests.test_results_snapshot import serve_snapshot


class HostedFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = fixtures.ResultsWebTest()._fixture(self.directory.name, cross_encoder=True)
        self.snapshot = export_snapshot(root / "jordan-role")
        candidate = next(row for row in self.snapshot["search"]["candidates"]
                         if row["person_id"] == fixtures.ResultsWebTest.PERSON)
        candidate.update(human_score=4, human_note="Own reviewer note")

    def test_pure_payload_keeps_local_feedback_context_without_auth_dependencies(self):
        search = search_from_snapshot(self.snapshot)
        candidate = search.candidate(fixtures.ResultsWebTest.PERSON)
        human = {"score": 3, "scale": 5}
        pure = build_feedback_payload(search, "Worth a conversation", candidate, human)
        local = build_feedback_request(search, "Worth a conversation", candidate, {}, human)
        self.assertEqual(pure, local.body())
        self.assertEqual(pure["metadata"]["human_judgment"],
                         {"score": 3, "scale": 5, "note": "Worth a conversation"})
        self.assertEqual(pure["metadata"]["jd"], search.jd_text)
        self.assertEqual(pure["metadata"]["person_id"], candidate.person_id)
        self.assertNotIn("operator_id", pure)
        self.assertNotIn("set_id", pure)
        self.assertEqual(build_feedback_payload(search, "Broaden the role")["feedback_type"], "bad_search")

    def test_reviewer_notes_are_only_rendered_in_authenticated_mode(self):
        public = render_snapshot(self.snapshot)
        authenticated = render_snapshot(self.snapshot, feedback_enabled=True)
        self.assertNotIn("Own reviewer note", public)
        self.assertIn("Saved score: 4/5", public)
        self.assertNotIn("data-hosted-feedback='true'", public)
        self.assertIn("data-hosted-feedback='true'", authenticated)
        self.assertIn("data-feedback-note='Own reviewer note'", authenticated)
        self.assertIn("Your score: 4/5", authenticated)
        self.assertIn("connect-src 'none'", authenticated)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "playwright is not installed")
    def test_browser_saves_only_after_parent_ack_and_retries_visible_errors(self):
        from playwright.sync_api import sync_playwright, expect

        with serve_snapshot(self.snapshot, feedback_enabled=True) as (url, requests), sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://**", lambda route: route.abort())
            page.goto(url)
            page.evaluate("""() => {
                window.received = [];
                window.addEventListener('message', (event) => {
                    if (event.source === document.querySelector('iframe').contentWindow
                        && event.data?.type === 'powerpacks:feedback') {
                        window.received.push(event.data);
                        document.body.dataset.received = String(window.received.length);
                    }
                });
            }""")
            frame = page.frame_locator("iframe")
            expect(frame.locator("[data-search-body][data-loaded='true']")).to_have_count(1)
            expect(frame.locator(".score-trigger:visible")).to_have_count(3)
            expect(frame.locator(".tag-trigger").first).to_be_disabled()
            score = frame.get_by_role("button", name="Score Jordan Bravo", exact=True)
            score.click()
            expect(frame.get_by_role("textbox")).to_have_value("Own reviewer note")
            frame.locator(".score-grid label").nth(2).click()
            frame.get_by_role("textbox").fill("Worth introducing")
            frame.get_by_role("button", name="Save", exact=True).click()
            expect(page.locator("body")).to_have_attribute("data-received", "1")
            message = page.evaluate("window.received[0]")
            self.assertEqual(message["values"], {
                "run_id": "jordan-role", "person_id": fixtures.ResultsWebTest.PERSON,
                "comment": "Worth introducing", "human_judgment": json.dumps({"score": 3, "scale": 5}, separators=(",", ":")),
            })
            self.assertIsInstance(message["requestId"], str)
            expect(score).to_have_text("Your score: 4/5")
            expect(frame.get_by_role("dialog")).to_be_visible()
            response = {"type": "powerpacks:feedback-result", "requestId": message["requestId"], "status": "submitted"}
            frame.locator("body").evaluate("""(element, message) => new Promise(resolve => {
                window.addEventListener('message', () => resolve(), {once: true});
                window.postMessage(message, '*');
            })""", response)
            expect(frame.get_by_role("dialog")).to_be_visible()
            expect(score).to_have_text("Your score: 4/5")
            response.update(status="failed", error="Could not save. Try again.")
            page.evaluate("message => document.querySelector('iframe').contentWindow.postMessage(message, '*')", response)
            expect(frame.get_by_role("alert")).to_have_text("Could not save. Try again.")
            expect(frame.get_by_role("button", name="Save", exact=True)).to_be_enabled()
            expect(frame.get_by_role("textbox")).to_have_value("Worth introducing")
            frame.get_by_role("button", name="Save", exact=True).click()
            expect(page.locator("body")).to_have_attribute("data-received", "2")
            retry = page.evaluate("window.received[1]")
            self.assertNotEqual(message["requestId"], retry["requestId"])
            response.update(status="submitted", requestId=retry["requestId"])
            page.evaluate("message => document.querySelector('iframe').contentWindow.postMessage(message, '*')", response)
            expect(frame.get_by_role("dialog")).to_have_count(0)
            expect(score).to_have_text("Your score: 3/5")
            score.click()
            expect(frame.get_by_role("textbox")).to_have_value("Worth introducing")
            frame.get_by_role("button", name="Cancel", exact=True).click()
            frame.locator(".search-card > .feedback-trigger").click()
            frame.get_by_role("textbox").fill("Please broaden the search")
            frame.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("body")).to_have_attribute("data-received", "3")
            search_feedback = page.evaluate("window.received[2]")
            self.assertEqual(search_feedback["values"], {
                "run_id": "jordan-role", "person_id": "", "comment": "Please broaden the search",
            })
            response.update(requestId=search_feedback["requestId"])
            page.evaluate("message => document.querySelector('iframe').contentWindow.postMessage(message, '*')", response)
            expect(frame.get_by_role("dialog")).to_have_count(0)
            self.assertNotIn("/feedback", requests)
            self.assertNotIn("/tags", requests)
            self.assertEqual(errors, [])
            browser.close()


if __name__ == "__main__":
    unittest.main()
