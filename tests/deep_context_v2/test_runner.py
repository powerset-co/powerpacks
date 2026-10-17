"""Check runner guarantees inside a child suite, not only during discovery."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "deep_context_v2_unit.py"


class RunnerTests(unittest.TestCase):
    def child_suite(self, definition: str, *, marker: Path | None = None) -> subprocess.CompletedProcess[str]:
        code = f"""
import runpy
import signal
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
runner = runpy.run_path({str(RUNNER)!r})
marker = Path({str(marker)!r})
{definition}
suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChildTests)
with patch.object(unittest.defaultTestLoader, 'discover', return_value=suite):
    raise SystemExit(runner['main']())
"""
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=10)

    def test_deadline_stops_cleanup_and_later_tests(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "continued"
            result = self.child_suite("""
class ChildTests(unittest.TestCase):
    def test_a_deadline(self):
        self.addCleanup(lambda: marker.write_text('cleanup ran'))
        signal.raise_signal(signal.SIGALRM)
    def test_b_must_not_run(self):
        marker.write_text('later test ran')
""", marker=marker)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("exceeded 290 seconds", result.stderr)
            self.assertFalse(marker.exists(), "unittest continued after the deadline")

    def test_network_calls_fail_inside_running_tests(self) -> None:
        result = self.child_suite("""
class ChildTests(unittest.TestCase):
    def test_guard(self):
        with self.assertRaisesRegex(AssertionError, 'must not use the network'):
            socket.getaddrinfo('example.invalid', 443)
        with self.assertRaisesRegex(AssertionError, 'must not use the network'):
            socket.create_connection(('127.0.0.1', 1))
        with socket.socket() as connection:
            with self.assertRaisesRegex(AssertionError, 'must not use the network'):
                connection.connect(('127.0.0.1', 1))
            with self.assertRaisesRegex(AssertionError, 'must not use the network'):
                connection.connect_ex(('127.0.0.1', 1))
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network disabled", result.stdout)

    def test_failed_assertion_makes_command_fail(self) -> None:
        result = self.child_suite("""
class ChildTests(unittest.TestCase):
    def test_failure(self):
        self.assertEqual('actual', 'expected')
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAILED", result.stderr)


if __name__ == "__main__":
    unittest.main()
