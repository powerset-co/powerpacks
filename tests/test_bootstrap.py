"""bin/bootstrap: the one command the install skill runs on the user's behalf.

Every case runs the real script with a fake HOME, a fake repo (a stub install.sh
that records its arguments) and stub tools on PATH. The contract under test is
the last line of output: DONE, NEEDS YOU, ASK, STOP or FAILED, so an agent can
act on it without reading anything else.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
        root = root.resolve()
        self.root = root
        self.home = root / "home"
        self.repo = root / "repo"
        self.bin = root / "bin"
        self.record = root / "install-record"
        (self.home / "Library").mkdir(parents=True)
        write(self.repo / "bin/bootstrap", BOOTSTRAP.read_text(), executable=True)
        write(self.repo / "packs/.keep", "")
        for relative in (
            "packs/powerset/primitives/install/status.py",
            "packs/ingestion/primitives/common/manifests.py",
            "packs/ingestion/primitives/common/jsonio.py",
        ):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        write(self.repo / "packs/ingestion/primitives/deep_context/review/cli.py",
              'import json\nprint(json.dumps({"url": "http://127.0.0.1:8765/install"}))\n')
        write(self.repo / "bin/ensure-uv", f'#!/usr/bin/env bash\necho "{self.repo}/bin/uv"\n', executable=True)
        write(self.repo / "bin/uv", f'#!/usr/bin/env bash\n[[ "$2" == find ]] && echo "{sys.executable}"\nexit 0\n', executable=True)
        write(self.repo / "bin/setup-python", '#!/usr/bin/env bash\nexit 0\n', executable=True)
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

    def progress(self) -> dict:
        return json.loads((self.repo / ".powerpacks/install/manifest.json").read_text())


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
        self.assertIn("STATUS PAGE: http://127.0.0.1:8765/install", proc.stdout)
        self.assertEqual(self.sandbox.progress()["status"], "completed")

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
        self.assertEqual(self.sandbox.progress()["status"], "waiting")
        self.assertEqual(self.sandbox.progress()["step"], "tools")

    def test_tools_without_homebrew_asks_the_human_for_the_password_step(self) -> None:
        proc = self.sandbox.run("--tools", home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 10)
        self.assertTrue(last_line(proc).startswith("NEEDS YOU: "))
        self.assertIn("brew.sh", last_line(proc))
        self.assertEqual(self.sandbox.progress()["status"], "waiting")

    def test_installer_failure_is_persisted_with_recovery_log(self) -> None:
        write(self.sandbox.repo / "install.sh", '#!/usr/bin/env bash\necho "broken dependency"\nexit 1\n', executable=True)
        proc = self.sandbox.run("--no-tools", "--harness", "codex")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        log_path = Path(self.sandbox.progress()["log_path"])
        self.assertIn("broken dependency", log_path.read_text())
        self.assertIn(str(log_path), proc.stdout)
        self.assertEqual(self.sandbox.progress()["retry_command"], "bin/bootstrap --no-tools --harness codex")

    def test_unexpected_error_does_not_leave_running_progress(self) -> None:
        (self.sandbox.repo / "packs/powerset/templates/env.powerset.example").unlink()
        proc = self.sandbox.run("--no-tools", "--powerset", "--harness", "codex")
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        self.assertIn("env.powerset.example", Path(self.sandbox.progress()["log_path"]).read_text())

    def test_page_start_failure_is_actionable_before_installing(self) -> None:
        write(self.sandbox.repo / "packs/ingestion/primitives/deep_context/review/cli.py",
              'import sys\nprint("port in use", file=sys.stderr)\nsys.exit(1)\n')
        proc = self.sandbox.run("--no-tools", "--harness", "codex")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        self.assertIn("port in use", Path(self.sandbox.progress()["log_path"]).read_text())
        self.assertEqual(self.sandbox.installed(), [])

    def test_dependency_transitions_are_written_by_real_setup_python(self) -> None:
        shutil.copyfile(ROOT / "bin/setup-python", self.sandbox.repo / "bin/setup-python")
        (self.sandbox.repo / "bin/setup-python").chmod(0o755)
        record = self.sandbox.root / "phases"
        write(self.sandbox.repo / "bin/uv",
              f'''#!/usr/bin/env bash
if [[ "$2" == find ]]; then echo "{sys.executable}"; fi
if [[ "$1" == sync ]]; then
  "{sys.executable}" -c 'import json; print(json.load(open("{self.sandbox.repo}/.powerpacks/install/manifest.json"))["step"])' >>"{record}"
fi
exit 0
''', executable=True)
        write(self.sandbox.repo / "install.sh",
              f'''#!/usr/bin/env bash
set -euo pipefail
"{self.sandbox.repo}/bin/setup-python"
"{sys.executable}" -c 'import json; print(json.load(open("{self.sandbox.repo}/.powerpacks/install/manifest.json"))["step"])' >>"{record}"
''', executable=True)
        proc = self.sandbox.run("--no-tools", "--harness", "codex")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(record.read_text().splitlines(), ["dependencies", "skills"])
        self.assertEqual(self.sandbox.progress()["status"], "completed")

    def test_selected_status_port_is_sent_to_launcher_and_rerun(self) -> None:
        write(self.sandbox.repo / "packs/ingestion/primitives/deep_context/review/cli.py",
              'import json,sys\nport=sys.argv[sys.argv.index("--port")+1]\nprint(json.dumps({"url": f"http://127.0.0.1:{port}/install"}))\n')
        proc = self.sandbox.run("--no-tools", "--port", "8876")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS PAGE: http://127.0.0.1:8876/install", proc.stdout)
        self.assertIn("--port 8876", self.sandbox.progress()["retry_command"])

    def test_terminated_install_is_failed_and_rerunnable(self) -> None:
        write(self.sandbox.repo / "install.sh", '#!/usr/bin/env bash\necho "install started"\nsleep 30\n', executable=True)
        proc = subprocess.Popen([str(BOOTSTRAP), "--no-tools", "--harness", "codex"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True,
                                env={"HOME": str(self.sandbox.home),
                                     "PATH": f"{self.sandbox.bin}:/usr/bin:/bin",
                                     "POWERPACKS_REPO_ROOT": str(self.sandbox.repo)})
        log = self.sandbox.repo / ".powerpacks/install/install.log"
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                if log.exists() and "install started" in log.read_text():
                    break
                time.sleep(0.02)
            self.assertIn("install started", log.read_text())
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 143, stdout + stderr)
            self.assertTrue(stdout.rstrip().splitlines()[-1].startswith("FAILED: "))
            self.assertEqual(self.sandbox.progress()["status"], "failed")
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()

    def test_failed_dependencies_preserve_installed_skills(self) -> None:
        write(self.sandbox.repo / "bin/setup-python", '#!/usr/bin/env bash\necho "dependencies failed" >&2\nexit 1\n', executable=True)
        for harness in ("codex", "pi"):
            with self.subTest(harness=harness):
                adapter = self.sandbox.repo / f"adapters/{harness}/install.sh"
                adapter.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / f"adapters/{harness}/install.sh", adapter)
                adapter.chmod(0o755)
                installed = self.sandbox.root / harness / "search/SKILL.md"
                write(installed, "existing installed skill")
                proc = subprocess.run([str(adapter), str(installed.parent.parent)],
                                      capture_output=True, text=True,
                                      env={"HOME": str(self.sandbox.home), "PATH": "/usr/bin:/bin"})
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(installed.read_text(), "existing installed skill")

    def test_failed_runtime_discovery_keeps_python_for_failure_reporting(self) -> None:
        write(self.sandbox.repo / "bin/uv", '#!/usr/bin/env bash\n[[ "$2" == find ]] && exit 1\nexit 0\n', executable=True)
        proc = self.sandbox.run("--no-tools")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        self.assertEqual(self.sandbox.progress()["step"], "runtime")

    def test_downloaded_bootstrap_hands_off_to_selected_release_contract(self) -> None:
        write(self.sandbox.repo / "bin/bootstrap",
              '#!/usr/bin/env bash\nprintf "DONE: prior-release-contract %s\\n" "$*"\n', executable=True)
        proc = subprocess.run(["bash", "-s", "--", "--no-tools", "--harness", "codex"],
                              input=BOOTSTRAP.read_text(), capture_output=True, text=True,
                              env={"HOME": str(self.sandbox.home),
                                   "PATH": f"{self.sandbox.bin}:/usr/bin:/bin",
                                   "POWERPACKS_REPO_ROOT": str(self.sandbox.repo)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(last_line(proc), "DONE: prior-release-contract --no-tools --harness codex")
        self.assertFalse((self.sandbox.repo / ".powerpacks/install/manifest.json").exists())
        self.assertEqual(self.sandbox.installed(), [])

if __name__ == "__main__":
    unittest.main()
