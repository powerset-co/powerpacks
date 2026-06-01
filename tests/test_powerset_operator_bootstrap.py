import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/powerset/primitives/operator_bootstrap/operator_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("operator_bootstrap", SCRIPT)
operator_bootstrap = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(operator_bootstrap)

OPERATOR_ID = "20000000-0000-0000-0000-000000000001"


def make_jwt(payload: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return f"{b64(json.dumps({'alg': 'none'}).encode())}.{b64(json.dumps(payload).encode())}.sig"


class PowersetOperatorBootstrapTests(unittest.TestCase):
    def temp_workspace(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tmp = Path(td.name)
        self.root_patch = mock.patch.object(operator_bootstrap, "ROOT", tmp)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        return tmp

    def args(self, tmp: Path, **overrides):
        values = {
            "summary_uri": "gs://bucket/bootstrap/summary.json",
            "bundle_dir": Path(".powerpacks/operator-bootstrap/bundles"),
            "registry_dir": Path(".powerpacks/operator-bootstrap/registry"),
            "env_file": "",
            "credentials_path": str(tmp / "credentials.json"),
            "operator_id": "",
            "operator": "",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def run_sync_payload(self, args, download, auth=None) -> tuple[int, dict]:
        buf = io.StringIO()
        with mock.patch.object(operator_bootstrap, "download_gcs_object", side_effect=download):
            with mock.patch.object(
                operator_bootstrap,
                "gcloud_auth_state",
                return_value=auth or {"gcloud_installed": True, "gcloud_active_account": "patrick@powerset.co"},
            ):
                with contextlib.redirect_stdout(buf):
                    code = operator_bootstrap.run_sync(args)
        return code, json.loads(buf.getvalue())

    def test_sync_downloads_matching_operator_from_gcloud_slug(self):
        tmp = self.temp_workspace()
        summary = {
            "operators": [{
                "operator": "patrick",
                "operator_id": OPERATOR_ID,
                "gcs": {"bundle": "gs://bucket/bootstrap/users/patrick/operator-bootstrap.tar.gz"},
            }]
        }

        def download(uri, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            if uri.endswith("summary.json"):
                output.write_text(json.dumps(summary), encoding="utf-8")
            else:
                output.write_bytes(b"bundle")
            return 0, {"status": "ok", "output": str(output)}

        code, payload = self.run_sync_payload(self.args(tmp), download)

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["operator"], "patrick")
        self.assertTrue((tmp / ".powerpacks/operator-bootstrap/bundles/patrick.operator-bootstrap.tar.gz").exists())
        latest = json.loads((tmp / ".powerpacks/operator-bootstrap/registry/latest-sync.json").read_text())
        self.assertEqual(latest["operator_id"], OPERATOR_ID)

    def test_sync_matches_operator_id_from_auth0_user_fixture(self):
        tmp = self.temp_workspace()
        creds = tmp / "credentials.json"
        creds.write_text(json.dumps({
            "access_token": make_jwt({"sub": "auth0|owner", "email": "arthur@powerset.co"}),
            "email": "arthur@powerset.co",
        }), encoding="utf-8")
        fixture = tmp / "postgres-fixture.json"
        fixture.write_text(json.dumps({
            "users": [{
                "id": OPERATOR_ID,
                "user_id": "auth0|owner",
                "email": "arthur@powerset.co",
                "name": "Arthur",
            }]
        }), encoding="utf-8")
        summary = {
            "operators": [{
                "operator": "arthur",
                "operator_id": OPERATOR_ID,
                "gcs": {"bundle": "gs://bucket/bootstrap/users/arthur/operator-bootstrap.tar.gz"},
            }]
        }

        def download(uri, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            if uri.endswith("summary.json"):
                output.write_text(json.dumps(summary), encoding="utf-8")
            else:
                output.write_bytes(b"arthur-bundle")
            return 0, {"status": "ok", "output": str(output)}

        with mock.patch.dict(os.environ, {"POWERPACKS_POSTGRES_FIXTURE_JSON": str(fixture)}, clear=True):
            code, payload = self.run_sync_payload(
                self.args(tmp, credentials_path=str(creds)),
                download,
                auth={"gcloud_installed": True, "gcloud_active_account": ""},
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["operator"], "arthur")
        self.assertIn(OPERATOR_ID, payload["operator_resolution"]["operator_id_candidates"])

    def test_sync_skips_when_no_matching_operator_exists(self):
        tmp = self.temp_workspace()
        summary = {
            "operators": [{
                "operator": "jake",
                "operator_id": "other-op",
                "gcs": {"bundle": "gs://bucket/bootstrap/users/jake/operator-bootstrap.tar.gz"},
            }]
        }

        def download(uri, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(summary), encoding="utf-8")
            return 0, {"status": "ok", "output": str(output)}

        with mock.patch.dict(os.environ, {}, clear=True):
            code, payload = self.run_sync_payload(self.args(tmp), download)

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["reason"], "no_matching_operator_bootstrap")
        self.assertFalse((tmp / ".powerpacks/operator-bootstrap/bundles/jake.operator-bootstrap.tar.gz").exists())

    def test_sync_blocks_when_summary_download_needs_gcloud_reauth(self):
        tmp = self.temp_workspace()

        def download(uri, output):
            return 20, {
                "status": "blocked_user_action",
                "reason": "gcloud_reauthentication_required",
                "reauth_command": "gcloud auth login --no-launch-browser",
            }

        code, payload = self.run_sync_payload(self.args(tmp), download)

        self.assertEqual(code, 20)
        self.assertEqual(payload["status"], "blocked_user_action")
        self.assertEqual(payload["reauth_command"], "gcloud auth login --no-launch-browser")


if __name__ == "__main__":
    unittest.main()
