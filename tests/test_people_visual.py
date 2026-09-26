"""Visual regression for the People page on the synthetic fixture.

Each state is screenshotted in Chrome with reduced motion and compared to
`tests/visual/people/<state>.png`. A pixel counts as changed when any channel
moves by more than 24; a state fails when more than 0.4% of its pixels changed.
`UPDATE_VISUAL=1` rewrites the baselines instead of comparing; a failing state
writes `<state>.actual.png` and `<state>.diff.png` next to the baseline.

Created: 2026-09-26. Change log: 2026-09-26 created for the TypeScript port.
"""

from __future__ import annotations

import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from packs.ingestion.primitives.share.web.server import make_handler, share_routes
from test_share_web import ShareWebFixture

BASELINES = Path(__file__).resolve().parent / "visual" / "people"
VIEWPORT = {"width": 1440, "height": 900}
NARROW = {"width": 1100, "height": 800}
CHANNEL_TOLERANCE = 24
CHANGED_PIXELS_ALLOWED = 0.004


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


class PeopleVisualTests(ShareWebFixture):
    """Every People state the port must keep pixel-for-pixel (system fonts, one machine)."""

    def setUp(self) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Playwright and Pillow are dev-only")
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(share_routes(self.db, self.people_csv)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.update = os.environ.get("UPDATE_VISUAL") == "1"
        self.failures: list[str] = []

    def _shot(self, page, state: str) -> None:
        page.wait_for_timeout(120)
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline = BASELINES / f"{state}.png"
        if self.update or not baseline.exists():
            page.screenshot(path=str(baseline))
            return
        actual = BASELINES / f"{state}.actual.png"
        page.screenshot(path=str(actual))
        changed = _compare(baseline, actual)
        if changed > CHANGED_PIXELS_ALLOWED:
            self.failures.append(f"{state}: {changed:.2%} of pixels changed")
        else:
            actual.unlink()

    def test_states_match_the_baselines(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce")
            page = context.new_page()
            page.goto(self.base + "/people")
            expect(page.locator(".row")).to_have_count(1)
            self._shot(page, "confirm-tab")
            page.locator("[data-tab='yes']").click()
            expect(page.locator(".row")).to_have_count(2)
            self._shot(page, "sharing-tab")
            page.locator("[data-facet-key='relationship_kind'][data-facet-value='colleague']").click()
            expect(page.locator(".row")).to_have_count(1)
            self._shot(page, "facet-selected")
            page.get_by_role("button", name="Clear filters").click()
            page.locator("[data-select-all]").check()
            expect(page.locator("[data-bulkbar] b")).to_have_text("2 selected")
            self._shot(page, "selection-bulk-bar")
            page.keyboard.press("Escape")
            page.locator(".row").first.click()
            expect(page.locator("[data-drawer] details[data-section='decision'][open]")).to_have_count(1)
            expect(page.locator("[data-drawer] details[data-section='relationship']")).to_have_count(1)
            self._shot(page, "drawer-open")
            for key in ("facts", "relationship", "topics", "contact", "confidence"):
                summary = page.locator(f"[data-drawer] details[data-section='{key}'] summary")
                if summary.count():
                    summary.click()
            self._shot(page, "drawer-sections-open")
            page.keyboard.press("Escape")
            page.locator("[data-tab='confirm']").click()
            page.locator("[data-select-all]").check()
            page.get_by_role("button", name="Share S").click()
            expect(page.locator(".toast")).to_contain_text("Marked 1 person for sharing.")
            self._shot(page, "toast-after-share")
            page.locator(".toast").get_by_role("button", name="Undo Z").click()
            expect(page.locator(".row")).to_have_count(1)
            page.locator("[data-search]").fill("nobody here")
            expect(page.locator("[data-empty]")).to_be_visible()
            self._shot(page, "empty-filtered")
            page.locator("[data-search]").fill("")
            page.set_viewport_size(NARROW)
            expect(page.locator(".row")).to_have_count(1)
            self._shot(page, "narrow-columns")
            browser.close()
        self.assertEqual(self.failures, [])


if __name__ == "__main__":
    unittest.main()
