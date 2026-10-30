"""The user's Chrome profiles, and copying one into the browser profiles Powerpacks signs in with.

Chrome refuses automation on its own data folder, so the picked profile is copied. Its cookies and
saved passwords are encrypted with Chrome's Keychain key, so the copy only opens signed in under
the installed Chrome with Playwright's mock keychain flags off: common/browser.js does that for a
profile holding COPIED_FROM. The user's Chrome is only read, and may be open.

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

CHROME_APP = Path("/Applications/Google Chrome.app")
CHROME_DATA = Path("~/Library/Application Support/Google/Chrome")
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


def chrome_profiles(data: Path = CHROME_DATA, app: Path = CHROME_APP) -> list[ChromeProfile]:
    """Chrome's profiles holding a LinkedIn or Google session: LinkedIn first, then last used."""
    state = data.expanduser() / "Local State"
    if not app.exists() or not state.is_file():
        return []
    cache = json.loads(state.read_text(encoding="utf-8"))["profile"]["info_cache"]
    found = []
    for folder, info in sorted(cache.items(), key=lambda item: -item[1].get("active_time", 0)):
        cookies = data.expanduser() / folder / "Cookies"
        found.append(ChromeProfile(folder, info.get("name", folder), info.get("user_name", ""),
                                   has_cookie(cookies, "linkedin.com", "li_at"), has_cookie(cookies, "google.com", "SID")))
    return sorted((profile for profile in found if profile.linkedin or profile.google),
                  key=lambda profile: not profile.linkedin)


def use_chrome_profile(folder: str, *, data: Path = CHROME_DATA, app: Path = CHROME_APP,
                       targets: tuple[Path, ...] = (BROWSER_PROFILE, DEFAULT_BROWSER_PROFILE)) -> None:
    """Copy one of chrome_profiles into each sign-in profile; what was there moves to a .bkup."""
    profile = next(profile for profile in chrome_profiles(data, app) if profile.folder == folder)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for target in (path.expanduser() for path in targets):
        if target.exists():
            target.rename(target.with_name(f"{target.name}.bkup-{stamp}"))
        shutil.copytree(data.expanduser() / folder, target / "Default", ignore=LEFT_BEHIND, symlinks=True)
        shutil.copy2(data.expanduser() / "Local State", target / "Local State")
        (target / COPIED_FROM).write_text(json.dumps({"folder": folder, "name": profile.name}), encoding="utf-8")
