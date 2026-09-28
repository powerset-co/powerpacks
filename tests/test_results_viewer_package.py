"""The results-viewer package must import on its own: the hosted API installs only its files."""
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "results-viewer"
MODULES = ("model", "rendering", "snapshot", "feedback_payload")


class ResultsViewerPackageTest(unittest.TestCase):
    def test_packaged_files_import_without_the_repo(self):
        files = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"][
            "wheel"]["force-include"]
        with tempfile.TemporaryDirectory() as directory:
            for source, target in files.items():
                destination = Path(directory) / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PACKAGE / source, destination)
            imports = "; ".join(f"import packs.search.primitives.deep_search.results_web.{name}" for name in MODULES)
            result = subprocess.run([sys.executable, "-c", imports], cwd=directory, capture_output=True, text=True,
                                    env={"PYTHONPATH": directory, "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
