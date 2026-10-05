"""Local setup actions preserve an active installation and expired QR codes."""

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from argparse import Namespace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.install.controller import InstallController, permission_app
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.steps import InstallState, InstallStep


class Request:
    def __init__(self, record, origin=None):
        body = json.dumps(record).encode()
        self.rfile = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body)), "Host": "127.0.0.1:8899"}
        if origin:
            self.headers["Origin"] = origin

    def _json(self, payload, status=200):
        self.payload, self.status = payload, status


class InstallControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.controller = InstallController(self.root)

    def test_other_site_cannot_open_settings(self):
        request = Request({}, "https://another.example")
        with patch("packs.powerset.primitives.install.controller.subprocess.run") as run:
            self.controller.post(request, "/api/install/permissions")
        self.assertEqual(request.status, 403)
        run.assert_not_called()

    def test_detached_server_retains_the_app_that_started_it(self):
        app = self.root / "Example.app"
        app.mkdir()
        with patch.dict(os.environ, {"POWERPACKS_PERMISSION_APP": str(app)}), patch("subprocess.check_output", return_value="1 /usr/bin/python"):
            self.assertEqual(permission_app(), str(app))

    def test_permission_button_opens_settings_and_highlights_actual_app(self):
        InstallStatus(self.root).write('step.waiting', step=InstallStep.IMESSAGE_ACCESS, pid=os.getpid(), action={"kind": "permission"})
        request = Request({})
        with patch("packs.powerset.primitives.install.controller.permission_app", return_value="/Applications/Example.app"), patch("subprocess.run") as run:
            self.controller.post(request, "/api/install/permissions")
        self.assertEqual(request.status, 202)
        self.assertEqual(run.call_args_list[1].args[0], ["open", "-R", "/Applications/Example.app"])

    def test_review_opens_in_the_default_browser(self):
        request = Request({})
        with patch("subprocess.run") as run:
            self.controller.post(request, "/api/install/review")
            self.assertEqual(request.status, 202)
            run.assert_called_once_with(["open", "http://127.0.0.1:8899/?stage=linkedin"], check=True)

    def test_only_current_waiting_qr_is_served(self):
        from packs.ingestion.primitives.deep_context.review.cli import _persistent_handler

        qr = self.root / ".powerpacks/messages/wacli-login-qr.png"
        qr.parent.mkdir(parents=True)
        qr.write_bytes(b"synthetic-qr-image")
        os.utime(qr, (1, 1))
        status = InstallStatus(self.root)
        status.write('whatsapp.blocked', pid=os.getpid(), action={"kind": "qr"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), _persistent_handler(self.root, Namespace(confirm_threshold=None)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        def get():
            with urllib.request.urlopen(url + "/api/install") as response:
                return json.load(response)
        self.assertNotIn("qr_url", get()["action"])
        qr.touch()
        self.assertIn("qr_url", get()["action"])
        with urllib.request.urlopen(url + "/api/install/qr") as response:
            self.assertEqual(response.read(), b"synthetic-qr-image")
        status.write('whatsapp.failed', pid=os.getpid(), action={"kind": "qr"})
        self.assertNotIn("qr_url", get()["action"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + "/api/install/qr")
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
