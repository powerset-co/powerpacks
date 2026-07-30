"""Contract tests for portable Codex automation packages."""

import json
import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATIONS_DIR = REPO_ROOT / "automations"


class AutomationPackageTests(unittest.TestCase):
    def test_packages_have_matching_native_and_portable_contracts(self) -> None:
        package_dirs = sorted(
            path for path in AUTOMATIONS_DIR.iterdir()
            if path.is_dir()
        )
        self.assertTrue(package_dirs)

        for package_dir in package_dirs:
            with self.subTest(package=package_dir.name):
                manifest = json.loads(
                    (package_dir / "codex-automation.json").read_text(encoding="utf-8")
                )
                automation = tomllib.loads(
                    (package_dir / "automation.toml").read_text(encoding="utf-8")
                )

                self.assertEqual(manifest["schemaVersion"], 1)
                self.assertEqual(manifest["install"]["suggestedId"], package_dir.name)
                self.assertEqual(automation["version"], 1)
                self.assertEqual(automation["id"], package_dir.name)
                self.assertEqual(automation["kind"], "cron")
                self.assertIn(automation["status"], {"ACTIVE", "PAUSED"})
                self.assertEqual(manifest["install"]["defaultStatus"], "PAUSED")
                self.assertTrue(automation["rrule"].startswith("RRULE:"))
                self.assertEqual(automation["execution_environment"], "local")
                self.assertEqual(automation["cwds"], ["${workspace}"])

    def test_message_refresh_has_required_sources_and_safety_boundaries(self) -> None:
        automation = tomllib.loads(
            (
                AUTOMATIONS_DIR
                / "refresh-message-sources"
                / "automation.toml"
            ).read_text(encoding="utf-8")
        )
        prompt = automation["prompt"]

        self.assertEqual(automation["status"], "ACTIVE")
        self.assertIn("$import-gmail", prompt)
        self.assertIn("$import-messages", prompt)
        self.assertIn("both iMessage and WhatsApp", prompt)
        self.assertIn("three-year window", prompt)
        self.assertIn("do not enrich or process", prompt)
        self.assertIn("Never call paid providers", prompt)
        self.assertIn("status.py status", prompt)
        self.assertIn("--output", prompt)
        self.assertIn(
            ".powerpacks/automations/refresh-message-sources/latest.json",
            prompt,
        )


if __name__ == "__main__":
    unittest.main()
