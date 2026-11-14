"""The user's Chrome and Brave profiles, and copying one into the browser profiles Powerpacks signs in with.

A browser refuses automation on its own data folder, so the picked profile is copied. Its cookies
and saved passwords are encrypted with that browser's Keychain key, so the copy only opens signed
in under the same installed browser with Playwright's mock keychain flags off: common/browser.js
does that for a profile holding COPIED_FROM, which names the browser. The user's browser is only
read, and may be open.

Changelog:
  2026-11-14: Brave profiles too; COPIED_FROM names the browser (none means Chrome).
Created: 2026-10-30
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from packs.ingestion.primitives.discover.linkedin.connections import BROWSER_PROFILE
from packs.ingestion.primitives.setup.automations.oauth_browser import DEFAULT_BROWSER_PROFILE

# key -> (app, data folder). common/browser.js maps the same keys to the app it opens a copy in.
BROWSERS = {
    "chrome": (Path("/Applications/Google Chrome.app"), Path("~/Library/Application Support/Google/Chrome")),
    "brave": (Path("/Applications/Brave Browser.app"), Path("~/Library/Application Support/BraveSoftware/Brave-Browser")),
}
COPIED_FROM = "chrome-profile.json"
CHROME_EPOCH_SECONDS = 11_644_473_600
# What a sign-in does not need: caches, extensions and history are most of a profile's size, and
# Sessions would reopen the user's tabs. Singleton* are the live Chrome's locks.
LEFT_BEHIND = shutil.ignore_patterns(
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker", "Shared Dictionary",
    "File System", "IndexedDB", "Sessions", "Extension*", "Local Extension Settings", "Managed Extension Settings",
    "Sync Extension Settings", "History*", "Favicons*", "Top Sites*", "Visited Links", "Singleton*", "LOCK")


@dataclass(frozen=True)
class ChromeProfile:
    browser: str
    folder: str
    name: str
    email: str
    linkedin: bool
    google: bool


def has_cookie(cookies: Path, host: str, name: str) -> bool:
    """An unexpired cookie in a Chrome cookie file, read by name alone and without Chrome's lock."""
    try:
        with closing(sqlite3.connect(f"file:{cookies}?mode=ro&immutable=1", uri=True)) as conn:
            (expires,) = conn.execute("SELECT MAX(expires_utc) FROM cookies WHERE host_key LIKE ? AND name = ?",
                                      (f"%{host}", name)).fetchone()
    except sqlite3.Error:
        return False
    return expires is not None and expires / 1_000_000 - CHROME_EPOCH_SECONDS > time.time()


def chrome_profiles(browsers: dict[str, tuple[Path, Path]] = BROWSERS) -> list[ChromeProfile]:
    """Chrome and Brave profiles holding a LinkedIn or Google session: LinkedIn first, then last used."""
    found = []
    for browser, (app, data) in browsers.items():
        state = data.expanduser() / "Local State"
        if not app.exists() or not state.is_file():
            continue
        for folder, info in json.loads(state.read_text(encoding="utf-8"))["profile"]["info_cache"].items():
            cookies = data.expanduser() / folder / "Cookies"
            found.append((info.get("active_time", 0), ChromeProfile(
                browser, folder, info.get("name", folder), info.get("user_name", ""),
                has_cookie(cookies, "linkedin.com", "li_at"), has_cookie(cookies, "google.com", "SID"))))
    found.sort(key=lambda item: -item[0])
    return sorted((profile for _, profile in found if profile.linkedin or profile.google),
                  key=lambda profile: not profile.linkedin)


def copied_from(target: Path = BROWSER_PROFILE) -> dict[str, str] | None:
    """The browser and profile folder the sign-in browser was copied from, or None before any copy."""
    marker = target.expanduser() / COPIED_FROM
    if not marker.is_file():
        return None
    saved = json.loads(marker.read_text(encoding="utf-8"))
    return {"browser": saved.get("browser", "chrome"), "folder": saved["folder"]}


def use_chrome_profile(browser: str, folder: str, *, browsers: dict[str, tuple[Path, Path]] = BROWSERS,
                       targets: tuple[Path, ...] = (BROWSER_PROFILE, DEFAULT_BROWSER_PROFILE)) -> None:
    """Copy one of chrome_profiles into each sign-in profile; what was there moves to a .bkup."""
    profile = next(profile for profile in chrome_profiles(browsers)
                   if (profile.browser, profile.folder) == (browser, folder))
    data = browsers[browser][1].expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for target in (path.expanduser() for path in targets):
        if target.exists():
            target.rename(target.with_name(f"{target.name}.bkup-{stamp}"))
        shutil.copytree(data / folder, target / "Default", ignore=LEFT_BEHIND, symlinks=True)
        shutil.copy2(data / "Local State", target / "Local State")
        (target / COPIED_FROM).write_text(json.dumps({"browser": browser, "folder": folder, "name": profile.name}),
                                          encoding="utf-8")
