import importlib.util
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
    def test_gcloud_login_fix_requires_tty_not_fix_args(self) -> None:
        doctor = load_doctor()
        original_run = doctor.run
        try:
            doctor.run = lambda *args, **kwargs: (1, "", "no active account")
            payload = doctor.check_gcloud_account()
        finally:
            doctor.run = original_run

        self.assertEqual(payload["id"], "gcloud_account")
        self.assertEqual(payload["fix_kind"], "interactive")
        self.assertEqual(payload["fix_command"], "gcloud auth login --no-launch-browser")
        self.assertTrue(payload["requires_tty"])
        self.assertNotIn("fix_args", payload)


if __name__ == "__main__":
    unittest.main()
