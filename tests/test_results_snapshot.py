"""Hosted snapshots reuse the local render model without local state or mutations."""

import copy
import html
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packs.search.primitives.deep_search.results_web.model import load_searches
from packs.search.primitives.deep_search.results_web.rendering import render_search_body
from packs.search.primitives.deep_search.results_web.snapshot import (
    _decode, export_snapshot, render_snapshot, validate_snapshot,
)
from packs.search.primitives.deep_search.results_web.model import SearchResult
from packs.search.primitives.deep_search.results_web import RESULTS_CSS, RESULTS_JS
from tests import test_deep_search_results_web as fixtures


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = fixtures.ResultsWebTest()._fixture(self.directory.name, cross_encoder=True)
        self.run_dir = root / "jordan-role"
        self.run_dir.joinpath("fit-labels.jsonl").write_text(json.dumps({
            "person_id": fixtures.ResultsWebTest.PERSON,
            "human": {"score": 5, "scale": 5, "note": "Strong work."},
        }) + "\n")
        self.run_dir.joinpath("tags.json").write_text(json.dumps({
            "tags": ["Discuss"], "assignments": {fixtures.ResultsWebTest.PERSON: ["Discuss"]},
        }))
        self.snapshot = export_snapshot(self.run_dir)

    def test_roundtrip_keeps_exact_rendered_results_and_saved_tags(self):
        saved = json.loads(json.dumps(self.snapshot))
        model = _decode(SearchResult, validate_snapshot(saved)["search"], "search")
        local = load_searches(self.run_dir.parent, self.run_dir.name)[0]
        self.assertEqual(render_search_body(model), render_search_body(local))
        self.assertEqual(saved["tags"]["assignments"], {fixtures.ResultsWebTest.PERSON: ["Discuss"]})
        self.assertEqual(model.candidate(fixtures.ResultsWebTest.PERSON).human_score, 5)

    def test_export_has_no_raw_artifacts_or_generated_profile_fields(self):
        text = json.dumps(self.snapshot)
        for excluded in (self.directory.name, "dense_text", "Generated semantic", "profiles_path",
                         "inferred_age", "birth_year", "api_key"):
            self.assertNotIn(excluded, text)

    def test_unknown_fields_and_nonfinite_scores_are_rejected(self):
        altered = copy.deepcopy(self.snapshot)
        altered["search"]["candidates"][0]["api_key"] = "secret"
        with self.assertRaisesRegex(ValueError, "renderer fields"):
            validate_snapshot(altered)
        altered = copy.deepcopy(self.snapshot)
        altered["search"]["total_cost_usd"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite number"):
            validate_snapshot(altered)

    def test_unsafe_link_schemes_and_url_credentials_are_rejected(self):
        for url in ("javascript:alert(1)", "data:text/html,x", "//example.com", "https://token@example.com"):
            altered = copy.deepcopy(self.snapshot)
            altered["search"]["candidates"][0]["linkedin_url"] = url
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "HTTP"):
                validate_snapshot(altered)

    def test_hosted_page_is_self_contained_and_read_only(self):
        html = render_snapshot(self.snapshot)
        self.assertIn("data-readonly='true'", html)
        self.assertIn("id='snapshot-tags'", html)
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link rel='stylesheet'", html)
        self.assertIn("Jordan Bravo", html)
        self.assertIn("data-score-filter='5'", html)
        self.assertNotIn("Loading results…", html)
        self.assertNotIn("data-feedback-note", html)
        self.assertNotIn("Strong work.", html)

    def test_script_like_tags_are_escaped(self):
        tag = "</script><script>alert(1)</script>"
        self.snapshot["tags"] = {"tags": [tag], "assignments": {fixtures.ResultsWebTest.PERSON: [tag]}}
        html = render_snapshot(self.snapshot)
        self.assertNotIn(tag, html)
        self.assertIn("\\u003c/script>", html)

    def test_date_and_pond_identifiers_cannot_inject_markup(self):
        from dataclasses import replace
        from packs.search.primitives.deep_search.results_web.rendering import _pond

        self.snapshot["search"]["created_at"] = "</span><img id='injected-date'>"
        self.assertNotIn("<img id='injected-date'>", render_snapshot(self.snapshot))
        search = _decode(SearchResult, self.snapshot["search"], "search")
        pond = replace(search.ponds[0], run_id="x'><img id='injected-pond'>")
        self.assertNotIn("<img id='injected-pond'>", _pond(pond, "test", selected=True))

    def test_hosted_assets_do_not_need_inline_scripts_or_styles(self):
        html = render_snapshot(self.snapshot, asset_base_url="https://api.example.com/v2/local-searches/assets")
        self.assertIn("src='https://api.example.com/v2/local-searches/assets/results.js'", html)
        self.assertIn("href='https://api.example.com/v2/local-searches/assets/results.css'", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<style>", html)
        self.assertNotIn("unsafe-inline", html)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "playwright is not installed")
    def test_browser_sandbox_filters_exports_and_profile_details_without_mutations(self):
        from playwright.sync_api import sync_playwright, expect

        snapshot = self.snapshot
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                assets = {"/assets/results.js": (RESULTS_JS, "text/javascript"),
                          "/assets/results.css": (RESULTS_CSS, "text/css")}
                if self.path in assets:
                    path, content_type = assets[self.path]
                    body = path.read_bytes()
                else:
                    origin = f"http://127.0.0.1:{self.server.server_port}"
                    content_type = "text/html"
                    doc = render_snapshot(snapshot, asset_base_url=f"{origin}/assets")
                    body = (f"<iframe style='width:100%;height:900px' sandbox='allow-scripts allow-downloads allow-popups' "
                            f"srcdoc='{html.escape(doc, quote=True)}'></iframe>").encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Security-Policy", "script-src 'self'; frame-src 'self'; style-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("https://**", lambda route: route.abort())
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                frame = page.frame_locator("iframe")
                expect(frame.locator("[data-search-body][data-loaded='true']")).to_have_count(1)
                expect(frame.locator(".candidate-row")).to_have_count(3)
                expect(frame.locator(".score-trigger:visible")).to_have_count(1)
                expect(frame.locator(".score-trigger:visible")).to_be_disabled()
                expect(frame.locator("[data-copy-results]")).to_be_hidden()
                frame.locator(".details-trigger").first.click()
                expect(frame.locator(".person-details:visible")).to_have_count(1)
                frame.locator(".details-trigger").first.click()
                frame.locator("[data-score-filter='5']").click()
                with page.expect_download() as download:
                    frame.locator("[data-export-csv]").click()
                csv = Path(download.value.path()).read_text()
                self.assertIn("Jordan Bravo", csv)
                self.assertIn(",5,", csv)
                self.assertNotIn("Casey Delta", csv)
                self.assertTrue(download.value.suggested_filename.startswith("discuss_"))
                self.assertEqual(errors, [])
                self.assertNotIn("/tags", requests)
                self.assertNotIn("/feedback", requests)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
