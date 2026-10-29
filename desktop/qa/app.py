"""Drive the Powerpacks QA build's real window: find things on the page, click and type the way a
person does, time how long the page takes to answer, and take WebKit's picture of it.

A click first checks that the element is what sits on top at its centre, so a menu opened under a
panel fails as "covered by" instead of passing on a click a person could never make. Every wait
has a time budget; a step that takes longer fails with the requests it waited on.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

QA_APP_ID = "co.powerset.powerpacks.qa"

# Installed once per page: console errors, and helpers to find an element by its words.
PAGE_HELPERS = r"""
(() => {
  if (window.__qa) return true;
  const errors = [];
  const error = console.error.bind(console);
  console.error = (...args) => { errors.push(args.map(String).join(" ")); error(...args); };
  window.addEventListener("error", (event) => errors.push(String(event.message)));
  window.addEventListener("unhandledrejection", (event) => errors.push(String(event.reason)));
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  };
  const words = (el) => (el.getAttribute("aria-label") || el.innerText || el.value || "").trim();
  const find = (target) => {
    if (target.css) {
      const els = [...document.querySelectorAll(target.css)].filter(visible);
      return target.text ? els.filter((el) => words(el).includes(target.text)) : els;
    }
    const roles = "button,a,[role],input,select,textarea,label,summary,li,td,span,div,h1,h2,h3,p";
    const hits = [...document.querySelectorAll(roles)].filter(
      (el) => visible(el) && (target.exact ? words(el) === target.text : words(el).includes(target.text)),
    );
    // The innermost match: a button, not the row that holds it.
    return hits.filter((el) => !hits.some((other) => other !== el && el.contains(other)));
  };
  const describe = (el) =>
    el ? `<${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".") : ""}> "${words(el).slice(0, 40)}"` : "nothing";
  window.__qa = { errors, find, describe, words };
  return true;
})()
"""


class Failed(AssertionError):
    pass


class App:
    def __init__(self, port: int, pictures: Path):
        self.base = f"http://127.0.0.1:{port}"
        self.pictures = pictures
        self.mark = 0.0

    def _post(self, path: str, body: dict | None = None) -> bytes:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def js(self, expression: str):
        answer = json.loads(self._post("/eval", {"js": expression}))
        if "error" in answer:
            raise Failed(f"page error: {answer['error']}")
        return answer.get("value")

    def ready(self, timeout: float = 600) -> None:
        """Wait for the hook, then for the local page to replace the splash."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js("location.port") == "8765" and self.js("document.readyState") == "complete":
                    self.js(PAGE_HELPERS)
                    return
            except (OSError, Failed):
                pass
            time.sleep(0.5)
        raise Failed("the app did not reach the local page")

    def go(self, path: str, until: dict, within_ms: int = 3000) -> int:
        """Open a page of the app (the side nav's way in) and wait for something on it."""
        self.focus()
        self.mark = self.js("performance.now()")
        self.js(f"(history.pushState(null, '', {json.dumps(path)}), dispatchEvent(new PopStateEvent('popstate')), true)")
        self.js(PAGE_HELPERS)
        return self.wait_for(until, within_ms, f"{path} shows {until}")

    def locate(self, target: dict, nth: int = 0) -> dict:
        found = self.js(
            f"""(() => {{
              const els = __qa.find({json.dumps(target)});
              const el = els[{nth}];
              if (!el) return null;
              el.scrollIntoView({{block: "center", inline: "center"}});
              const box = el.getBoundingClientRect();
              const x = box.left + box.width / 2, y = box.top + box.height / 2;
              const top = document.elementFromPoint(x, y);
              const disabled = el.disabled || el.getAttribute("aria-disabled") === "true";
              return {{x, y, count: els.length, disabled,
                       covered: top && !el.contains(top) && !top.contains(el) ? __qa.describe(top) : null}};
            }})()"""
        )
        if not found:
            raise Failed(f"no {target} on the page")
        return found

    def focus(self) -> None:
        """Bring the window forward and wait until it is the key window: a click on a window in
        the background only activates it, and WebKit pauses animations while it is hidden."""
        deadline = time.time() + 5
        while not json.loads(self._post("/focus"))["key"]:
            if time.time() > deadline:
                raise Failed("the window did not come forward")
            # macOS may refuse an app's own activation while another app is in use.
            subprocess.run(["osascript", "-e", f'tell application id "{QA_APP_ID}" to activate'])
            time.sleep(0.2)

    def click(self, target: dict, nth: int = 0) -> None:
        """Click where a person would, once the element has stopped moving and nothing covers it
        (a person waits out an opening drawer too, so this waits up to a second)."""
        self.focus()
        deadline = time.time() + 1
        last = None
        while True:
            spot = self.locate(target, nth)
            settled = last is not None and (spot["x"], spot["y"]) == (last["x"], last["y"])
            if settled and not spot["covered"]:
                break
            if time.time() > deadline:
                if spot["covered"]:
                    raise Failed(f"{target} is covered by {spot['covered']}: a person's click lands there")
                break
            last = spot
            time.sleep(0.05)
        if spot["disabled"]:
            raise Failed(f"{target} is disabled")
        self.mark = self.js("performance.now()")
        self._post("/click", {"x": spot["x"], "y": spot["y"]})

    def reload(self) -> None:
        """A fresh page for the next scenario."""
        self.js("(location.reload(), true)")
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if not self.js("Boolean(window.__qa)"):
                    break
            except Failed:
                pass
            time.sleep(0.1)
        self.ready()

    def type(self, text: str) -> None:
        """One key at a time, at a quick person's pace."""
        self.focus()
        self.mark = self.js("performance.now()")
        for letter in text:
            self._post("/type", {"text": letter})
            time.sleep(0.05)

    def key(self, name: str) -> None:
        self.focus()
        self.mark = self.js("performance.now()")
        self._post("/type", {"key": name})

    def shows(self, target: dict) -> bool:
        return bool(self.js(f"__qa.find({json.dumps(target)}).length > 0"))

    def value(self, css: str) -> str:
        return self.js(f"(document.querySelector({json.dumps(css)}) || {{}}).value ?? null")

    def wait_for(self, target: dict | str, within_ms: int, what: str, patience: float = 0) -> int:
        """Wait for an element (or a JS condition) after the last action; fail past the budget."""
        condition = target if isinstance(target, str) else f"__qa.find({json.dumps(target)}).length > 0"
        # The page may wait for much longer than the budget so the failure says by how much.
        give_up = time.time() + (patience or max(10.0, within_ms / 1000 * 4))
        while time.time() < give_up:
            if self.js(condition):
                took = round(self.js("performance.now()") - self.mark)
                if took > within_ms:
                    raise Failed(f"{what}: {took} ms, budget {within_ms} ms; {self.requests()}")
                return took
            time.sleep(0.02)
        raise Failed(f"{what}: never happened; {self.requests()}")

    def requests(self) -> str:
        """The page's requests since the last action, slowest first."""
        entries = self.js(
            f"""performance.getEntriesByType("resource")
              .filter((e) => e.startTime >= {self.mark})
              .map((e) => [new URL(e.name).pathname + new URL(e.name).search, Math.round(e.duration)])"""
        )
        entries = sorted(entries or [], key=lambda entry: -entry[1])[:5]
        return "requests: " + (", ".join(f"{path} {ms} ms" for path, ms in entries) or "none")

    def errors(self) -> list[str]:
        return self.js("__qa.errors.splice(0)") or []

    def opened(self) -> list[str]:
        return json.loads(urllib.request.urlopen(self.base + "/opened", timeout=10).read())

    def picture(self, name: str) -> Path:
        with urllib.request.urlopen(self.base + "/snapshot", timeout=30) as response:
            path = self.pictures / f"{name}.png"
            path.write_bytes(response.read())
            return path
