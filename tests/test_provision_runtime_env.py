import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py"


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
            self.assertIn("OPENROUTER_API_KEY", payload["missing"])
            self.assertIn("PARALLEL_API_KEY", payload["missing"])

    def test_check_redacts_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            # search-core requires TURBOPUFFER_API_KEY, DATABASE_URL,
            # OPENAI_API_KEY, OPENROUTER_API_KEY, and PARALLEL_API_KEY. Leave
            # the latter three missing so the check exercises the missing-key path.
            env_file.write_text("TURBOPUFFER_API_KEY=tp\nDATABASE_URL=postgres://db\n")
            proc = subprocess.run(
                [
                    sys.executable, str(PROVISION),
                    "check",
                    "--profile", "search-core",
                    "--env-file", str(env_file),
                ],
                cwd=ROOT, text=True, capture_output=True,
            )

            payload = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(payload["status"], "missing")
            self.assertIn("OPENAI_API_KEY", payload["missing"])
            self.assertIn("OPENROUTER_API_KEY", payload["missing"])
            self.assertIn("PARALLEL_API_KEY", payload["missing"])
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
            # Per-user is the default scope. Each .env value comes from
            # `powerpacks-users-<slug>-<capability>` for the active gcloud account.
            self.assertEqual(payload["scope"], {"mode": "per_user", "email": "alice@powerset.co", "slug": "alice"})
            self.assertIn("TURBOPUFFER_API_KEY=value-for-powerpacks-users-alice-turbopuffer-api-key", text)
            self.assertIn("DATABASE_URL=value-for-powerpacks-users-alice-database-url", text)
            self.assertIn("OPENROUTER_API_KEY=value-for-powerpacks-users-alice-openrouter-api-key", text)
            self.assertIn("PARALLEL_API_KEY=value-for-powerpacks-users-alice-parallel-api-key", text)
            self.assertNotIn("value-for-powerpacks", proc.stdout)
            self.assertTrue(all(item["redacted"] == "***" for item in payload["secrets"]))

    def test_pull_with_shared_flag_uses_legacy_flat_mapping(self) -> None:
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
                    sys.executable, str(PROVISION),
                    "pull",
                    "--profile", "search-core",
                    "--env-file", str(env_file),
                    "--confirm",
                    "--shared",
                ],
                cwd=ROOT, env=env, text=True, capture_output=True, check=True,
            )
            payload = json.loads(proc.stdout)
            text = env_file.read_text()
            self.assertEqual(payload["scope"], {"mode": "shared"})
            self.assertIn("TURBOPUFFER_API_KEY=value-for-powerpacks-turbopuffer-api-key", text)
            self.assertIn("DATABASE_URL=value-for-powerpacks-database-url", text)
            self.assertIn("OPENROUTER_API_KEY=value-for-powerpacks-openrouter-api-key", text)
            self.assertIn("PARALLEL_API_KEY=value-for-powerpacks-parallel-api-key", text)

    def test_best_effort_pull_writes_available_keys_when_one_secret_missing(self) -> None:
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
                "if 'openrouter-api-key' in secret:\n"
                "    print('NOT_FOUND: missing', file=sys.stderr); raise SystemExit(5)\n"
                "print('value-for-' + secret)\n"
            )
            fake_gcloud.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [
                    sys.executable, str(PROVISION),
                    "pull",
                    "--profile", "search-core",
                    "--env-file", str(env_file),
                    "--confirm",
                    "--best-effort",
                ],
                cwd=ROOT, env=env, text=True, capture_output=True, check=True,
            )
            payload = json.loads(proc.stdout)
            text = env_file.read_text()
            self.assertEqual(payload["status"], "partial")
            self.assertIn("OPENROUTER_API_KEY", payload["missing"])
            self.assertIn("OPENROUTER_API_KEY", payload["fetch_errors"])
            self.assertIn("TURBOPUFFER_API_KEY=value-for-powerpacks-users-alice-turbopuffer-api-key", text)
            self.assertIn("PARALLEL_API_KEY=value-for-powerpacks-users-alice-parallel-api-key", text)
            self.assertNotIn("OPENROUTER_API_KEY=", text)

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
            # Non-powerset.co users now get a structured `not_privileged`
            # response instead of a hard error, so the $powerset login flow
            # can surface a friendly contact-us message.
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(payload["status"], "not_privileged")
            self.assertIn("non-powerset.co", payload["error"])
            self.assertIn("Contact a Powerpacks maintainer", payload["message"])


if __name__ == "__main__":
    unittest.main()


class ProbeCommandTests(unittest.TestCase):
    def test_probe_classifies_per_user_secret_access(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            fake_gcloud = bin_dir / "gcloud"
            # Simulate a per-user IAM landscape: one secret accessible, one
            # denied, the rest not provisioned.
            fake_gcloud.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args[:3] == ['auth', 'list', '--filter=status:ACTIVE']:\n"
                "    print('arthur@powerset.co')\n"
                "    raise SystemExit(0)\n"
                "if args[:2] == ['secrets', 'describe']:\n"
                "    name = args[2]\n"
                "    if 'turbopuffer-api-key' in name:\n"
                "        print(name); raise SystemExit(0)\n"
                "    if 'database-url' in name:\n"
                "        print('PERMISSION_DENIED: blocked', file=sys.stderr); raise SystemExit(7)\n"
                "    print('NOT_FOUND: missing', file=sys.stderr); raise SystemExit(5)\n"
                "raise SystemExit('unexpected: ' + repr(args))\n"
            )
            fake_gcloud.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PROVISION),
                    "probe",
                    "--profile",
                    "search-core",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["primitive"], "provision_runtime_env")
            self.assertEqual(payload["command"], "probe")
            self.assertEqual(payload["status"], "partial")
            self.assertEqual(payload["email"], "arthur@powerset.co")
            self.assertEqual(payload["slug"], "arthur")
            self.assertIn("TURBOPUFFER_API_KEY", payload["accessible"])
            self.assertIn("DATABASE_URL", payload["denied"])
            # OPENAI_API_KEY, OPENROUTER_API_KEY, and PARALLEL_API_KEY are part
            # of search-core but neither accessible nor explicitly denied → not_provisioned.
            self.assertIn("OPENAI_API_KEY", payload["not_provisioned"])
            self.assertIn("OPENROUTER_API_KEY", payload["not_provisioned"])
            self.assertIn("PARALLEL_API_KEY", payload["not_provisioned"])
            # Per-user names follow the powerpacks-users-<slug>-<base> rule.
            ids = {r["secret_id"] for r in payload["results"]}
            self.assertIn("powerpacks-users-arthur-turbopuffer-api-key", ids)
            self.assertIn("powerpacks-users-arthur-database-url", ids)

    def test_probe_with_explicit_email_overrides_gcloud_account(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            fake_gcloud = bin_dir / "gcloud"
            fake_gcloud.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if sys.argv[1:3] == ['secrets', 'describe']:\n"
                "    print(sys.argv[3]); raise SystemExit(0)\n"
                "raise SystemExit('unexpected')\n"
            )
            fake_gcloud.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PROVISION),
                    "probe",
                    "--profile",
                    "search-core",
                    "--email",
                    "jake@powerset.co",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["email"], "jake@powerset.co")
            self.assertEqual(payload["slug"], "jake")
            for result in payload["results"]:
                self.assertTrue(result["secret_id"].startswith("powerpacks-users-jake-"))


class AuthInspectTests(unittest.TestCase):
    AUTH = ROOT / "packs/powerset/primitives/auth/auth.py"

    def _make_jwt(self, payload: dict) -> str:
        import base64

        def b64(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
        header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        body = b64(json.dumps(payload).encode())
        return f"{header}.{body}.sig"

    def test_inspect_classifies_admin_role(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            creds_path = Path(td) / "credentials.json"
            token = self._make_jwt({
                "email": "arthur@powerset.co",
                "sub": "auth0|abc",
                "aud": "https://api.powerset.dev",
                "iss": "https://aleph-mvp.us.auth0.com/",
                "https://api.powerset.dev/roles": ["admin", "user"],
            })
            creds_path.write_text(json.dumps({
                "access_token": token,
                "refresh_token": "rt",
                "expires_at": 9999999999,
                "email": "arthur@powerset.co",
            }))
            proc = subprocess.run(
                [
                    sys.executable, str(self.AUTH), "inspect",
                    "--credentials-path", str(creds_path),
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["authorization"], "admin")
            self.assertIn("admin", payload["roles"])
            self.assertEqual(payload["email"], "arthur@powerset.co")

    def test_inspect_marks_unauthorized_when_no_user_or_admin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            creds_path = Path(td) / "credentials.json"
            token = self._make_jwt({
                "email": "stranger@example.com",
                "permissions": ["read:public"],
            })
            creds_path.write_text(json.dumps({
                "access_token": token,
                "expires_at": 9999999999,
                "email": "stranger@example.com",
            }))
            proc = subprocess.run(
                [
                    sys.executable, str(self.AUTH), "inspect",
                    "--credentials-path", str(creds_path),
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            # Exit 2 = unauthorized, structured payload still emitted.
            self.assertEqual(proc.returncode, 2)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["authorization"], "unauthorized")
            self.assertEqual(payload["roles"], ["read:public"])


class DoctorTests(unittest.TestCase):
    DOCTOR = ROOT / "packs/powerset/primitives/doctor/doctor.py"

    def test_doctor_emits_structured_report_with_required_check_ids(self) -> None:
        # Run the real doctor on this machine. We don't assert specific
        # statuses (those vary by user/box); we assert the report shape so
        # the $powerset login flow can rely on it.
        proc = subprocess.run(
            [sys.executable, str(self.DOCTOR), "run",
             "--profile", "search-core",
             "--env-file", "/tmp/__doctor_test_no_env__",
             "--gcp-project", "powerset-search"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["primitive"], "powerset_doctor")
        self.assertIn(payload["overall"], ("ok", "warn", "needs_setup"))
        ids = {c["id"] for c in payload["checks"]}
        # `python`, `gcloud_installed`, `auth0_login`, `env_file` are always run.
        # ADC is opt-in because normal Powerpacks workflows do not need it.
        self.assertNotIn("gcloud_adc", ids)
        self.assertIn("python", ids)
        self.assertIn("gcloud_installed", ids)
        self.assertIn("auth0_login", ids)
        self.assertIn("env_file", ids)
        for c in payload["checks"]:
            self.assertIn(c["status"], ("ok", "warn", "missing", "fail"))
            self.assertIsInstance(c["message"], str)
        self.assertIsInstance(payload["counts"], dict)
        self.assertIsInstance(payload["next_actions"], list)
