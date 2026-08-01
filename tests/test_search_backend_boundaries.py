import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TypedSearchBackendImportBoundaryTests(unittest.TestCase):
    def _run(self, code: str) -> None:
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_runner_imports_with_remote_modules_blocked(self) -> None:
        self._run(
            """
import builtins
real = builtins.__import__
def blocked(name, *args, **kwargs):
    if 'turbopuffer' in name or name == 'postgres_client':
        raise ModuleNotFoundError(name)
    return real(name, *args, **kwargs)
builtins.__import__ = blocked
from packs.search.backends.local.runner import LocalSearchRunner
assert LocalSearchRunner
"""
        )

    def test_remote_runner_imports_with_local_modules_blocked(self) -> None:
        self._run(
            """
import builtins
real = builtins.__import__
def blocked(name, *args, **kwargs):
    if 'duckdb' in name or 'local_duckdb' in name:
        raise ModuleNotFoundError(name)
    return real(name, *args, **kwargs)
builtins.__import__ = blocked
from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
assert TurboPufferSearchRunner
"""
        )

    def test_composition_root_does_not_eager_import_runners(self) -> None:
        self._run(
            """
import sys
import packs.search.pipeline.search
assert 'packs.search.backends.local.runner' not in sys.modules
assert 'packs.search.backends.turbopuffer.runner' not in sys.modules
"""
        )


if __name__ == "__main__":
    unittest.main()
