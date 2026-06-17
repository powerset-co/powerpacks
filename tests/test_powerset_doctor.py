import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = ROOT / "packs/powerset/primitives/doctor/doctor.py"


def load_doctor():
    spec = importlib.util.spec_from_file_location("powerset_doctor", DOCTOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PowersetDoctorTests(unittest.TestCase):
    def test_runtime_keys_missing_uses_api_pull(self) -> None:
        doctor = load_doctor()
        original_run = doctor.run
        try:
            doctor.run = lambda *args, **kwargs: (
                2,
                json.dumps({"status": "missing", "missing": ["MODAL_TOKEN_ID"]}),
                "",
            )
            payload = doctor.check_runtime_keys(Path(".env"))
        finally:
            doctor.run = original_run

        self.assertEqual(payload["id"], "runtime_keys")
        self.assertEqual(payload["status"], "missing")
        self.assertEqual(payload["fix_kind"], "interactive")
        self.assertEqual(payload["fix_command"], "$powerset env pull")
        self.assertIn("pull_runtime_keys", payload["fix_args"][1])


if __name__ == "__main__":
    unittest.main()
