import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHD = ROOT / "scripts/install-powerpacks-console-launchd.sh"
HOSTNAME = ROOT / "scripts/install-powerpacks-console-hostname.sh"
DAEMON = ROOT / "scripts/powerpacks-console-daemon.sh"
COMPOSE_WRAPPER = ROOT / "scripts/run-powerpacks-compose.sh"
STACK_LAUNCHD = ROOT / "scripts/install-powerpacks-stack-launchd.sh"
COMPOSE_FILE = ROOT / "compose.powerpacks.yml"


def env_with_fake_bin(bin_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env


class PowerpacksConsolePersistenceTests(unittest.TestCase):
    def test_console_scripts_parse_and_expose_help_or_print(self) -> None:
        for script in [LAUNCHD, HOSTNAME, DAEMON, COMPOSE_WRAPPER, STACK_LAUNCHD]:
            with self.subTest(script=script.relative_to(ROOT)):
                parse = subprocess.run(["bash", "-n", str(script)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(parse.returncode, 0, parse.stderr)

        help_proc = subprocess.run([str(LAUNCHD), "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        self.assertIn("persistent macOS launchd", help_proc.stdout)

        compose_help = subprocess.run([str(COMPOSE_WRAPPER), "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(compose_help.returncode, 0, compose_help.stderr)
        self.assertIn("Docker Compose", compose_help.stdout)
        self.assertIn("restart: unless-stopped", compose_help.stdout)

        stack_help = subprocess.run([str(STACK_LAUNCHD), "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(stack_help.returncode, 0, stack_help.stderr)
        self.assertIn("RunAtLoad + KeepAlive", stack_help.stdout)

        print_proc = subprocess.run([str(HOSTNAME), "print"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(print_proc.returncode, 0, print_proc.stderr)
        self.assertIn("/etc/hosts maps names to IPs only", print_proc.stdout)
        self.assertIn("http://powerpacks.test:5177", print_proc.stdout)
        self.assertIn("http://powerpacks:5177", print_proc.stdout)
        self.assertNotIn("sudo", print_proc.stderr.lower())

    def test_console_launchd_install_generates_parseable_plist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            (fake_bin / "uname").write_text("#!/usr/bin/env bash\necho Darwin\n")
            (fake_bin / "launchctl").write_text("#!/usr/bin/env bash\nexit 0\n")
            (fake_bin / "uname").chmod(0o755)
            (fake_bin / "launchctl").chmod(0o755)
            home = tmp / "home with spaces & chars"
            repo = tmp / "repo with spaces"
            repo.mkdir()
            env = env_with_fake_bin(fake_bin)
            env.update({"HOME": str(home), "POWERPACKS_REPO_ROOT": str(repo), "PORT": "5178", "HOST": "127.0.0.1"})
            proc = subprocess.run([str(LAUNCHD), "install"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plist_path = home / "Library" / "LaunchAgents" / "com.powerset.powerpacks.console.plist"
            payload = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(payload["ProgramArguments"], [str(DAEMON)])
            self.assertEqual(payload["EnvironmentVariables"]["POWERPACKS_REPO_ROOT"], str(repo))
            self.assertEqual(payload["EnvironmentVariables"]["PORT"], "5178")
            self.assertEqual(payload["EnvironmentVariables"]["HOST"], "127.0.0.1")
            self.assertTrue(payload["KeepAlive"])
            self.assertTrue(payload["RunAtLoad"])

    def test_hostname_helper_documents_port_limitation(self) -> None:
        text = HOSTNAME.read_text()
        self.assertIn("/etc/hosts maps names to IPs only", text)
        self.assertIn("cannot map ports", text)
        self.assertIn("port-80 reverse proxy", text)
        self.assertIn("intentionally does not install", text)

        skill = (ROOT / "packs/powerset/skills/codex-loop/SKILL.md").read_text()
        self.assertIn("scripts/install-powerpacks-console-launchd.sh install", skill)
        self.assertIn("scripts/install-powerpacks-console-hostname.sh print", skill)
        self.assertIn("A bare `http://powerpacks` URL requires", skill)

    def test_compose_file_defines_persistent_console_and_loop_services(self) -> None:
        text = COMPOSE_FILE.read_text()
        self.assertIn("console:", text)
        self.assertIn("codex-heartbeat:", text)
        self.assertIn("restart: unless-stopped", text)
        self.assertIn("profiles: [\"loops\"]", text)
        self.assertIn("127.0.0.1:${POWERPACKS_CONSOLE_PORT:-5177}:5177", text)
        self.assertIn("npm ci --no-audit --no-fund", text)
        self.assertNotIn("rsync -a --delete", text)
        self.assertNotIn("/workspace/powerpacks-console-app", text)
        self.assertIn("powerpacks-console-node-modules", text)
        self.assertIn("create_host_path: false", text)
        self.assertIn("powerpacks-codex-home", text)
        self.assertNotIn("OPENAI_API_KEY", text)
        self.assertRegex(
            text,
            r"(?s)console:.*?target: /workspace/powerpacks\n\s+read_only: false",
        )
        self.assertRegex(
            text,
            r"(?s)codex-heartbeat:.*?target: /workspace/powerpacks\n\s+read_only: true",
        )

        app_package = (ROOT / "app/package.json").read_text()
        self.assertIn('"dev": "vite"', app_package)
        self.assertIn('"dev:lan": "vite --host 0.0.0.0"', app_package)

        setup_app = (ROOT / "bin/setup-app").read_text()
        self.assertIn('HOST="${HOST:-127.0.0.1}"', setup_app)

        console_runner = (ROOT / "scripts/run-powerpacks-console.sh").read_text()
        self.assertIn('HOST="${HOST:-127.0.0.1}"', console_runner)

    def test_vite_console_ignores_runtime_state_for_hmr(self) -> None:
        vite_config = (ROOT / "app/vite.config.ts").read_text()
        self.assertIn('host: process.env.HOST || "127.0.0.1"', vite_config)
        self.assertIn('"**/.powerpacks/**"', vite_config)
        self.assertIn('"**/.codex/**"', vite_config)
        self.assertIn('"**/.venv/**"', vite_config)

        readme = (ROOT / "app/README.md").read_text()
        self.assertIn("UI/control plane", readme)
        self.assertIn("Future WSS/pub-sub client", readme)
        self.assertIn("allowlisted network-search query tasks", readme)


if __name__ == "__main__":
    unittest.main()
