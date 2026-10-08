"""Redirect local avatar requests to signed, cached vendor-gateway images."""

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


def get_profile_image(handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
    if parsed.path != "/api/profile-image":
        return False

    image_url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
    if not image_url.startswith("https://media.licdn.com/"):
        _respond(handler, HTTPStatus.BAD_REQUEST)
        return True

    key = os.environ.get("POWERSET_API_KEY") or _read_env_file(Path.cwd() / ".env").get("POWERSET_API_KEY")
    if not key:
        _respond(handler, HTTPStatus.SERVICE_UNAVAILABLE)
        return True

    request = urllib.request.Request(
        f"{GATEWAY}/profile-image/sign?{urllib.parse.urlencode({'url': image_url})}",
        headers={"x-powerset-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            path = json.loads(response.read())["path"]
        if not isinstance(path, str) or not path.startswith("/profile-image?") or "\r" in path or "\n" in path:
            raise ValueError("Invalid signed image path")
    except (OSError, ValueError, KeyError, TypeError):
        _respond(handler, HTTPStatus.BAD_GATEWAY)
        return True

    _respond(handler, HTTPStatus.FOUND, location=GATEWAY + path)
    return True


def _respond(handler: BaseHTTPRequestHandler, status: HTTPStatus, *, location: str = "") -> None:
    handler.send_response(status)
    if location:
        handler.send_header("Location", location)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
