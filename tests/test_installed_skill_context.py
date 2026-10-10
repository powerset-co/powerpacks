"""Installed skills locate Powerpacks without relying on the chat's directory."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstalledSkillContextTests(unittest.TestCase):
    def test_adapters_install_usage_context_without_copying_another_runtime(self):
        for harness in ("codex", "claude-code", "pi"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                home = base / "home"
                skills = home / "skills"
                empty = base / "empty chat"
                empty.mkdir()
                paid = skills / "search/powerpacks/.powerpacks/paid.json"
                paid.parent.mkdir(parents=True)
                paid.write_text("preserve existing paid data\n")
                legacy = home / ".codex/powerpacks/.powerpacks/paid.json"
                legacy.parent.mkdir(parents=True)
                legacy.write_text("preserve legacy paid data\n")
                result = subprocess.run(
                    [str(ROOT / f"adapters/{harness}/install.sh"), str(skills)],
                    cwd=empty, text=True, capture_output=True,
                    env={**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
                         "POWERPACKS_SKIP_UV_SYNC": "1", "POWERPACKS_SKIP_AGENT_BOOTSTRAP": "1"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(paid.exists(), "Adapter discarded existing local data")
                self.assertEqual(paid.read_text(), "preserve existing paid data\n")
                self.assertEqual(legacy.read_text(), "preserve legacy paid data\n")
                self.assertIn(f"{ROOT}/AGENTS.md", (skills / "search/SKILL.md").read_text())
                for pack, name in (("search", "search"), ("search", "search-company"),
                                   ("powerset", "install-powerpacks"), ("ingestion", "deep-context")):
                    if harness == "pi" and name == "deep-context":
                        continue
                    for source in (ROOT / "packs" / pack / "skills" / name).glob("*.md"):
                        if source.name != "SKILL.md":
                            installed = skills / name / source.name
                            self.assertFalse(installed.is_symlink())
                            self.assertEqual(installed.read_bytes(), source.read_bytes())
                self.assertTrue((skills / "gtm/SKILL.md").exists())
                self.assertIn(f"{ROOT}/AGENTS.md", (skills / "gtm/SKILL.md").read_text())
                self.assertTrue((skills / "powerpacks-doctor/SKILL.md").exists())
                self.assertFalse((skills / "powerpacks-doctor/powerpacks").exists())
                self.assertFalse((home / ".codex/powerpacks/packs").exists())

    def test_skill_root_resolver_accepts_the_installed_worktree(self):
        text = (ROOT / "packs/powerset/skills/powerset/SKILL.md").read_text()
        resolver = text.split("resolve_powerpacks_root() {", 1)[1].split("\n}", 1)[0]
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                ["bash", "-c", "resolve_powerpacks_root() {" + resolver
                 + "\n}\nresolve_powerpacks_root"],
                cwd=ROOT, text=True, capture_output=True,
                env={"HOME": home, "PATH": os.environ["PATH"],
                     "POWERPACKS_REPO_ROOT": str(ROOT)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(ROOT))

    def test_shared_worker_updates_install_as_independent_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            checkout = base / "checkout"
            shared = checkout / "packs/shared/skills/tmux-worker.md"
            shared.parent.mkdir(parents=True)
            sources = []
            for name in ("search", "search-company"):
                source = checkout / "packs/search/skills" / name / "SKILL.md"
                source.parent.mkdir(parents=True)
                source.write_text(f"---\nname: {name}\ndescription: Search\n---\n\nRead tmux-worker.md.\n")
                (source.parent / "tmux-worker.md").symlink_to("../../../shared/skills/tmux-worker.md")
                sources.append(source)
            for content in ("Original worker instructions\n", "Updated worker instructions\n"):
                shared.write_text(content)
                for source in sources:
                    destination = base / "installed" / source.parent.name / "SKILL.md"
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "bin/install-skill"), str(checkout),
                         str(source), str(destination)], text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    installed = destination.parent / "tmux-worker.md"
                    self.assertFalse(installed.is_symlink())
                    self.assertEqual(installed.read_text(), content)
            shared.unlink()
            for source in sources:
                installed = base / "installed" / source.parent.name / "tmux-worker.md"
                self.assertEqual(installed.read_text(), "Updated worker instructions\n")

    def test_installed_skill_preserves_metadata_and_locates_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            checkout = base / "Powerpacks installation"
            unrelated = base / "empty chat"
            unrelated.mkdir()
            source = base / "source.md"
            destination = base / "skills/search/SKILL.md"
            metadata = "---\nname: search\ndescription: Find people\n---\n"
            source.write_text(metadata + "\n# Search\n\nRun the search.\n")
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/install-skill"), str(checkout),
                 str(source), str(destination)], cwd=unrelated,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = destination.read_text()
            self.assertTrue(installed.startswith(metadata))
            self.assertIn(f"{checkout}/AGENTS.md", installed)
            self.assertIn(f"{checkout}/.codex/AGENTS.md", installed)
            self.assertIn(f"Run commands from `{checkout}`", installed)
            self.assertIn("# Search\n\nRun the search.", installed)
            self.assertEqual(source.read_text(), metadata + "\n# Search\n\nRun the search.\n")


if __name__ == "__main__":
    unittest.main()
