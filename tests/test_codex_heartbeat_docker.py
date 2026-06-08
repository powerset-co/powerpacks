import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def os_environ_with_path(bin_dir: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env.pop("OPENAI_API_KEY", None)
    return env


class CodexHeartbeatDockerTests(unittest.TestCase):
    def test_shell_scripts_parse_and_expose_help(self) -> None:
        scripts = [
            ROOT / "scripts/codex-heartbeat.sh",
            ROOT / "scripts/run-codex-heartbeat-docker.sh",
        ]
        for script in scripts:
            with self.subTest(script=script.relative_to(ROOT)):
                parse = subprocess.run(["bash", "-n", str(script)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(parse.returncode, 0, parse.stderr)
                help_proc = subprocess.run([str(script), "--help"], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
                self.assertIn("Codex", help_proc.stdout)

    def test_docker_wrapper_defaults_to_safe_login_snapshot(self) -> None:
        wrapper = (ROOT / "scripts/run-codex-heartbeat-docker.sh").read_text()
        self.assertIn('CODEX_HOME_MODE="${POWERPACKS_CODEX_HOME_MODE:-snapshot}"', wrapper)
        self.assertIn('target=/host-codex,readonly', wrapper)
        self.assertIn('$CONTAINER_CODEX_HOME_VOLUME:/root/.codex', wrapper)
        self.assertIn('--restart unless-stopped', wrapper)
        self.assertIn('POWERPACKS_CODEX_HOME_MODE=direct', wrapper)
        self.assertIn('POWERPACKS_CODEX_CACHE_VOLUME', wrapper)
        self.assertIn('$CONTAINER_CACHE_VOLUME:/root/.cache/powerpacks', wrapper)
        self.assertIn('pass_env_if_set POWERPACKS_HEARTBEAT_SKIP_INSTALL', wrapper)
        self.assertIn('pass_env_if_set POWERPACKS_SKIP_UV_SYNC', wrapper)
        self.assertNotIn('mapfile', wrapper)

    def test_docker_wrapper_start_uses_readonly_snapshot_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            docker_log = tmp / "docker.log"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '--- docker invocation ---' >> \"$DOCKER_LOG\"\n"
                "printf '%s\\n' \"$@\" >> \"$DOCKER_LOG\"\n"
                "case \"${1:-}\" in\n"
                "  build|run|rm|stop|ps|logs) exit 0 ;;\n"
                "  inspect|container) exit 1 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            fake_docker.chmod(0o755)
            host_codex = tmp / "host-codex"
            host_codex.mkdir()
            (host_codex / "auth.json").write_text("{}\n")

            env = {
                **os_environ_with_path(fake_bin),
                "DOCKER_LOG": str(docker_log),
                "HOST_CODEX_HOME": str(host_codex),
                "POWERPACKS_CODEX_HEARTBEAT_CONTAINER": "pp-test-snapshot",
            }
            proc = subprocess.run(
                [str(ROOT / "scripts/run-codex-heartbeat-docker.sh"), "start"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            log = docker_log.read_text()
            self.assertIn("--restart\nunless-stopped", log)
            self.assertIn(f"type=bind,source={host_codex},target=/host-codex,readonly", log)
            self.assertIn("powerpacks-codex-home:/root/.codex", log)
            self.assertIn(f"type=bind,source={ROOT},target=/workspace/powerpacks,readonly", log)
            self.assertIn("POWERPACKS_SYNC_HOST_CODEX_HOME=1", log)

    def test_docker_wrapper_supports_api_key_without_host_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            docker_log = tmp / "docker.log"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '--- docker invocation ---' >> \"$DOCKER_LOG\"\n"
                "printf '%s\\n' \"$@\" >> \"$DOCKER_LOG\"\n"
                "case \"${1:-}\" in build|run) exit 0 ;; *) exit 1 ;; esac\n"
            )
            fake_docker.chmod(0o755)
            missing_home = tmp / "missing-codex-home"
            env = {
                **os_environ_with_path(fake_bin),
                "DOCKER_LOG": str(docker_log),
                "HOST_CODEX_HOME": str(missing_home),
                "OPENAI_API_KEY": "sk-test",
                "POWERPACKS_HEARTBEAT_SKIP_INSTALL": "1",
                "POWERPACKS_CODEX_HEARTBEAT_CONTAINER": "pp-test-api-key",
            }
            proc = subprocess.run(
                [str(ROOT / "scripts/run-codex-heartbeat-docker.sh"), "once"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            log = docker_log.read_text()
            self.assertIn("OPENAI_API_KEY=sk-test", log)
            self.assertIn("POWERPACKS_HEARTBEAT_SKIP_INSTALL=1", log)
            self.assertIn("POWERPACKS_SYNC_HOST_CODEX_HOME=0", log)
            self.assertNotIn("target=/host-codex", log)
            self.assertIn("HEARTBEAT_ONCE=1", log)

    def test_docker_wrapper_allows_noop_start_without_login_in_snapshot_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            docker_log = tmp / "docker.log"
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '--- docker invocation ---' >> \"$DOCKER_LOG\"\n"
                "printf '%s\\n' \"$@\" >> \"$DOCKER_LOG\"\n"
                "case \"${1:-}\" in build|run) exit 0 ;; *) exit 1 ;; esac\n"
            )
            fake_docker.chmod(0o755)
            missing_home = tmp / "missing-codex-home"
            proc = subprocess.run(
                [str(ROOT / "scripts/run-codex-heartbeat-docker.sh"), "once"],
                cwd=ROOT,
                env={**os_environ_with_path(fake_bin), "DOCKER_LOG": str(docker_log), "HOST_CODEX_HOME": str(missing_home)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("heartbeat can still no-op", proc.stderr)
            self.assertIn("POWERPACKS_SYNC_HOST_CODEX_HOME=0", docker_log.read_text())

    def test_codex_install_can_skip_agent_bootstrap_for_readonly_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            codex_home = tmp / ".codex"
            skills_dir = tmp / "skills"
            proc = subprocess.run(
                [str(ROOT / "install.sh"), "codex", str(skills_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={
                    **os_environ_with_path(Path.cwd()),
                    "CODEX_HOME": str(codex_home),
                    "POWERPACKS_SKIP_UV_SYNC": "1",
                    "POWERPACKS_SKIP_AGENT_BOOTSTRAP": "1",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("skipped local Codex profile generation", proc.stdout)
            self.assertTrue((skills_dir / "powerset" / "SKILL.md").exists())

    def test_heartbeat_syncs_host_codex_home_without_writing_back(self) -> None:
        heartbeat = (ROOT / "scripts/codex-heartbeat.sh").read_text()
        self.assertIn('rsync -a --delete', heartbeat)
        self.assertIn('$HOST_CODEX_HOME"/ "$CODEX_HOME"/', heartbeat)
        self.assertIn('POWERPACKS_SYNC_HOST_CODEX_HOME', heartbeat)
        self.assertIn('codex login --with-api-key', heartbeat)
        self.assertIn('--check-due', heartbeat)
        self.assertIn("date -u '+%Y-%m-%dT%H:%M:%SZ'", heartbeat)
        self.assertIn('install.sh" codex', heartbeat)
        self.assertIn('UV_PROJECT_ENVIRONMENT', heartbeat)
        self.assertIn('POWERPACKS_SKIP_AGENT_BOOTSTRAP', heartbeat)
        self.assertIn('codex-heartbeat-runner.py', heartbeat)
        self.assertIn('exit "$heartbeat_status"', heartbeat)

    def test_dockerfile_installs_codex_and_uses_heartbeat_entrypoint(self) -> None:
        dockerfile = (ROOT / "adapters/codex/docker/Dockerfile").read_text()
        self.assertIn("ARG CODEX_VERSION=latest", dockerfile)
        self.assertIn('npm install -g "@openai/codex@${CODEX_VERSION}"', dockerfile)
        self.assertIn("COPY scripts/codex-heartbeat.sh", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/powerpacks-codex-heartbeat"]', dockerfile)
        self.assertIn("ENV CODEX_HOME=/root/.codex", dockerfile)

    def test_dockerignore_keeps_build_context_small_and_secret_safe(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn(".env", dockerignore)
        self.assertIn(".venv", dockerignore)
        self.assertIn(".git", dockerignore)
        self.assertIn("app/node_modules", dockerignore)

    def test_docs_answer_login_sharing_question(self) -> None:
        docs = (ROOT / "docs/codex-heartbeat-docker.md").read_text()
        self.assertIn("Sharing the regular shell Codex login", docs)
        self.assertIn("read-only at `/host-codex`", docs)
        self.assertIn("separate writable Docker volume", docs)
        self.assertIn("codex login --with-api-key", docs)
        self.assertIn("read-only checkout", docs)
        self.assertIn("POWERPACKS_HEARTBEAT_SKIP_INSTALL=1", docs)
        self.assertIn("macOS `launchd`", docs)
        self.assertIn("retry_interval_seconds", docs)
        self.assertIn("POWERPACKS_CODEX_HOME_MODE=direct", docs)


if __name__ == "__main__":
    unittest.main()
