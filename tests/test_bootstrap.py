"""bin/bootstrap: the one command the install skill runs on the user's behalf.

Every case runs the real script with a fake HOME, a fake repo (a stub install.sh
that records its arguments) and stub tools on PATH. The contract under test is
the last line of output: DONE, NEEDS YOU, STOP or FAILED, so an agent can
act on it without reading anything else.
"""
from __future__ import annotations

import json
import os
import re
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
        write(self.repo / "packs/powerset/primitives/install/onboard.py",
              'import sys\nfrom pathlib import Path\n'
              'from packs.powerset.primitives.install.status import InstallStatus,InstallStep,InstallState\n'
              'root=Path(sys.argv[sys.argv.index("--root")+1])\n'
              'import os\npid=os.getpid()\n'
              'InstallStatus(root).write(step=InstallStep.READY,status=InstallState.COMPLETED,'
              'message="Account connected and search verified",pid=pid)\n'
              'print("DONE: Account connected and search verified")\n')
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
        proc = self.sandbox.run(env={"CLAUDE_CODE_REMOTE": "true"})
        self.assertEqual(proc.returncode, 3)
        self.assertTrue(last_line(proc).startswith("STOP: "))
        self.assertIn("Codex", last_line(proc))
        self.assertEqual(self.sandbox.installed(), [])

    def test_account_context_is_refreshed_after_onboarding(self) -> None:
        context = self.sandbox.repo / "profile-account.txt"
        write(self.sandbox.repo / "bin/agent-bootstrap",
              'from pathlib import Path\n'
              f'root=Path({str(self.sandbox.repo)!r})\n'
              f'Path({str(context)!r}).write_text((root/".env").read_text())\n')
        with (self.sandbox.repo / "packs/powerset/primitives/install/onboard.py").open("a") as handle:
            handle.write('\n(root/".env").write_text("ACCOUNT=casey@example.com\\n")\n')
        proc = self.sandbox.run("--powerset", "--harness", "codex")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(context.exists(), "Onboarding did not refresh user context")
        self.assertEqual(context.read_text(), "ACCOUNT=casey@example.com\n")

    def test_one_command_passes_source_account_and_history_to_onboarding(self) -> None:
        record = self.sandbox.root / "source-options.json"
        with (self.sandbox.repo / "packs/powerset/primitives/install/onboard.py").open("a") as handle:
            handle.write(f'\nimport json\nPath({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n')
        proc = self.sandbox.run("--powerset", "--harness", "codex", "--gmail-email", "personal@gmail.com",
                                "--sync-after", "2025-10-03", "--wacli-store", "/tmp/fresh-whatsapp", "--refresh")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = json.loads(record.read_text())
        self.assertEqual(argv[argv.index("--gmail-email") + 1], "personal@gmail.com")
        self.assertEqual(argv[argv.index("--sync-after") + 1], "2025-10-03")
        self.assertEqual(argv[argv.index("--wacli-store") + 1], "/tmp/fresh-whatsapp")
        self.assertIn("--refresh", argv)

    def test_installs_for_every_harness_found_and_reports_done(self) -> None:
        proc = self.sandbox.run(home_dirs=(".codex", ".claude"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.sandbox.installed()), ["claude-code", "codex"])
        self.assertTrue(last_line(proc).startswith("DONE: "))
        self.assertIn("STATUS PAGE: http://127.0.0.1:8765/install", proc.stdout)
        self.assertEqual(self.sandbox.progress()["status"], "completed")

    def test_no_harness_found_installs_both_and_says_so(self) -> None:
        proc = self.sandbox.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.sandbox.installed()), ["claude-code", "codex"])

    def test_explicit_harness_wins(self) -> None:
        proc = self.sandbox.run("--harness", "pi", home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.sandbox.installed(), ["pi"])

    def test_rerun_is_a_no_op_that_still_reports_done(self) -> None:
        first = self.sandbox.run(home_dirs=(".codex",))
        second = self.sandbox.run(home_dirs=(".codex",))
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertTrue(last_line(second).startswith("DONE: "))

    def test_powerset_flag_creates_env_once_and_never_overwrites_it(self) -> None:
        first = self.sandbox.run("--powerset", home_dirs=(".codex",))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        env_file = self.sandbox.repo / ".env"
        self.assertIn("POWERSET_API_URL=", env_file.read_text(encoding="utf-8"))
        env_file.write_text("KEEP=me\n", encoding="utf-8")
        self.sandbox.run("--powerset", home_dirs=(".codex",))
        self.assertEqual(env_file.read_text(encoding="utf-8"), "KEEP=me\n")

    def test_missing_developer_tools_asks_the_human_first(self) -> None:
        write(self.sandbox.bin / "xcode-select", "#!/usr/bin/env bash\nexit 2\n", executable=True)
        proc = self.sandbox.run(home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 10)
        self.assertTrue(last_line(proc).startswith("NEEDS YOU: "))
        self.assertIn("Install", last_line(proc))
        self.assertEqual(self.sandbox.installed(), [])

    def test_optional_tools_do_not_prompt_by_default(self) -> None:
        proc = self.sandbox.run(home_dirs=(".codex",))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(last_line(proc).startswith("DONE: "))
        self.assertNotIn("ASK:", proc.stdout)

    def test_installer_failure_is_persisted_with_recovery_log(self) -> None:
        write(self.sandbox.repo / "install.sh", '#!/usr/bin/env bash\necho "broken dependency"\nexit 1\n', executable=True)
        proc = self.sandbox.run("--harness", "codex")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        log_path = Path(self.sandbox.progress()["log_path"])
        self.assertIn("broken dependency", log_path.read_text())
        self.assertIn(str(log_path), proc.stdout)
        self.assertEqual(self.sandbox.progress()["retry_command"], "bin/bootstrap --harness codex")

    def test_unexpected_error_does_not_leave_running_progress(self) -> None:
        (self.sandbox.repo / "packs/powerset/templates/env.powerset.example").unlink()
        proc = self.sandbox.run("--powerset", "--harness", "codex")
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        self.assertIn("env.powerset.example", Path(self.sandbox.progress()["log_path"]).read_text())

    def test_page_start_failure_is_actionable_before_installing(self) -> None:
        write(self.sandbox.repo / "packs/ingestion/primitives/deep_context/review/cli.py",
              'import sys\nprint("port in use", file=sys.stderr)\nsys.exit(1)\n')
        proc = self.sandbox.run("--harness", "codex")
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
        proc = self.sandbox.run("--harness", "codex")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(record.read_text().splitlines(), ["dependencies", "skills"])
        self.assertEqual(self.sandbox.progress()["status"], "completed")

    def test_selected_status_port_is_sent_to_launcher_and_rerun(self) -> None:
        write(self.sandbox.repo / "packs/ingestion/primitives/deep_context/review/cli.py",
              'import json,sys\nport=sys.argv[sys.argv.index("--port")+1]\nprint(json.dumps({"url": f"http://127.0.0.1:{port}/install"}))\n')
        proc = self.sandbox.run("--port", "8876")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS PAGE: http://127.0.0.1:8876/install", proc.stdout)
        self.assertIn("--port 8876", self.sandbox.progress()["retry_command"])

    def test_terminated_install_is_paused_and_rerunnable(self) -> None:
        write(self.sandbox.repo / "install.sh", '#!/usr/bin/env bash\necho "install started"\nsleep 30\n', executable=True)
        proc = subprocess.Popen([str(BOOTSTRAP), "--harness", "codex"],
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
            self.assertTrue(stdout.rstrip().splitlines()[-1].startswith("NEEDS YOU: "))
            self.assertEqual(self.sandbox.progress()["status"], "waiting")
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
        proc = self.sandbox.run()
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(last_line(proc).startswith("FAILED: "))
        self.assertEqual(self.sandbox.progress()["status"], "failed")
        self.assertEqual(self.sandbox.progress()["step"], "runtime")

    def test_downloaded_bootstrap_hands_off_to_selected_release_contract(self) -> None:
        write(self.sandbox.repo / "bin/bootstrap",
              '#!/usr/bin/env bash\nprintf "DONE: prior-release-contract %s\\n" "$*"\n', executable=True)
        proc = subprocess.run(["bash", "-s", "--", "--harness", "codex"],
                              input=BOOTSTRAP.read_text(), capture_output=True, text=True,
                              env={"HOME": str(self.sandbox.home),
                                   "PATH": f"{self.sandbox.bin}:/usr/bin:/bin",
                                   "POWERPACKS_REPO_ROOT": str(self.sandbox.repo)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(last_line(proc), "DONE: prior-release-contract --harness codex")
        self.assertFalse((self.sandbox.repo / ".powerpacks/install/manifest.json").exists())
        self.assertEqual(self.sandbox.installed(), [])

    def test_hosted_onboarding_wait_preserves_human_action(self) -> None:
        write(self.sandbox.repo / "packs/powerset/primitives/install/onboard.py",
              'import sys\nfrom pathlib import Path\n'
              'from packs.powerset.primitives.install.status import InstallStatus,InstallStep,InstallState\n'
              'root=Path(sys.argv[sys.argv.index("--root")+1])\n'
              'import os\npid=os.getpid()\n'
              'InstallStatus(root).write(step=InstallStep.ACCOUNT,status=InstallState.WAITING,'
              'message="Waiting for account login",pid=pid)\n'
              'print("NEEDS YOU: Waiting for account login")\nsys.exit(10)\n')
        proc = self.sandbox.run("--powerset", "--harness", "codex")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertEqual(last_line(proc), "NEEDS YOU: Waiting for account login")
        self.assertEqual(self.sandbox.progress()["status"], "waiting")

    def test_hosted_error_preserves_message_and_visible_login_url(self) -> None:
        write(self.sandbox.repo / "packs/powerset/primitives/install/onboard.py",
              'import sys\nfrom pathlib import Path\n'
              'from packs.powerset.primitives.install.status import InstallStatus,InstallStep,InstallState\n'
              'root=Path(sys.argv[sys.argv.index("--root")+1])\n'
              'import os\npid=os.getpid()\n'
              'print("Open https://example.test/login", file=sys.stderr)\n'
              'InstallStatus(root).write(step=InstallStep.ACCOUNT,status=InstallState.FAILED,'
              'message="Account login timed out; ask me to reopen sign-in",pid=pid)\n'
              'print("FAILED: Account login timed out")\nsys.exit(1)\n')
        proc = self.sandbox.run("--powerset", "--harness", "codex")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Open https://example.test/login", proc.stderr)
        self.assertIn("Open https://example.test/login", Path(self.sandbox.progress()["log_path"]).read_text())
        self.assertEqual(self.sandbox.progress()["message"], "Account login timed out; ask me to reopen sign-in")

    def test_install_only_explicitly_skips_account_checks(self) -> None:
        proc = self.sandbox.run("--harness", "codex")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("No account was connected", last_line(proc))
        for step in ("account", "credentials", "connection", "network"):
            self.assertEqual(self.sandbox.progress()["steps"][step]["status"], "skipped")


class PublishedBootstrapTests(unittest.TestCase):
    """Real local Git releases and real Codex installer; runtime/server are stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sandbox = Sandbox(Path(self.tmp.name))
        self.source = self.sandbox.repo
        self.remote = self.sandbox.root / "published.git"
        self.checkout = self.sandbox.home / "powerpacks"
        self.env = {"HOME": str(self.sandbox.home), "PATH": f"{self.sandbox.bin}:/usr/bin:/bin",
                    "POWERPACKS_REPO_URL": str(self.remote), "POWERPACKS_SKIP_AGENT_BOOTSTRAP": "1"}
        for relative in ("bin/powerpacks-channel", "bin/install-skill", "install.sh", "adapters/codex/install.sh",
                         "bin/powerpacks-install-stamp", "bin/update-powerpacks"):
            write(self.source / relative, (ROOT / relative).read_text(), executable=True)
        for directory in ("docs", "templates", "config"):
            write(self.source / directory / ".keep", "")
        for script in ("build-local-duckdb-shim.py", "adopt-powerpacks-state.py", "fix-powerpacks-state.py"):
            write(self.source / "scripts" / script, "# fixture\n")
        write(self.source / "pyproject.toml", '[project]\nname="fixture"\nversion="1.0.0"\n')
        adapter = (ROOT / "adapters/codex/install.sh").read_text()
        for name, path in re.findall(r'install_skill (\S+) "\$REPO_ROOT/([^"]+)"', adapter):
            write(self.source / path, f"---\nname: {name}\n---\nFixture skill\n")
        write(self.source / ".gitignore", ".env\n.powerpacks/\n__pycache__/\n")
        write(self.source / "bin/bootstrap", '#!/usr/bin/env bash\necho "DONE: old release"\n', executable=True)
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "old release")
        self.git(self.source, "tag", "powerpacks-v1.0.0")
        self.git(self.sandbox.root, "init", "--bare", str(self.remote))
        self.git(self.source, "remote", "add", "origin", str(self.remote))
        self.git(self.source, "push", "origin", "main", "--tags")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
            env={**self.env, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
                 "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def publish(self, *, interruptible=False):
        write(self.source / "bin/bootstrap", BOOTSTRAP.read_text(), executable=True)
        write(self.source / "packs/search/skills/search/SKILL.md", "---\nname: search\n---\nCurrent release skill\n")
        if interruptible:
            write(self.source / "bin/setup-python",
                  '#!/usr/bin/env bash\nif [[ -f "$HOME/pause-install" ]]; then echo "paused fixture"; sleep 30; fi\n', executable=True)
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "new release")
        self.git(self.source, "tag", "powerpacks-v1.1.0")
        self.git(self.source, "push", "origin", "main", "--tags")
        return self.git(self.source, "rev-parse", "HEAD")

    def installed_old(self):
        self.git(self.sandbox.root, "clone", str(self.remote), str(self.checkout))
        self.git(self.checkout, "switch", "-c", "powerpacks-stable", "powerpacks-v1.0.0")

    def launch(self, **env):
        return subprocess.run(["bash", "-s", "--", "--harness", "codex"],
            input=BOOTSTRAP.read_text(), capture_output=True, text=True, env={**self.env, **env}, timeout=30)

    def test_fresh_clone_runs_selected_release_and_real_installer(self):
        expected = self.publish()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), expected)
        installed = self.sandbox.home / ".agents/skills/search/SKILL.md"
        self.assertIn("Current release skill", installed.read_text())
        self.assertNotIn("checking for the current", result.stdout)

    def test_existing_release_updates_and_rerun_preserves_user_files(self):
        self.installed_old()
        write(self.checkout / ".env", "CUSTOM_SETTING=keep\n")
        write(self.checkout / ".powerpacks/contacts.csv", "synthetic,data\n")
        custom_skill = self.sandbox.home / ".agents/skills/my-custom/SKILL.md"
        write(custom_skill, "my own skill")
        write(self.sandbox.home / ".codex/config.toml", "custom = true\n")
        expected = self.publish()
        for _ in range(2):
            result = self.launch()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), expected)
            self.assertEqual((self.checkout / ".env").read_text(), "CUSTOM_SETTING=keep\n")
            self.assertEqual((self.checkout / ".powerpacks/contacts.csv").read_text(), "synthetic,data\n")
            self.assertEqual(custom_skill.read_text(), "my own skill")
            self.assertEqual((self.sandbox.home / ".codex/config.toml").read_text(), "custom = true\n")
            self.assertIn("Current release skill", (self.sandbox.home / ".agents/skills/search/SKILL.md").read_text())

    def test_dirty_work_is_not_discarded_and_local_entry_does_not_update(self):
        self.installed_old()
        original = self.git(self.checkout, "rev-parse", "HEAD")
        self.publish()
        write(self.checkout / "packs/search/skills/search/SKILL.md", "local changes")
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(last_line(result).startswith("FAILED: "))
        self.assertIn("Local code changes", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), original)
        self.assertEqual((self.checkout / "packs/search/skills/search/SKILL.md").read_text(), "local changes")
        # Running an installed/development entrypoint is intentionally not an updater.
        result = subprocess.run([str(self.checkout / "bin/bootstrap")],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(last_line(result), "DONE: old release")

    def test_explicit_pin_and_edge_override_are_respected(self):
        self.installed_old()
        self.publish()
        result = self.launch(POWERPACKS_REF="powerpacks-v1.0.0")
        self.assertEqual(last_line(result), "DONE: old release")
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "powerpacks-pinned")
        result = self.launch(POWERPACKS_CHANNEL="edge")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "powerpacks-edge")

    def test_public_ssh_fetch_recovers_over_https_without_changing_origin(self):
        self.installed_old()
        expected = self.publish()
        ssh_origin = "git@github.com:powerset-co/powerpacks.git"
        self.git(self.checkout, "remote", "set-url", "origin", ssh_origin)
        self.git(self.checkout, "config", f"url.{self.remote}.insteadOf", "https://github.com/powerset-co/powerpacks.git")
        write(self.sandbox.bin / "git",
              '#!/usr/bin/env bash\n'
              'if [[ "$*" == *"fetch --quiet --tags origin"* ]]; then echo "mock SSH unavailable" >&2; exit 128; fi\n'
              'exec /usr/bin/git "$@"\n', executable=True)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), expected)
        self.assertEqual(self.git(self.checkout, "config", "remote.origin.url"), ssh_origin)

    def test_local_commits_and_colliding_untracked_files_are_preserved(self):
        self.installed_old()
        write(self.source / "docs/new-guide.md", "published guide")
        self.publish()
        local_file = self.checkout / "docs/new-guide.md"
        write(local_file, "my unpublished guide")
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(local_file.read_text(), "my unpublished guide")
        self.git(self.checkout, "add", "docs/new-guide.md")
        self.git(self.checkout, "commit", "-m", "local-only work")
        original = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(last_line(result).startswith("FAILED: "))
        self.assertIn("local commits", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), original)

    def test_interrupted_install_reruns_without_losing_data(self):
        self.installed_old()
        self.publish(interruptible=True)
        write(self.checkout / ".powerpacks/contacts.csv", "keep contacts")
        pause = self.sandbox.home / "pause-install"
        write(pause, "pause")
        launcher = self.sandbox.root / "downloaded-bootstrap"
        write(launcher, BOOTSTRAP.read_text(), executable=True)
        proc = subprocess.Popen([str(launcher), "--harness", "codex"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env, start_new_session=True)
        log = self.checkout / ".powerpacks/install/install.log"
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log.exists() and "paused fixture" in log.read_text():
                    break
                time.sleep(0.02)
            self.assertIn("paused fixture", log.read_text())
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=5)
            self.assertEqual(json.loads((log.parent / "manifest.json").read_text())["status"], "waiting")
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
        pause.unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / ".powerpacks/contacts.csv").read_text(), "keep contacts")
        self.assertEqual(json.loads((log.parent / "manifest.json").read_text())["status"], "completed")

if __name__ == "__main__":
    unittest.main()
