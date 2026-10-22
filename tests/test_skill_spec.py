"""Every skill stays on the Agent Skills spec (agentskills.io), so the standard
installers keep working on the repo as it is."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def skill_dirs() -> list[Path]:
    return sorted(path.parent for path in ROOT.glob("packs/*/skills/*/SKILL.md"))


def frontmatter(skill: Path) -> dict[str, str]:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    self_check = match is not None
    assert self_check, f"{skill.name}: no frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class SkillSpecTests(unittest.TestCase):
    def test_every_skill_meets_the_agent_skills_spec(self) -> None:
        self.assertEqual(len(skill_dirs()), 22)
        for skill in skill_dirs():
            with self.subTest(skill=skill.name):
                fields = frontmatter(skill)
                self.assertEqual(fields["name"], skill.name)
                self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(fields["name"]), 64)
                self.assertTrue(0 < len(fields["description"]) <= 1024)
                self.assertLessEqual(set(fields) - SPEC_FIELDS, set(), f"non-spec frontmatter: {set(fields) - SPEC_FIELDS}")


class StandardInstallTests(unittest.TestCase):
    def test_no_skill_depends_on_a_sibling_bundle_directory(self) -> None:
        # Standard installers copy or link only the skill folder; the runtime is
        # the checkout, so every command runs from the repo root.
        for skill in skill_dirs():
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("--project powerpacks python powerpacks/", text, skill.name)


if __name__ == "__main__":
    unittest.main()
