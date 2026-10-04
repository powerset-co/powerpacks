"""Keep one ordinary deep-context path and hide retired maintenance verbs."""
import os
import tempfile
import subprocess
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import build_parser

ROOT = Path(__file__).resolve().parents[1]


class DeepContextCommandsTests(unittest.TestCase):
    def test_realize_is_one_sqlite_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            uv = root / "uv"
            uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
            uv.chmod(0o755)
            subprocess.run([str(ROOT / "bin/deep-context"), "realize"], cwd=ROOT,
                           env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "CALLS": str(calls)},
                           capture_output=True, text=True, check=True)
            commands = calls.read_text().splitlines()
        self.assertEqual(len(commands), 1)
        self.assertIn("deep_context.realize.export_people", commands[0])

    def test_bare_review_starts_the_server_under_macos_system_bash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            (root / "uv").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
            # No server is found, probed or waited for: the run never touches a real one.
            for name in ("lsof", "curl"):
                (root / name).write_text("#!/bin/sh\nexit 1\n")
            (root / "sleep").write_text("#!/bin/sh\nexit 0\n")
            for fake in root.iterdir():
                fake.chmod(0o755)
            result = subprocess.run(["/bin/bash", str(ROOT / "bin/deep-context"), "review"], cwd=ROOT,
                                    env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "CALLS": str(calls)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = calls.read_text().splitlines()
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].endswith("deep_context.review.reconcile_review_web start"), commands[0])

    def test_maintenance_verbs_are_not_public(self) -> None:
        result = subprocess.run([str(ROOT / 'bin/deep-context'), '--help'], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        help_text = result.stderr
        for command in ('  refresh ', '  rejudge ', '  heal ', '  reconcile '):
            self.assertNotIn(command, help_text)
        for command in ('  synthesize ', '  review ', '  restart '):
            self.assertIn(command, help_text)
        self.assertNotIn('--fresh', help_text)

    def test_retired_verbs_fail_before_running(self) -> None:
        for command in ('refresh', 'rejudge', 're-review', 'heal', 'reconcile', 'apply-retargets',
                        'persist-review-identities'):
            with self.subTest(command=command):
                result = subprocess.run([str(ROOT / 'bin/deep-context'), command], cwd=ROOT,
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unknown command', result.stderr)

    def test_rejudge_is_not_a_synthesis_flag(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(['--rejudge'])

