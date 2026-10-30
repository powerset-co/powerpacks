"""Chrome profiles to pick from, and the copy into the sign-in browsers."""
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
        write_cookies(self.data / "Default", (".www.linkedin.com", "li_at"))
        write_cookies(self.data / "Profile 1", (".google.com", "SID"))
        write_cookies(self.data / "Profile 2", (".example.com", "other"))
        (self.data / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Person 1", "user_name": "a@example.com", "active_time": 1},
            "Profile 1": {"name": "Work", "active_time": 2},
            "Profile 2": {"name": "Empty", "active_time": 3},
        }}}))

    def test_lists_signed_in_profiles_linkedin_first(self) -> None:
        profiles = chrome_profiles(self.data, self.app)
        self.assertEqual([profile.folder for profile in profiles], ["Default", "Profile 1"])
        self.assertTrue(profiles[0].linkedin)
        self.assertTrue(profiles[1].google)

    def test_no_chrome_no_profiles(self) -> None:
        self.assertEqual(chrome_profiles(self.data, self.root / "missing.app"), [])

    def test_copy_keeps_what_was_there_and_leaves_the_caches(self) -> None:
        (self.data / "Default" / "Cache").mkdir()
        (self.data / "Default" / "Sessions").mkdir()
        (self.data / "Default" / "Login Data").write_text("saved")
        target = self.root / "browser-profiles" / "linkedin"
        target.mkdir(parents=True)
        (target / "old").write_text("kept")

        use_chrome_profile("Default", data=self.data, app=self.app, targets=(target,))

        self.assertEqual((target / "Default" / "Login Data").read_text(), "saved")
        self.assertTrue((target / "Local State").is_file())
        self.assertEqual(json.loads((target / COPIED_FROM).read_text())["folder"], "Default")
        self.assertFalse((target / "Default" / "Cache").exists())
        self.assertFalse((target / "Default" / "Sessions").exists())
        (backup,) = target.parent.glob("linkedin.bkup-*")
        self.assertEqual((backup / "old").read_text(), "kept")
