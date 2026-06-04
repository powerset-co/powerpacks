import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import plistlib


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/codex-heartbeat-runner.py"
HEARTBEAT = ROOT / "scripts/codex-heartbeat.sh"
LAUNCHD = ROOT / "scripts/install-codex-heartbeat-launchd.sh"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def fake_path(bin_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env.pop("OPENAI_API_KEY", None)
    return env


class CodexHeartbeatSchedulerTests(unittest.TestCase):
    def test_runner_not_due_does_not_invoke_codex_or_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            (fake_bin / "codex").write_text("#!/usr/bin/env bash\necho should-not-run >&2\nexit 99\n")
            (fake_bin / "codex").chmod(0o755)
            last_success = time.time()
            write_json(config, {"enabled": True, "interval_seconds": 3600, "prompt": "should not run"})
            write_json(state, {"last_success_epoch": last_success, "last_success_at": "now"})

            before = state.read_text()
            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state)],
                cwd=ROOT,
                env=fake_path(fake_bin),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("not due", proc.stdout)
            self.assertEqual(state.read_text(), before)

    def test_runner_due_invokes_codex_and_records_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo fake-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            write_json(config, {"enabled": True, "interval_seconds": 1, "prompt": "run me"})
            write_json(state, {"last_success_epoch": time.time() - 10})

            env = fake_path(fake_bin)
            env["CODEX_LOG"] = str(codex_log)
            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("fake-codex-ok", proc.stdout)
            self.assertIn("exec\nrun me", codex_log.read_text())
            updated_state = json.loads(state.read_text())
            self.assertEqual(updated_state["last_exit_code"], 0)
            self.assertIn("last_success_epoch", updated_state)

    def test_runner_multi_task_config_runs_only_due_enabled_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo fake-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            now = time.time()
            write_json(
                config,
                {
                    "enabled": True,
                    "interval_seconds": 3600,
                    "retry_interval_seconds": 900,
                    "tasks": [
                        {"id": "due-a", "interval_seconds": 1, "prompt": "prompt a"},
                        {"id": "not-due-b", "interval_seconds": 3600, "prompt": "prompt b"},
                        {"id": "disabled-c", "enabled": False, "prompt": "prompt c"},
                    ],
                },
            )
            write_json(
                state,
                {
                    "tasks": {
                        "due-a": {"last_success_epoch": now - 10},
                        "not-due-b": {"last_success_epoch": now},
                    }
                },
            )

            env = fake_path(fake_bin)
            env["CODEX_LOG"] = str(codex_log)
            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            log = codex_log.read_text()
            self.assertIn("exec\nprompt a", log)
            self.assertNotIn("prompt b", log)
            self.assertNotIn("prompt c", log)
            updated = json.loads(state.read_text())
            self.assertEqual(updated["tasks"]["due-a"]["last_exit_code"], 0)
            self.assertNotIn("last_exit_code", updated["tasks"]["not-due-b"])

    def test_runner_task_can_resume_explicit_codex_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo fake-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            write_json(
                config,
                {
                    "tasks": [
                        {
                            "id": "resume-task",
                            "interval_seconds": 1,
                            "session": {"mode": "resume-id", "id": "session-123"},
                            "prompt": "continue task",
                        }
                    ]
                },
            )
            write_json(state, {"tasks": {"resume-task": {"last_success_epoch": time.time() - 10}}})
            env = fake_path(fake_bin)
            env["CODEX_LOG"] = str(codex_log)
            proc = subprocess.run([str(RUNNER), "--config", str(config), "--state", str(state)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("exec\nresume\nsession-123\ncontinue task", codex_log.read_text())

    def test_runner_task_can_resume_last_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo fake-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            write_json(
                config,
                {
                    "tasks": [
                        {
                            "id": "resume-last-task",
                            "interval_seconds": 1,
                            "session": {"mode": "resume-last", "all": True},
                            "prompt": "continue last task",
                        }
                    ]
                },
            )
            write_json(state, {"tasks": {"resume-last-task": {"last_success_epoch": time.time() - 10}}})
            env = fake_path(fake_bin)
            env["CODEX_LOG"] = str(codex_log)
            proc = subprocess.run([str(RUNNER), "--config", str(config), "--state", str(state)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("exec\nresume\n--last\n--all\ncontinue last task", codex_log.read_text())

    def test_runner_multi_task_max_tasks_per_tick_drains_one_due_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo fake-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            write_json(
                config,
                {
                    "max_tasks_per_tick": 1,
                    "tasks": [
                        {"id": "due-a", "interval_seconds": 1, "prompt": "prompt a"},
                        {"id": "due-b", "interval_seconds": 1, "prompt": "prompt b"},
                    ],
                },
            )
            write_json(
                state,
                {
                    "tasks": {
                        "due-a": {"last_success_epoch": time.time() - 10},
                        "due-b": {"last_success_epoch": time.time() - 10},
                    }
                },
            )

            env = fake_path(fake_bin)
            env["CODEX_LOG"] = str(codex_log)
            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            log = codex_log.read_text()
            self.assertIn("prompt a", log)
            self.assertNotIn("prompt b", log)
            updated = json.loads(state.read_text())
            self.assertIn("last_success_epoch", updated["tasks"]["due-a"])
            self.assertNotIn("last_exit_code", updated["tasks"]["due-b"])

    def test_record_attempt_marks_only_max_tasks_per_tick(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            write_json(
                config,
                {
                    "max_tasks_per_tick": 1,
                    "tasks": [
                        {"id": "due-a", "interval_seconds": 1, "prompt": "prompt a"},
                        {"id": "due-b", "interval_seconds": 1, "prompt": "prompt b"},
                    ],
                },
            )
            write_json(
                state,
                {
                    "tasks": {
                        "due-a": {"last_success_epoch": time.time() - 10},
                        "due-b": {"last_success_epoch": time.time() - 10},
                    }
                },
            )
            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state), "--record-attempt"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            recorded = json.loads(state.read_text())
            self.assertEqual(recorded["tasks"]["due-a"]["last_exit_code"], 125)
            self.assertNotIn("last_exit_code", recorded["tasks"]["due-b"])

    def test_include_pending_does_not_bypass_real_failure_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo fake-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            now = time.time()
            write_json(
                config,
                {
                    "tasks": [
                        {"id": "failed-a", "interval_seconds": 1, "retry_interval_seconds": 900, "prompt": "failed prompt"},
                        {"id": "pending-b", "interval_seconds": 1, "retry_interval_seconds": 900, "prompt": "pending prompt"},
                    ],
                },
            )
            write_json(
                state,
                {
                    "tasks": {
                        "failed-a": {
                            "last_success_epoch": now - 3600,
                            "last_attempt_epoch": now,
                            "last_exit_code": 1,
                        },
                        "pending-b": {
                            "last_success_epoch": now - 3600,
                            "last_attempt_epoch": now,
                            "last_exit_code": 125,
                        },
                    }
                },
            )
            env = fake_path(fake_bin)
            env["CODEX_LOG"] = str(codex_log)
            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state), "--include-pending"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            log = codex_log.read_text()
            self.assertNotIn("failed prompt", log)
            self.assertIn("pending prompt", log)
            updated = json.loads(state.read_text())
            self.assertEqual(updated["tasks"]["failed-a"]["last_exit_code"], 1)
            self.assertEqual(updated["tasks"]["pending-b"]["last_exit_code"], 0)

    def test_runner_retry_backoff_prevents_repeated_failed_spend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            (fake_bin / "codex").write_text("#!/usr/bin/env bash\necho should-not-retry >&2\nexit 99\n")
            (fake_bin / "codex").chmod(0o755)
            write_json(config, {"enabled": True, "interval_seconds": 1, "retry_interval_seconds": 900, "prompt": "retry"})
            write_json(
                state,
                {
                    "last_success_epoch": time.time() - 3600,
                    "last_attempt_epoch": time.time(),
                    "last_exit_code": 1,
                },
            )

            proc = subprocess.run(
                [str(RUNNER), "--config", str(config), "--state", str(state)],
                cwd=ROOT,
                env=fake_path(fake_bin),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("retry backoff", proc.stdout)

    def test_shell_once_not_due_skips_auth_install_and_codex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_repo = tmp / "fake-powerpacks"
            (fake_repo / "scripts").mkdir(parents=True)
            (fake_repo / "scripts" / "codex-heartbeat-runner.py").write_text(RUNNER.read_text(), encoding="utf-8")
            (fake_repo / "scripts" / "codex-heartbeat-runner.py").chmod(0o755)
            (fake_repo / "install.sh").write_text("#!/usr/bin/env bash\necho install-should-not-run >&2\nexit 99\n")
            (fake_repo / "install.sh").chmod(0o755)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            (fake_bin / "codex").write_text("#!/usr/bin/env bash\necho codex-should-not-run >&2\nexit 99\n")
            (fake_bin / "codex").chmod(0o755)
            write_json(config, {"enabled": True, "interval_seconds": 3600, "prompt": "noop"})
            write_json(state, {"last_success_epoch": time.time()})

            env = fake_path(fake_bin)
            env.update(
                {
                    "POWERPACKS_REPO_ROOT": str(fake_repo),
                    "POWERPACKS_HEARTBEAT_CONFIG": str(config),
                    "POWERPACKS_HEARTBEAT_STATE": str(state),
                    "POWERPACKS_HEARTBEAT_SKIP_INSTALL": "0",
                    "HEARTBEAT_ONCE": "1",
                }
            )
            proc = subprocess.run(
                [str(HEARTBEAT), "--once"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("not due", proc.stdout)
            self.assertNotIn("installing/updating", proc.stdout)
            self.assertNotIn("install-should-not-run", proc.stderr)
            self.assertNotIn("codex-should-not-run", proc.stderr)

    def test_shell_prep_failure_records_backoff_before_next_poll(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_repo = tmp / "fake-powerpacks"
            (fake_repo / "scripts").mkdir(parents=True)
            (fake_repo / "scripts" / "codex-heartbeat-runner.py").write_text(RUNNER.read_text(), encoding="utf-8")
            (fake_repo / "scripts" / "codex-heartbeat-runner.py").chmod(0o755)
            install_count = tmp / "install-count"
            (fake_repo / "install.sh").write_text(
                "#!/usr/bin/env bash\n"
                "count=$(cat \"$INSTALL_COUNT\" 2>/dev/null || echo 0)\n"
                "echo $((count + 1)) > \"$INSTALL_COUNT\"\n"
                "echo install-failed >&2\n"
                "exit 42\n"
            )
            (fake_repo / "install.sh").chmod(0o755)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            (fake_bin / "codex").write_text("#!/usr/bin/env bash\necho codex-should-not-run >&2\nexit 99\n")
            (fake_bin / "codex").chmod(0o755)
            write_json(config, {"enabled": True, "interval_seconds": 1, "retry_interval_seconds": 900, "prompt": "due"})
            write_json(state, {"last_success_epoch": time.time() - 3600})

            env = fake_path(fake_bin)
            env.update(
                {
                    "POWERPACKS_REPO_ROOT": str(fake_repo),
                    "POWERPACKS_HEARTBEAT_CONFIG": str(config),
                    "POWERPACKS_HEARTBEAT_STATE": str(state),
                    "POWERPACKS_SYNC_HOST_CODEX_HOME": "0",
                    "INSTALL_COUNT": str(install_count),
                }
            )
            first = subprocess.run([str(HEARTBEAT), "--once"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 42)
            self.assertIn("install-failed", first.stderr)
            recorded = json.loads(state.read_text())
            self.assertEqual(recorded["last_exit_code"], 42)
            self.assertIn("heartbeat preparation failed", recorded["last_due_reason"])

            second = subprocess.run([str(HEARTBEAT), "--once"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("retry backoff", second.stdout)
            self.assertEqual(install_count.read_text().strip(), "1")

    def test_shell_successful_due_run_overwrites_pending_attempt_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_repo = tmp / "fake-powerpacks"
            (fake_repo / "scripts").mkdir(parents=True)
            (fake_repo / "scripts" / "codex-heartbeat-runner.py").write_text(RUNNER.read_text(), encoding="utf-8")
            (fake_repo / "scripts" / "codex-heartbeat-runner.py").chmod(0o755)
            install_count = tmp / "install-count"
            (fake_repo / "install.sh").write_text(
                "#!/usr/bin/env bash\n"
                "count=$(cat \"$INSTALL_COUNT\" 2>/dev/null || echo 0)\n"
                "echo $((count + 1)) > \"$INSTALL_COUNT\"\n"
                "exit 0\n"
            )
            (fake_repo / "install.sh").chmod(0o755)
            config = tmp / "config.json"
            state = tmp / "state.json"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            codex_log = tmp / "codex.log"
            (fake_bin / "codex").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >> \"$CODEX_LOG\"\n"
                "echo shell-codex-ok\n"
            )
            (fake_bin / "codex").chmod(0o755)
            write_json(config, {"enabled": True, "interval_seconds": 1, "retry_interval_seconds": 900, "prompt": "due shell"})
            write_json(state, {"last_success_epoch": time.time() - 3600})

            env = fake_path(fake_bin)
            env.update(
                {
                    "POWERPACKS_REPO_ROOT": str(fake_repo),
                    "POWERPACKS_HEARTBEAT_CONFIG": str(config),
                    "POWERPACKS_HEARTBEAT_STATE": str(state),
                    "POWERPACKS_SYNC_HOST_CODEX_HOME": "0",
                    "INSTALL_COUNT": str(install_count),
                    "CODEX_LOG": str(codex_log),
                }
            )
            proc = subprocess.run([str(HEARTBEAT), "--once"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("shell-codex-ok", proc.stdout)
            self.assertIn("--include-pending", HEARTBEAT.read_text())
            self.assertEqual(install_count.read_text().strip(), "1")
            self.assertIn("exec\ndue shell", codex_log.read_text())
            recorded = json.loads(state.read_text())
            self.assertEqual(recorded["last_exit_code"], 0)
            self.assertIn("last_success_epoch", recorded)

    def test_init_config_explicit_path_and_launchd_help(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "local-heartbeat.json"
            proc = subprocess.run([str(RUNNER), "--config", str(config), "--init-config"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(config.read_text())
            self.assertEqual(data["interval_seconds"], 3600)
            self.assertEqual(data["retry_interval_seconds"], 900)

        help_proc = subprocess.run([str(LAUNCHD), "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        self.assertIn("launchd", help_proc.stdout)
        launcher = LAUNCHD.read_text()
        self.assertIn("Library/LaunchAgents", launcher)
        self.assertIn("plistlib", launcher)
        self.assertIn("POWERPACKS_HEARTBEAT_CONFIG", launcher)
        self.assertIn("POWERPACKS_SYNC_HOST_CODEX_HOME", launcher)

    def test_launchd_install_generates_parseable_plist_with_direct_program_args(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            (fake_bin / "uname").write_text("#!/usr/bin/env bash\necho Darwin\n")
            (fake_bin / "launchctl").write_text("#!/usr/bin/env bash\nexit 0\n")
            (fake_bin / "uname").chmod(0o755)
            (fake_bin / "launchctl").chmod(0o755)
            env = fake_path(fake_bin)
            env.update(
                {
                    "HOME": str(tmp / "home with spaces & chars"),
                    "POWERPACKS_HEARTBEAT_CONFIG": str(tmp / "config & state" / "heartbeat.json"),
                    "POWERPACKS_HEARTBEAT_STATE": str(tmp / "config & state" / "state.json"),
                }
            )
            proc = subprocess.run([str(LAUNCHD), "install"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plist_path = Path(env["HOME"]) / "Library" / "LaunchAgents" / "com.powerset.powerpacks.codex-heartbeat.plist"
            payload = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(payload["ProgramArguments"], [str(HEARTBEAT)])
            self.assertEqual(payload["EnvironmentVariables"]["POWERPACKS_HEARTBEAT_CONFIG"], env["POWERPACKS_HEARTBEAT_CONFIG"])
            self.assertEqual(payload["EnvironmentVariables"]["CODEX_HOME"], f"{env['HOME']}/.codex")


if __name__ == "__main__":
    unittest.main()
