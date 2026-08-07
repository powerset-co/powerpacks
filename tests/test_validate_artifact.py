import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py"


def run_validate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


class ValidateArtifactTests(unittest.TestCase):
    def test_list_schemas_includes_known_names(self) -> None:
        result = run_validate("--list-schemas")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = result.stdout.split()
        self.assertIn("search-network-jd-plan", names)
        self.assertIn("candidate-frontier", names)



    def test_unknown_schema_errors(self) -> None:
        result = run_validate("--schema", "nope", "--file", str(SCRIPT))
        self.assertEqual(result.returncode, 1)
        self.assertIn("schema not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
