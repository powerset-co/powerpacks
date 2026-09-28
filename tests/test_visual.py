"""Visual regression for the People and Searches pages on synthetic fixtures.

Both pages are served by one handler composed as review/server.py composes it (the
app shell, the Searches JSON routes, the People routes, the legacy search routes).
Each state is screenshotted in Chrome with reduced motion and compared to
`tests/visual/<page>/<state>.png`. A pixel counts as changed when any channel moves
by more than 24; a state fails when more than 0.4% of its pixels changed.
`UPDATE_VISUAL=1` rewrites the baselines instead of comparing; a failing state writes
`<state>.actual.png` and `<state>.diff.png` next to the baseline.

The Searches sidebar groups runs by age, so its context runs on a fixed clock and zone.

Created: 2026-09-26.
Changelog:
  2026-09-26: drawer sections open through their heading toggles (DetailsSection folds, no <details>).
  2026-09-26: created for the TypeScript port (People).
  2026-09-26: renamed from test_people_visual.py; Searches states added; one handler for both.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packs.ingestion.primitives.share.web.server import ShareRoutes, share_routes
from packs.search.primitives.deep_search.results_web.api import search_api
from packs.search.primitives.deep_search.results_web.server import _send, search_routes
from packs.shared.web.app import AppRoutes
from test_local_searches_browser import _results_root
from test_share_web import ShareWebFixture

VISUAL = Path(__file__).resolve().parent / "visual"
VIEWPORT = {"width": 1440, "height": 900}
NARROW = {"width": 1100, "height": 800}
CHANNEL_TOLERANCE = 24
CHANGED_PIXELS_ALLOWED = 0.004
SEARCHES_NOW = "2026-09-26T12:00:00Z"
SEARCHES_ZONE = "UTC"


def _compare(baseline: Path, actual: Path) -> float:
    """The share of pixels that changed, writing a diff image when any did."""
    from PIL import Image, ImageChops

    before = Image.open(baseline).convert("RGB")
    after = Image.open(actual).convert("RGB")
    if before.size != after.size:
        return 1.0
    diff = ImageChops.difference(before, after).point(lambda value: 255 if value > CHANNEL_TOLERANCE else 0)
    mask = diff.convert("L").point(lambda value: 255 if value else 0)
    changed = sum(1 for value in mask.getdata() if value)
    if changed:
        mask.save(actual.with_name(actual.name.replace(".actual.png", ".diff.png")))
    return changed / (before.size[0] * before.size[1])


def _handler(share: ShareRoutes, results_root: Path) -> type[BaseHTTPRequestHandler]:
    """review/server.py's order: the shell, Searches JSON, People, legacy search routes."""
    app = AppRoutes()
    searches = search_routes(results_root, base="/searches")
    searches_json = search_api(searches)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not (app.get(self, parsed) or searches_json.get(self, parsed) or share.get(self, parsed)
                    or searches.get(self, parsed)):
                _send(self, b"not found", "text/plain", status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not (share.post(self, parsed) or searches.post(self, parsed)):
                _send(self, b"not found", "text/plain", status=HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    return Handler


class VisualTests(ShareWebFixture):
    """Every People and Searches state the app must keep pixel-for-pixel (system fonts, one machine)."""

    def setUp(self) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Playwright and Pillow are dev-only")
        super().setUp()
        results = tempfile.TemporaryDirectory()
        self.addCleanup(results.cleanup)
        _results_root(Path(results.name))
        handler = _handler(share_routes(self.db, self.people_csv), Path(results.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.update = os.environ.get("UPDATE_VISUAL") == "1"
        self.failures: list[str] = []
        self.errors: list[str] = []

    def _shot(self, page, name: str) -> None:
        page.wait_for_timeout(120)
        baseline = VISUAL / f"{name}.png"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        if self.update or not baseline.exists():
            page.screenshot(path=str(baseline))
            return
        actual = baseline.with_name(f"{baseline.stem}.actual.png")
        page.screenshot(path=str(actual))
        changed = _compare(baseline, actual)
        if changed > CHANGED_PIXELS_ALLOWED:
            self.failures.append(f"{name}: {changed:.2%} of pixels changed")
        else:
            actual.unlink()

    def test_states_match_the_baselines(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            self._people(browser)
            self._searches(browser)
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])

    def _people(self, browser) -> None:
        from playwright.sync_api import expect

        context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce")
        page = context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(self.base + "/people")
        expect(page.locator(".row")).to_have_count(1)
        self._shot(page, "people/confirm-tab")
        page.locator("[data-tab='yes']").click()
        expect(page.locator(".row")).to_have_count(3)
        self._shot(page, "people/sharing-tab")
        page.locator("[data-facet-key='relationship_kind'][data-facet-value='colleague']").click()
        expect(page.locator(".row")).to_have_count(1)
        self._shot(page, "people/facet-selected")
        page.get_by_role("button", name="Clear filters").click()
        page.locator("[data-select-all]").check()
        expect(page.locator("[data-action-bar] b")).to_have_text("3 selected")
        self._shot(page, "people/selection-bulk-bar")
        page.keyboard.press("Escape")
        page.locator(".row").first.click()
        expect(page.locator("[data-drawer] [data-section='decision'][data-open='true']")).to_have_count(1)
        expect(page.locator("[data-drawer] [data-section='relationship']")).to_have_count(1)
        self._shot(page, "people/drawer-open")
        for key in ("facts", "relationship", "topics", "contact", "confidence"):
            toggle = page.locator(f"[data-drawer] [data-section='{key}'] button[aria-expanded]")
            if toggle.count():
                toggle.click()
        self._shot(page, "people/drawer-sections-open")
        page.keyboard.press("Escape")
        page.locator("[data-tab='confirm']").click()
        page.locator("[data-select-all]").check()
        page.get_by_role("button", name="Share S").click()
        expect(page.locator(".toast")).to_contain_text("Marked 1 person for sharing.")
        self._shot(page, "people/toast-after-share")
        page.locator(".toast").get_by_role("button", name="Undo Z").click()
        expect(page.locator(".row")).to_have_count(1)
        page.locator("[data-search]").fill("nobody here")
        expect(page.locator("[data-empty]")).to_be_visible()
        self._shot(page, "people/empty-filtered")
        page.locator("[data-search]").fill("")
        page.set_viewport_size(NARROW)
        expect(page.locator(".row")).to_have_count(1)
        self._shot(page, "people/narrow-columns")
        context.close()

    def _searches(self, browser) -> None:
        from playwright.sync_api import expect

        context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce", timezone_id=SEARCHES_ZONE)
        page = context.new_page()
        page.clock.set_fixed_time(SEARCHES_NOW)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(self.base + "/searches")
        expect(page.locator("[data-search-list] [data-run-id]")).to_have_count(3)
        expect(page.locator("[data-run-empty]")).to_be_visible()
        self._shot(page, "searches/list")

        page.locator("[data-run-id='jordan-role']").click()
        names = page.locator(".result-row .result-who b")
        expect(names).to_have_text(["Jordan Bravo", "Morgan Echo", "Casey Delta"])
        self._shot(page, "searches/run-open")

        page.locator("[data-person-id='p-jordan'] .result-main").click()
        expect(page.locator("[data-drawer] [data-career]")).to_contain_text("Example University")
        expect(page.get_by_role("toolbar", name="Review")).to_be_visible()
        self._shot(page, "searches/drawer-open")
        page.keyboard.press("Escape")
        expect(page.get_by_role("toolbar", name="Review")).to_have_count(0)

        page.get_by_role("button", name="Overall score 5", exact=True).click()
        expect(names).to_have_text(["Jordan Bravo"])
        self._shot(page, "searches/score-5-filter")
        page.get_by_role("button", name="All scores", exact=True).click()
        expect(names).to_have_count(3)

        page.get_by_role("button", name="Add tag to Casey Delta", exact=True).click()
        with page.expect_response(lambda response: response.url.endswith("/searches/tags")
                                  and response.request.method == "POST"):
            page.get_by_role("textbox", name="Add tag", exact=True).fill("Backend | Infra")
            page.keyboard.press("Enter")
        page.keyboard.press("Escape")
        expect(page.locator("[data-person-id='p-casey'] [data-tag='Backend | Infra']")).to_be_visible()
        self._shot(page, "searches/tagged-row")

        page.get_by_role("button", name="Score Morgan Echo", exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        self._shot(page, "searches/score-dialog")
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)

        page.get_by_role("button", name="Send feedback about Backend Engineer", exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        self._shot(page, "searches/feedback-dialog")
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)

        page.get_by_role("button", name="Overall score 1", exact=True).click()
        expect(page.locator("[data-results-empty]").first).to_be_visible()
        self._shot(page, "searches/empty-filter")
        page.get_by_role("button", name="All scores", exact=True).click()
        expect(names).to_have_count(3)

        page.set_viewport_size(NARROW)
        page.mouse.move(0, 0)
        self._shot(page, "searches/narrow")
        context.close()


if __name__ == "__main__":
    unittest.main()
