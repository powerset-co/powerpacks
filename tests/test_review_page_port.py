"""The page server takes its port from another Powerpacks page, never from another app."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.review import cli

# A 3.13-style page: answers /healthz without saying whose checkout it serves.
_SERVER = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status": "ok"}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class OtherServerTests(unittest.TestCase):
    def _serve(self, *marker: str) -> tuple[subprocess.Popen, int]:
        port = _free_port()
        process = subprocess.Popen([sys.executable, "-c", _SERVER, str(port), *marker])
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    return process, port
            time.sleep(0.05)
        self.fail("test server did not start")

    def test_an_older_powerpacks_page_on_the_port_is_stopped(self):
        process, port = self._serve("packs.ingestion.primitives.deep_context.review.cli")

        self.assertIsNone(cli._owned_listener("127.0.0.1", port, Path("/tmp/other-checkout")))
        self.assertIsNotNone(process.wait(timeout=5))
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_another_app_on_the_port_is_left_running(self):
        process, port = self._serve()

        with self.assertRaises(SystemExit):
            cli._owned_listener("127.0.0.1", port, Path("/tmp/other-checkout"))
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
