"""Chrome and Brave profiles to pick from, and the copy into the sign-in browsers."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from packs.ingestion.primitives.setup.automations.chrome_profiles import (
    CHROME_EPOCH_SECONDS,
    COPIED_FROM,
    chrome_profiles,
    copied_from,
    use_chrome_profile,
)


def write_cookies(folder: Path, *rows: tuple[str, str]) -> None:
    folder.mkdir(parents=True)
    expires = int((time.time() + 3600 + CHROME_EPOCH_SECONDS) * 1_000_000)
    with closing(sqlite3.connect(folder / "Cookies")) as conn:
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
        conn.executemany("INSERT INTO cookies VALUES (?, ?, ?)", [(*row, expires) for row in rows])
        conn.commit()


class ChromeProfilesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = self.root / "Chrome.app"
        self.app.mkdir()
        self.data = self.root / "Chrome"
        self.brave_app = self.root / "Brave.app"
        self.brave_app.mkdir()
        self.brave = self.root / "Brave"
        write_cookies(self.brave / "Default", (".google.com", "SID"))
        (self.brave / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Brave person", "active_time": 5}}}}))
        self.browsers = {"chrome": (self.app, self.data), "brave": (self.brave_app, self.brave)}
        write_cookies(self.data / "Default", (".www.linkedin.com", "li_at"))
        write_cookies(self.data / "Profile 1", (".google.com", "SID"))
        write_cookies(self.data / "Profile 2", (".example.com", "other"))
        (self.data / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Person 1", "user_name": "a@example.com", "active_time": 1},
            "Profile 1": {"name": "Work", "active_time": 2},
            "Profile 2": {"name": "Empty", "active_time": 3},
        }}}))

    def test_lists_signed_in_profiles_linkedin_first_then_last_used(self) -> None:
        profiles = chrome_profiles(self.browsers)
        self.assertEqual([(profile.browser, profile.folder) for profile in profiles],
                         [("chrome", "Default"), ("brave", "Default"), ("chrome", "Profile 1")])
        self.assertTrue(profiles[0].linkedin)
        self.assertTrue(profiles[1].google)

    def test_a_browser_not_installed_has_no_profiles(self) -> None:
        self.browsers["chrome"] = (self.root / "missing.app", self.data)
        self.assertEqual([profile.browser for profile in chrome_profiles(self.browsers)], ["brave"])

    def test_copy_keeps_what_was_there_and_leaves_the_caches(self) -> None:
        (self.data / "Default" / "Cache").mkdir()
        (self.data / "Default" / "Sessions").mkdir()
        (self.data / "Default" / "Login Data").write_text("saved")
        target = self.root / "browser-profiles" / "linkedin"
        target.mkdir(parents=True)
        (target / "old").write_text("kept")
        self.assertIsNone(copied_from(target))

        use_chrome_profile("chrome", "Default", browsers=self.browsers, targets=(target,))

        self.assertEqual((target / "Default" / "Login Data").read_text(), "saved")
        self.assertTrue((target / "Local State").is_file())
        self.assertEqual(json.loads((target / COPIED_FROM).read_text())["folder"], "Default")
        self.assertEqual(copied_from(target), {"browser": "chrome", "folder": "Default"})
        self.assertFalse((target / "Default" / "Cache").exists())
        self.assertFalse((target / "Default" / "Sessions").exists())
        (backup,) = target.parent.glob("linkedin.bkup-*")
        self.assertEqual((backup / "old").read_text(), "kept")

    def test_copy_from_brave_names_brave(self) -> None:
        target = self.root / "browser-profiles" / "google-oauth"
        use_chrome_profile("brave", "Default", browsers=self.browsers, targets=(target,))
        self.assertEqual(copied_from(target), {"browser": "brave", "folder": "Default"})
        self.assertTrue((target / "Default" / "Cookies").is_file())

    def test_a_copy_from_before_brave_is_chrome(self) -> None:
        target = self.root / "old-copy"
        target.mkdir()
        (target / COPIED_FROM).write_text(json.dumps({"folder": "Default", "name": "Person 1"}))
        self.assertEqual(copied_from(target), {"browser": "chrome", "folder": "Default"})
