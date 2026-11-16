"""Run the same scenarios headless: Playwright's WebKit (the engine under the app's window) on a
page server of its own, so nothing touches the installed Powerpacks, its port or the screen.

What it can't check: anything that calls the desktop app (chat, sign-ins, updates), since a plain
browser has no `window.__TAURI__`. Those stay on the native QA build (`run.py` without --browser).
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Error, Route, sync_playwright

from app import PAGE_HELPERS, App, Failed

LOCAL = re.compile(r"^https?://(127\.0\.0\.1|localhost)[:/]")


class Browser(App):
    def __init__(self, base: str, pictures: Path, engine: str = "webkit"):
        self.base, self.pictures, self.mark = base, pictures, 0.0
        self._opened: list[str] = []
        self._playwright = sync_playwright().start()
        self._browser = getattr(self._playwright, engine).launch()
        context = self._browser.new_context(viewport={"width": 1440, "height": 900})
        # A link that leaves the app is recorded, not followed (the app hands those to the browser).
        context.route(lambda url: not LOCAL.match(url), self._leave)
        self.page = context.new_page()
        context.on("page", lambda popup: popup.close())  # its first request was recorded above

    def _leave(self, route: Route) -> None:
        if route.request.is_navigation_request():
            self._opened.append(route.request.url)
        route.abort()

    def js(self, expression: str):
        try:
            return self.page.evaluate(expression)
        except Error as error:
            raise Failed(f"page error: {error.message.splitlines()[0]}") from None

    def ready(self, timeout: float = 600) -> None:
        self.page.goto(self.base + "/agent", wait_until="load", timeout=timeout * 1000)
        self.js(PAGE_HELPERS)

    def reload(self) -> None:
        self.js("(sessionStorage.clear(), true)")
        self.ready()

    def focus(self) -> None:
        pass  # headless: always the key window

    def _tap(self, x: float, y: float) -> None:
        self.page.mouse.click(x, y)

    def _press(self, text: str = "", key: str = "") -> None:
        if text:
            self.page.keyboard.type(text)
        else:
            self.page.keyboard.press(key)

    def opened(self) -> list[str]:
        opened, self._opened = self._opened, []
        return opened

    def picture(self, name: str) -> Path:
        path = self.pictures / f"{name}.png"
        self.page.screenshot(path=path)
        return path

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()
