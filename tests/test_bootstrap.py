"""bin/bootstrap: the one command the install skill runs on the user's behalf.

Every case runs the real script with a fake HOME, a fake repo (a stub install.sh
that records its arguments) and stub tools on PATH. The contract under test is
the last line of output: DONE, NEEDS YOU, ASK, STOP or FAILED, so an agent can
act on it without reading anything else.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bin/bootstrap"


def write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


class Sandbox:
    """A fake Mac: HOME, a repo with a stub installer, and a PATH of stub tools."""

    def __init__(self, root: Path, tools: tuple[str, ...] = ()) -> None:
        self.root = root
        self.home = root / "home"
        self.repo = root / "repo"
        self.bin = root / "bin"
        self.record = root / "install-record"
        (self.home / "Library").mkdir(parents=True)
        write(self.repo / "packs/.keep", "")
        write(
            self.repo / "install.sh",
            f'#!/usr/bin/env bash\nset -euo pipefail\necho "$1" >> "{self.record}"\n',
            executable=True,
        )
        write(self.repo / "packs/powerset/templates/env.powerset.example", "POWERSET_API_URL=https://example.com\n")
        # xcode-select present means git is available; the real one stays off PATH.
        write(self.bin / "xcode-select", "#!/usr/bin/env bash\n[[ \"$1\" == -p ]] && echo /Library/Developer/CommandLineTools\n", executable=True)
        write(self.bin / "uname", "#!/usr/bin/env bash\necho Darwin\n", executable=True)
        for tool in tools:
            write(self.bin / tool, "#!/usr/bin/env bash\nexit 0\n", executable=True)

    def run(self, *args: str, home_dirs: tuple[str, ...] = (), env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        for name in home_dirs:
            (self.home / name).mkdir(parents=True, exist_ok=True)
        full_env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "POWERPACKS_REPO_ROOT": str(self.repo),
            **(env or {}),
        }
        return subprocess.run([str(BOOTSTRAP), *args], capture_output=True, text=True, env=full_env, check=False)

    def installed(self) -> list[str]:
        return self.record.read_text(encoding="utf-8").split() if self.record.exists() else []


def last_line(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout.rstrip("\n").splitlines()[-1] if proc.stdout.strip() else ""


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sandbox = Sandbox(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cloud_session_stops_before_touching_anything(self) -> None:
        proc = self.sandbox.run("--no-tools", env={"CLAUDE_CODE_REMOTE": "true"})
        self.assertEqual(proc.returncode, 3)
        self.assertTrue(last_line(proc).startswith("STOP: "))
        self.assertIn("Codex", last_line(proc))
        self.assertEqual(self.sandbox.installed(), [])

    def test_installs_for_every_harness_found_and_reports_done(self) -> None:
        proc = self.sandbox.run("--no-tools", home_dirs=(".codex", ".claude"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.sandbox.installed()), ["claude-code", "codex"])
        self.assertTrue(last_line(proc).startswith("DONE: "))

    def test_no_harness_found_installs_both_and_says_so(self) -> None:
        proc = self.sandbox.run("--no-tools")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.sandbox.installed()), ["claude-code", "codex"])

    def test_explicit_harness_wins(self) -> None:
        proc = self.sandbox.run("--no-tools", "--harness", "pi", home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.sandbox.installed(), ["pi"])

    def test_rerun_is_a_no_op_that_still_reports_done(self) -> None:
        first = self.sandbox.run("--no-tools", home_dirs=(".codex",))
        second = self.sandbox.run("--no-tools", home_dirs=(".codex",))
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertTrue(last_line(second).startswith("DONE: "))

    def test_powerset_flag_creates_env_once_and_never_overwrites_it(self) -> None:
        first = self.sandbox.run("--no-tools", "--powerset", home_dirs=(".codex",))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        env_file = self.sandbox.repo / ".env"
        self.assertIn("POWERSET_API_URL=", env_file.read_text(encoding="utf-8"))
        env_file.write_text("KEEP=me\n", encoding="utf-8")
        self.sandbox.run("--no-tools", "--powerset", home_dirs=(".codex",))
        self.assertEqual(env_file.read_text(encoding="utf-8"), "KEEP=me\n")

    def test_missing_developer_tools_asks_the_human_first(self) -> None:
        write(self.sandbox.bin / "xcode-select", "#!/usr/bin/env bash\nexit 2\n", executable=True)
        proc = self.sandbox.run("--no-tools", home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 10)
        self.assertTrue(last_line(proc).startswith("NEEDS YOU: "))
        self.assertIn("Install", last_line(proc))
        self.assertEqual(self.sandbox.installed(), [])

    def test_missing_tools_are_listed_as_a_question_not_installed(self) -> None:
        proc = self.sandbox.run(home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(last_line(proc).startswith("ASK: "))
        self.assertIn("msgvault", last_line(proc))
        self.assertIn("--tools", last_line(proc))
        self.assertEqual(self.sandbox.installed(), ["codex"])

    def test_tools_without_homebrew_asks_the_human_for_the_password_step(self) -> None:
        proc = self.sandbox.run("--tools", home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 10)
        self.assertTrue(last_line(proc).startswith("NEEDS YOU: "))
        self.assertIn("brew.sh", last_line(proc))

if __name__ == "__main__":
    unittest.main()
