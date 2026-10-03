"""The public rebuild command reaches the isolated preparation primitive."""
from pathlib import Path
import subprocess
import unittest


class RebuildCliTests(unittest.TestCase):
    def test_rebuild_help_exposes_required_isolated_inputs(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [str(root / "bin/deep-context"), "rebuild", "--help"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--original-state-root", "--backup-root", "--state-root",
                     "--people-csv", "--owner-profile"):
            self.assertIn(flag, result.stdout)


if __name__ == "__main__":
    unittest.main()
