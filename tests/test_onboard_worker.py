"""Exercise the standalone worker through real, isolated tmux and a local fake Codex."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


WORKER = Path(__file__).resolve().parents[1] / "bin/onboard-worker"
TMUX = shutil.which("tmux")
FAKE_CODEX = r'''
import json
import os
from pathlib import Path
import sys
import tty

if "--help" in sys.argv:
    print("__HELP__")
    raise SystemExit(0)

events = Path(__EVENTS__)

def record(value):
    with events.open("a") as stream:
        stream.write(json.dumps(value) + "\n")

tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004hREADY\r\n")
sys.stdout.flush()
record({"kind": "started", "pid": os.getpid(), "cwd": os.getcwd(),
        "args": sys.argv[1:], "app": os.environ.get("POWERPACKS_PERMISSION_APP")})
message = b""
while True:
    char = os.read(sys.stdin.fileno(), 1)
    if not char:
        break
    message += char
    if char != b"\r" or (message.startswith(b"\x1b[200~") and b"\x1b[201~" not in message):
        continue
    value = message[:-1].removeprefix(b"\x1b[200~").removesuffix(b"\x1b[201~").decode()
    record({"kind": "message", "text": value})
    print("RECEIVED " + json.dumps(value), end="\r\n", flush=True)
    message = b""
    if value in ("exit", "crash"):
        print("FINAL " + value, end="\r\n", flush=True)
        if value == "crash":
            os._exit(23)
        raise SystemExit(0)
'''


@unittest.skipUnless(TMUX, "tmux is required for the worker integration tests")
class OnboardWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="worker-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cwd = self.root / "empty home"
        self.cwd.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "tmux").symlink_to(TMUX)
        self.socket = self.root / "worker.sock"
        self.addCleanup(self.kill_server, self.socket)
        self.events = self.root / "events.jsonl"
        self.app = self.root / "Codex.app"
        self.app.mkdir()
        self.env = {**os.environ, "PATH": str(self.bin), "HOME": str(self.cwd),
                    "POWERPACKS_PERMISSION_APP": str(self.app), "TERM": "xterm-256color"}
        self.env.pop("TMUX", None)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Install from the URL. Preserve the user's choices.\n")
        self.write_codex()

    def write_codex(self, help_text="--no-daemon --no-alt-screen"):
        script = FAKE_CODEX.replace("__EVENTS__", repr(str(self.events))).replace("__HELP__", help_text)
        executable = self.bin / "codex"
        executable.write_text(f"#!{sys.executable}\n" + script)
        executable.chmod(0o755)

    def kill_server(self, socket):
        subprocess.run([TMUX, "-S", str(socket), "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def call(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(WORKER), "--socket", str(self.socket), *args],
                                cwd=self.cwd, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def start(self):
        return self.call("start", "--prompt-file", str(self.prompt), "--cwd", str(self.cwd),
                         "--sandbox", "danger-full-access", "--approval", "never")

    def records(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.03)
        self.fail("Worker did not reach the expected state: " + repr(self.call("read")))

    def started(self, count=1):
        def reached():
            rows = [row for row in self.records() if row["kind"] == "started"]
            return rows if len(rows) >= count else None
        return self.wait_for(reached)

    def test_start_without_repo_preserves_prompt_and_explicit_permissions(self):
        marker = self.root / "injected"
        prompt = f"Install from URL; $(touch {marker}) `touch {marker}`\nKeep quoted 'choices'."
        self.prompt.write_text(prompt)
        result = self.start()
        self.assertEqual(result["state"], "running")
        row = self.started()[0]
        self.assertEqual(row["cwd"], str(self.cwd))
        self.assertEqual(row["app"], str(self.app))
        self.assertEqual(row["args"][:-1], ["--no-daemon", "--no-alt-screen", "-C", str(self.cwd),
                                           "-s", "danger-full-access", "-a", "never"])
        self.assertIn("Powerpacks onboarding worker", row["args"][-1])
        self.assertTrue(row["args"][-1].endswith(prompt))
        self.assertFalse(marker.exists())
        self.assertFalse((self.cwd / ".powerpacks").exists())

    def test_repeated_start_reuses_pid(self):
        first = self.start()
        self.started()
        second = self.start()
        self.assertEqual(second["pid"], first["pid"])
        self.assertEqual(len(self.records()), 1)

    def test_send_preserves_literal_multiline_metacharacters(self):
        self.start()
        self.started()
        marker = self.root / "injected"
        message = f"Skip Gmail now.\n$(touch {marker}); `touch {marker}` 'quoted' & | $HOME"
        self.assertEqual(self.call("send", message)["state"], "sent")
        rows = self.wait_for(lambda: [row for row in self.records() if row["kind"] == "message"])
        self.assertEqual(rows[0]["text"], message)
        self.assertFalse(marker.exists())

    def test_exit_and_crash_retain_output_then_restart(self):
        self.start()
        self.started()
        for count, message, code in ((1, "exit", 0), (2, "crash", 23)):
            with self.subTest(message=message):
                self.call("send", message)
                def exited():
                    value = self.call("read")
                    return value if value["state"] == "exited" else None
                result = self.wait_for(exited)
                self.assertEqual(result["exit_code"], code)
                self.assertIn("FINAL " + message, result["output"])
                self.assertEqual(self.call("read")["output"], result["output"])
                self.assertIn("not running", self.call("send", "retry", expected=1)["error"])
                restarted = self.start()
                self.assertEqual(restarted["state"], "running")
                self.assertNotEqual(restarted["pid"], result["pid"])
                self.started(count + 1)

    def test_missing_codex_fails_without_starting_session(self):
        (self.bin / "codex").unlink()
        result = self.call("start", "--prompt-file", str(self.prompt),
                           "--sandbox", "read-only", "--approval", "never", expected=1)
        self.assertIn("unavailable", result["error"])
        self.assertEqual(self.call("read")["state"], "not_started")

    def test_unsupported_codex_fails_without_starting_session(self):
        self.write_codex("--no-alt-screen")
        result = self.call("start", "--prompt-file", str(self.prompt),
                           "--sandbox", "read-only", "--approval", "never", expected=1)
        self.assertIn("lacks --no-daemon", result["error"])
        self.assertEqual(self.call("read")["state"], "not_started")

    def test_prompt_sandbox_and_approval_are_required(self):
        options = {"--prompt-file": str(self.prompt), "--sandbox": "read-only", "--approval": "never"}
        for missing in options:
            with self.subTest(missing=missing):
                args = [item for option, value in options.items() if option != missing for item in (option, value)]
                result = subprocess.run([sys.executable, str(WORKER), "--socket", str(self.socket), "start", *args],
                                        env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn(missing, result.stderr)
        self.assertEqual(self.call("read")["state"], "not_started")

    def test_cleanup_keeps_unrelated_tmux_server(self):
        unrelated = self.root / "unrelated.sock"
        self.addCleanup(self.kill_server, unrelated)
        subprocess.run([TMUX, "-S", str(unrelated), "-f", "/dev/null", "new-session",
                        "-d", "-s", "unrelated", "/bin/sh"], check=True)
        self.start()
        self.started()
        self.kill_server(self.socket)
        self.assertEqual(self.call("read")["state"], "not_started")
        result = subprocess.run([TMUX, "-S", str(unrelated), "has-session", "-t", "unrelated"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
