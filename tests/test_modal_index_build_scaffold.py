import argparse
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODAL = ROOT / "packs/indexing/primitives/modal_index_build/modal_index_build.py"
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"


def load_modal_module():
    spec = importlib.util.spec_from_file_location("modal_index_build", MODAL)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ModalIndexBuildScaffoldTests(unittest.TestCase):
    def test_plan_is_static_and_paths_are_operator_scoped(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(MODAL),
                "plan",
                "--input",
                str(FIXTURE_PEOPLE),
                "--output-dir",
                ".powerpacks/search-index",
                "--run-id",
                "modal-run",
                "--operator-id",
                "arthur@example.com/unsafe",
                "--pull-duckdb",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads(proc.stdout)
        self.assertEqual(plan["app_name"], "powerpacks-indexing")
        self.assertEqual(plan["volume"]["name"], "powerpacks-search-index")
        self.assertIn("/mnt/powerpacks/operators/", plan["volume"]["run_path"])
        self.assertNotIn("@", plan["volume"]["run_path"])
        self.assertNotIn("/unsafe", plan["volume"]["run_path"])
        self.assertTrue(plan["pull"]["duckdb"])

    def test_missing_modal_returns_structured_error_without_import_requirement(self) -> None:
        module = load_modal_module()
        args = argparse.Namespace(
            input=str(FIXTURE_PEOPLE),
            output_dir=".powerpacks/search-index",
            run_id="modal-run",
            operator_id="local:user",
            default_operator_id=None,
            limit=None,
            cache_policy="reuse",
            volume_name="powerpacks-search-index",
            pull_duckdb=True,
            materialize_compat_artifacts=False,
            allow_unverified_live_run=True,
        )
        original = module.build_modal_app
        module.build_modal_app = lambda _volume_name: (_ for _ in ()).throw(ModuleNotFoundError("modal"))
        try:
            out = module.cmd_run(args)
        finally:
            module.build_modal_app = original
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["kind"], "modal_unavailable")
        self.assertIn("build_processing_pipeline.py run", out["fallback_command"])

    def test_run_is_gated_until_live_modal_smoke_is_explicitly_requested(self) -> None:
        module = load_modal_module()
        args = argparse.Namespace(
            input=str(FIXTURE_PEOPLE),
            output_dir=".powerpacks/search-index",
            run_id="modal-run",
            operator_id="local:user",
            default_operator_id=None,
            limit=None,
            cache_policy="reuse",
            volume_name="powerpacks-search-index",
            pull_duckdb=True,
            materialize_compat_artifacts=False,
            allow_unverified_live_run=False,
        )
        out = module.cmd_run(args)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["kind"], "modal_live_run_unverified")
        self.assertIn("fallback_command", out)

    def test_source_uses_lazy_modal_patterns_and_pyproject_has_no_modal_dependency(self) -> None:
        source = MODAL.read_text(encoding="utf-8")
        self.assertIn('modal.App(APP_NAME)', source)
        self.assertIn('modal.Image.debian_slim(python_version="3.12").uv_sync()', source)
        self.assertIn('modal.Volume.from_name(volume_name, create_if_missing=True)', source)
        self.assertIn('volume.commit()', source)
        before_lazy = source.split("def build_modal_app", 1)[0]
        self.assertNotIn("import modal", before_lazy)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        dependencies = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
        self.assertNotIn("modal", dependencies.lower())


if __name__ == "__main__":
    unittest.main()
