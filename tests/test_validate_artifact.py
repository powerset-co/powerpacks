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
        self.assertIn("probe-summaries", names)

    def test_valid_probe_summaries_passes(self) -> None:
        doc = [
            {
                "id": "p1",
                "status": "completed",
                "query": "senior backend engineers",
                "artifact_dir": ".powerpacks/runs/artifacts/x",
                "csv": ".powerpacks/runs/artifacts/x/results.csv",
                "state": ".powerpacks/runs/x.json",
                "found_count": 12,
                "fallback_reason": None,
            }
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(doc, handle)
            path = handle.name
        result = run_validate("--schema", "probe-summaries", "--file", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok:", result.stdout)

    def test_wrapper_shaped_probe_summaries_fails_with_pointer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"probes": []}, handle)
            path = handle.name
        result = run_validate("--schema", "probe-summaries", "--file", path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid:", result.stderr)

    def test_unknown_schema_errors(self) -> None:
        result = run_validate("--schema", "nope", "--file", str(SCRIPT))
        self.assertEqual(result.returncode, 1)
        self.assertIn("schema not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
