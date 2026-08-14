"""Contract tests for native Codex automation templates and installation."""

import importlib.util
import tempfile
import tomllib
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATIONS_DIR = REPO_ROOT / "automations"
INSTALLER_PATH = REPO_ROOT / "bin" / "install-codex-automation"
REFRESH_SKILL_PATH = (
    REPO_ROOT
    / "packs"
    / "ingestion"
    / "skills"
    / "refresh-message-sources"
    / "SKILL.md"
)


def load_installer():
    loader = SourceFileLoader("install_codex_automation", str(INSTALLER_PATH))
    spec = importlib.util.spec_from_file_location(
        "install_codex_automation",
        INSTALLER_PATH,
        loader=loader,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutomationPackageTests(unittest.TestCase):
    def test_packages_have_valid_native_contracts(self) -> None:
        package_dirs = sorted(
            path for path in AUTOMATIONS_DIR.iterdir()
            if path.is_dir()
        )
        self.assertTrue(package_dirs)

        for package_dir in package_dirs:
            with self.subTest(package=package_dir.name):
                automation = tomllib.loads(
                    (package_dir / "automation.toml").read_text(encoding="utf-8")
                )

                self.assertEqual(automation["version"], 1)
                self.assertEqual(automation["id"], package_dir.name)
                self.assertEqual(automation["kind"], "cron")
                self.assertIn(automation["status"], {"ACTIVE", "PAUSED"})
                self.assertTrue(automation["rrule"].startswith("RRULE:"))
                self.assertEqual(automation["execution_environment"], "local")
                self.assertEqual(automation["cwds"], ["${workspace}"])

    def test_installer_renders_workspace_status_and_timestamps(self) -> None:
        installer = load_installer()
        source = (
            AUTOMATIONS_DIR
            / "refresh-message-sources"
            / "automation.toml"
        )
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "Powerpacks checkout"
            workspace.mkdir()
            existing = Path(td) / "existing.toml"
            existing.write_text("created_at = 123\n", encoding="utf-8")

            automation_id, rendered = installer.render_automation(
                source,
                workspace,
                active=False,
                existing=existing,
                now_ms=456,
            )
            config = tomllib.loads(rendered)

        self.assertEqual(automation_id, "refresh-message-sources")
        self.assertEqual(config["cwds"], [str(workspace.resolve())])
        self.assertEqual(config["status"], "PAUSED")
        self.assertEqual(config["created_at"], 123)
        self.assertEqual(config["updated_at"], 456)

    def test_installer_writes_native_codex_file(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            workspace = temp_root / "workspace"
            workspace.mkdir()
            destination = installer.install(
                "refresh-message-sources",
                workspace,
                temp_root / "codex",
                active=True,
                dry_run=False,
            )
            config = tomllib.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(
            destination,
            temp_root
            / "codex"
            / "automations"
            / "refresh-message-sources"
            / "automation.toml",
        )
        self.assertEqual(config["status"], "ACTIVE")
        self.assertEqual(config["cwds"], [str(workspace.resolve())])

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
        self.assertEqual(prompt.strip(), "Run `$refresh-message-sources`.")

    def test_refresh_skill_owns_source_scope_snapshot_and_archive_gate(self) -> None:
        skill = REFRESH_SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("Automation ID: refresh-message-sources", skill)
        self.assertIn("Refresh message sources MM/DD/YY", skill)
        self.assertIn("$import-gmail sync", skill)
        self.assertIn("$import-messages sync", skill)
        self.assertIn("omit their fan-in", skill)
        self.assertIn("Never run Deep Context", skill)
        self.assertIn("status.py status", skill)
        self.assertIn("--output", skill)
        self.assertIn(
            ".powerpacks/automations/refresh-message-sources/latest.json",
            skill,
        )
        self.assertIn("codex_app__set_thread_archived", skill)
        self.assertIn("archived: true", skill)
        self.assertIn("exact metadata line is absent", skill)
        self.assertIn("do not call any archive action", skill)
        self.assertIn("Never substitute `/archive`, `codex archive`", skill)

    def test_import_skills_define_unattended_sync_contracts(self) -> None:
        gmail_skill = (
            REPO_ROOT
            / "packs"
            / "ingestion"
            / "skills"
            / "import-gmail"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        messages_skill = (
            REPO_ROOT
            / "packs"
            / "ingestion"
            / "skills"
            / "import-messages"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "Unattended sync mode — `$import-gmail sync`",
            gmail_skill,
        )
        self.assertIn("every stored account automatically", gmail_skill)
        self.assertIn("gmail: skipped_unconfigured", gmail_skill)
        self.assertIn("gmail: skipped_needs_user_action", gmail_skill)
        self.assertIn(
            "Unattended sync mode — `$import-messages sync`",
            messages_skill,
        )
        self.assertIn("messages: skipped_unconfigured", messages_skill)
        self.assertIn("messages: skipped_needs_user_action", messages_skill)
        self.assertIn("rerun it with `--confirm-import`", messages_skill)


if __name__ == "__main__":
    unittest.main()
