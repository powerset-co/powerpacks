"""Sign batches of cached LinkedIn images with the server's gateway key."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import _read_env_file

GATEWAY = "https://proxy.powerset.dev"
MAX_BATCH_SIZE = 100
MAX_BODY_BYTES = 256 * 1024


def post_profile_images(handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
    if parsed.path != "/api/profile-image/sign/batch":
        return False

    origin = handler.headers.get("Origin")
    if origin and urllib.parse.urlparse(origin).netloc != handler.headers.get("Host"):
        _respond(handler, HTTPStatus.FORBIDDEN)
        return True

    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY_BYTES:
            raise ValueError("Invalid body length")
        urls = json.loads(handler.rfile.read(length))["urls"]
        if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_BATCH_SIZE:
            raise ValueError("Invalid image batch")
    except (ValueError, KeyError, TypeError):
        _respond(handler, HTTPStatus.BAD_REQUEST)
        return True

    key = os.environ.get("POWERSET_API_KEY") or _read_env_file(Path.cwd() / ".env").get("POWERSET_API_KEY")
    if not key:
        _respond(handler, HTTPStatus.SERVICE_UNAVAILABLE)
        return True

    request = urllib.request.Request(
        f"{GATEWAY}/profile-image/sign/batch",
        data=json.dumps({"urls": urls}).encode(),
        headers={"x-powerset-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            paths = json.loads(response.read())["paths"]
        if not isinstance(paths, list) or len(paths) != len(urls):
            raise ValueError("Invalid signed image batch")
        for path in paths:
            if path is not None and (
                not isinstance(path, str) or not path.startswith("/profile-image?") or "\r" in path or "\n" in path
            ):
                raise ValueError("Invalid signed image path")
    except (OSError, ValueError, KeyError, TypeError):
        _respond(handler, HTTPStatus.BAD_GATEWAY)
        return True

    _respond(handler, HTTPStatus.OK, body=json.dumps({"paths": paths}).encode())
    return True


def _respond(handler: BaseHTTPRequestHandler, status: HTTPStatus, *, body: bytes = b"") -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json" if body else "text/plain")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
