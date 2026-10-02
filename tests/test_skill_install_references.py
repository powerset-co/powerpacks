"""Installed search references resolve without repository-relative skill paths."""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = {
    "search": "search",
    "search-company": "search",
    "search-sql": "search",
    "search-contacts": "contacts",
}


class SkillInstallReferencesTests(unittest.TestCase):
    def test_search_markdown_and_sibling_references_are_installed(self) -> None:
        for harness in ("codex", "claude-code", "pi"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                installed = root / "skills"
                bundle = root / "bundle"
                for directory in ("docs", "templates", "config", "scripts"):
                    (repo / directory).mkdir(parents=True)
                (repo / "pyproject.toml").write_text("")
                for name in (
                    "build-local-duckdb-shim.py",
                    "adopt-powerpacks-state.py",
                    "fix-powerpacks-state.py",
                ):
                    (repo / "scripts" / name).write_text("")
                for skill, pack in SKILLS.items():
                    relative = Path("packs") / pack / "skills" / skill
                    (repo / relative).mkdir(parents=True)
                    for source in (ROOT / relative).glob("*.md"):
                        shutil.copyfile(source, repo / relative / source.name)

                # Execute the real packaging functions, excluding global skill
                # cleanup, Python setup, and bootstrap side effects.
                script = (ROOT / "adapters" / harness / "install.sh").read_text()
                functions = re.findall(r"^\w+\(\) \{\n.*?^\}", script, re.M | re.S)
                commands = [
                    "set -euo pipefail",
                    f"REPO_ROOT={shlex.quote(str(repo))}",
                    f"SKILLS_DIR={shlex.quote(str(installed))}",
                    f"BUNDLE_DIR={shlex.quote(str(bundle))}",
                    *functions,
                ]
                if harness == "codex":
                    commands.append("install_powerpacks_bundle")
                for skill, pack in SKILLS.items():
                    commands.append(
                        f'install_skill {skill} "$REPO_ROOT/packs/{pack}/skills/{skill}/SKILL.md"'
                    )
                subprocess.run(["bash", "-c", "\n".join(commands)], check=True, capture_output=True)

                for skill, pack in SKILLS.items():
                    source_dir = ROOT / "packs" / pack / "skills" / skill
                    for source in source_dir.glob("*.md"):
                        self.assertEqual((installed / skill / source.name).read_bytes(), source.read_bytes())
                self.assertTrue((installed / "search" / "deep-mode.md").is_file())
                for sibling in ("search-company", "search-sql", "search-contacts"):
                    self.assertTrue((installed / "search" / ".." / sibling / "SKILL.md").is_file())
                runtime = installed / "search" / "powerpacks"
                self.assertEqual(runtime.is_symlink(), harness == "codex")
                self.assertFalse(list((runtime / "packs").rglob("SKILL.md")))


if __name__ == "__main__":
    unittest.main()
