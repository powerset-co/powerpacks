"""The Chrome connections read keeps the export newest-first and asks only for new rows."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.discover.linkedin.connections import LinkedInConnections
from packs.ingestion.primitives.setup.automations.shell import CommandResult

MODULE = "packs.ingestion.primitives.discover.linkedin.connections"
EXPORT = (
    "Notes:\n\"Exported from LinkedIn\"\n\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Casey,Lane,https://www.linkedin.com/in/casey-lane,casey@example.com,Example Co,Engineer,01 Jan 2026\n"
)


class LinkedInConnectionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.csv = Path(temp.name) / "discover/linkedin/Connections.csv"
        self.known = []
        deps = patch(f"{MODULE}.ensure_playwright_core", return_value={"status": "ok", "node_path": "/tmp/node_modules"})
        deps.start()
        self.addCleanup(deps.stop)

    def scrape(self, browser_payload):
        def browser(command, *, timeout, env):
            known_file = command[command.index("--known-file") + 1]
            self.known = json.loads(Path(known_file).read_text())
            self.flags = {flag: command[command.index(flag) + 1] for flag in ("--stop-after-known", "--max-loads")}
            return CommandResult(ok=True, stdout=json.dumps(browser_payload))
        with patch(f"{MODULE}.run_streaming_command", side_effect=browser):
            return LinkedInConnections(csv_path=self.csv, profile_dir=self.csv.parent / "profile").run()

    def rows(self):
        return self.csv.read_text(encoding="utf-8").splitlines()

    def test_first_read_writes_the_export_columns(self):
        result = self.scrape({"status": "ok", "owner_slug": "casey-owner", "loads": 3, "stopped": "end", "connections": [
            {"slug": "jordan-bravo", "name": "Jordan Bravo", "headline": "Founder at Example", "connected_on": "October 2, 2026"},
        ]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["connections"], result["added"]), (1, 1))
        self.assertEqual(self.known, [])
        self.assertEqual(self.rows(), [
            "First Name,Last Name,URL,Email Address,Company,Position,Connected On",
            "Jordan,Bravo,https://www.linkedin.com/in/jordan-bravo,,,Founder at Example,\"October 2, 2026\"",
        ])

    def test_rerun_sends_known_people_and_puts_new_ones_on_top_of_the_existing_export(self):
        self.csv.parent.mkdir(parents=True)
        self.csv.write_text(EXPORT, encoding="utf-8")
        result = self.scrape({"status": "ok", "owner_slug": "casey-owner", "loads": 1, "stopped": "known", "connections": [
            {"slug": "jordan-bravo", "name": "Jordan Bravo", "headline": "Founder", "connected_on": ""},
            {"slug": "casey-lane", "name": "Casey Lane", "headline": "Engineer", "connected_on": ""},
        ]})
        self.assertEqual(self.known, ["casey-lane"])
        self.assertEqual((result["connections"], result["added"]), (2, 1))
        rows = self.rows()
        self.assertTrue(rows[1].startswith("Jordan,Bravo,https://www.linkedin.com/in/jordan-bravo"))
        self.assertIn("casey@example.com", rows[2])

    def manifest(self):
        return json.loads(self.csv.with_name("connections.json").read_text())

    def test_capped_run_says_the_rest_keeps_syncing_and_the_next_run_goes_deeper(self):
        cards = [{"slug": f"person-{n}", "name": f"Person {n}", "headline": "", "connected_on": ""} for n in range(3)]
        first = self.scrape({"status": "ok", "owner_slug": "casey-owner", "loads": 300, "stopped": "limit", "connections": cards[:2]})
        self.assertEqual(self.flags, {"--stop-after-known": "25", "--max-loads": "300"})
        self.assertFalse(first["complete"])
        self.assertIn("keep syncing on your next run", first["message"])
        self.assertEqual((self.manifest()["complete"], self.manifest()["loads"]), (False, 300))

        second = self.scrape({"status": "ok", "owner_slug": "casey-owner", "loads": 320, "stopped": "end", "connections": cards})
        self.assertEqual(self.flags, {"--stop-after-known": "0", "--max-loads": "600"})
        self.assertEqual((second["connections"], second["added"], second["complete"]), (3, 1, True))
        self.assertNotIn("next run", second["message"])

        self.scrape({"status": "ok", "owner_slug": "casey-owner", "loads": 1, "stopped": "known", "connections": cards[:1]})
        self.assertEqual(self.flags, {"--stop-after-known": "25", "--max-loads": "300"})
        self.assertTrue(self.manifest()["complete"])

    def test_nothing_new_leaves_the_csv_alone_and_records_the_owner(self):
        self.csv.parent.mkdir(parents=True)
        self.csv.write_text(EXPORT, encoding="utf-8")
        before = self.csv.stat().st_mtime_ns
        result = self.scrape({"status": "ok", "owner_slug": "casey-owner", "loads": 1, "stopped": "known", "connections": [
            {"slug": "Casey-Lane", "name": "Casey Lane", "headline": "Engineer", "connected_on": ""},
        ]})
        self.assertEqual((result["added"], result["connections"]), (0, 1))
        self.assertEqual(self.csv.stat().st_mtime_ns, before)
        self.assertEqual(self.manifest()["owner_url"], "https://www.linkedin.com/in/casey-owner")

    def test_login_not_finished_waits_and_keeps_the_existing_export(self):
        self.csv.parent.mkdir(parents=True)
        self.csv.write_text(EXPORT, encoding="utf-8")
        result = self.scrape({"status": "needs_user_action", "message": "Log in to LinkedIn in the Chrome window Powerpacks opened."})
        self.assertEqual(result["status"], "needs_user_action")
        self.assertEqual(self.csv.read_text(encoding="utf-8"), EXPORT)

    def test_browser_error_fails_without_writing(self):
        result = self.scrape({"status": "error", "message": "chrome is not installed"})
        self.assertEqual(result, {"status": "failed", "message": "chrome is not installed"})
        self.assertFalse(self.csv.exists())


if __name__ == "__main__":
    unittest.main()
