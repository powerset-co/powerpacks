import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "primitives" / "provision_runtime_env" / "provision_runtime_env.py"


class ProvisionRuntimeEnvTests(unittest.TestCase):
    def test_plan_reports_missing_without_auth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PROVISION),
                    "plan",
                    "--profile",
                    "search-core",
                    "--env-file",
                    str(env_file),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(proc.stdout)
            self.assertEqual(payload["command"], "plan")
            self.assertIn("TURBOPUFFER_API_KEY", payload["missing"])
            self.assertIn("DATABASE_URL", payload["missing"])
            self.assertIn("OPENAI_API_KEY", payload["missing"])

    def test_check_redacts_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("TURBOPUFFER_API_KEY=tp\nDATABASE_URL=postgres://db\nOPENAI_API_KEY=sk\n")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PROVISION),
                    "check",
                    "--profile",
                    "search-core",
                    "--env-file",
                    str(env_file),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

            payload = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(payload["status"], "missing")
            self.assertIn("TURBOPUFFER_REGION", payload["missing"])
            self.assertNotIn("postgres://db", proc.stdout)
            self.assertTrue(all("redacted" in item for item in payload["secrets"]))

    def test_pull_from_fake_gcp_writes_allowlisted_env_without_printing_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env_file = tmp / ".env"
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            fake_gcloud = bin_dir / "gcloud"
            fake_gcloud.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if sys.argv[1:4] == ['auth', 'list', '--filter=status:ACTIVE']:\n"
                "    print('alice@powerset.co')\n"
                "    raise SystemExit(0)\n"
                "secret = sys.argv[sys.argv.index('--secret') + 1]\n"
                "print('value-for-' + secret)\n"
            )
            fake_gcloud.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PROVISION),
                    "pull",
                    "--profile",
                    "search-core",
                    "--env-file",
                    str(env_file),
                    "--confirm",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(proc.stdout)
            text = env_file.read_text()
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["gcloud_account"], "alice@powerset.co")
            self.assertIn("TURBOPUFFER_API_KEY=value-for-powerpacks-turbopuffer-api-key", text)
            self.assertIn("DATABASE_URL=value-for-powerpacks-database-url", text)
            self.assertNotIn("value-for-powerpacks", proc.stdout)
            self.assertTrue(all(item["redacted"] == "***" for item in payload["secrets"]))

    def test_pull_rejects_non_powerset_email(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            fake_gcloud = bin_dir / "gcloud"
            fake_gcloud.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if sys.argv[1:4] == ['auth', 'list', '--filter=status:ACTIVE']:\n"
                "    print('alice@example.com')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit('unexpected command')\n"
            )
            fake_gcloud.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PROVISION),
                    "pull",
                    "--profile",
                    "search-core",
                    "--env-file",
                    str(tmp / ".env"),
                    "--confirm",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )

            payload = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(payload["status"], "failed")
            self.assertIn("non-powerset.co", payload["error"])


if __name__ == "__main__":
    unittest.main()
