"""The index runs through the Powerset API (network-search-api, /v2/powerpacks/modal): upload an
input file, start a run, read its status, download an output. The laptop signs in with its Powerset
credentials and holds no Modal token for these; the API derives the operator from the account.
Transfers use gzip level 6; deploy the API gzip routes before releasing this client.

Created: 2026-10-26
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from packs.powerset.primitives.auth import auth
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import DEFAULT_API_BASE

PREFIX = "/v2/powerpacks/modal"


class RunError(RuntimeError):
    """The API refused or could not answer; the message is what it said."""


def _base() -> str:
    return os.environ.get("POWERSET_API_URL", DEFAULT_API_BASE).rstrip("/") + PREFIX


def _token() -> str:
    credentials = auth._credentials_with_fresh_token(
        Path(os.environ.get("POWERPACKS_CREDENTIALS_PATH", str(auth.DEFAULT_CREDENTIALS_PATH))),
        os.environ.get("POWERPACKS_AUTH0_DOMAIN"), os.environ.get("POWERPACKS_AUTH0_CLIENT_ID"))
    return str(credentials["access_token"])


def _call(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(_base() + path, data=data, method=method, headers={
        "Authorization": f"Bearer {_token()}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return {}
        raise RunError(f"{method} {path}: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}") from exc


def upload(path: Path, name: str) -> str:
    """Puts one input file where the API can reach it; returns the object name to start a run with."""
    with tempfile.TemporaryFile() as handle:
        with path.open("rb") as source, gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=6, mtime=0) as compressed:
            shutil.copyfileobj(source, compressed, length=1 << 20)
        size = handle.tell()
        handle.seek(0)
        session = _call("POST", "/uploads", {"name": name + ".gz"})
        request = urllib.request.Request(session["upload_url"], data=handle, method="PUT", headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(size)})
        with urllib.request.urlopen(request, timeout=3600):
            pass
    return str(session["object"])


def start(kind: str, object_name: str, **params: object) -> str:
    """Starts a run (`import-linkedin` or `index-people`) over an uploaded input; returns its label."""
    return str(_call("POST", f"/runs/{kind}", {"object": object_name, **params}, timeout=600)["label"])


def status(label: str) -> dict | None:
    """The run's status.json, or None before the run has written one."""
    return _call("GET", f"/runs/{label}") or None


def download(label: str, name: str, target: Path) -> int:
    """Streams one output file to `target` (the previous copy renamed .bkup); bytes written, -1 if
    the API has no such file."""
    request = urllib.request.Request(f"{_base()}/runs/{label}/files/{name}.gz",
                                     headers={"Authorization": f"Bearer {_token()}"})
    try:
        response = urllib.request.urlopen(request, timeout=3600)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return -1
        raise RunError(f"GET {label}/files/{name}: HTTP {exc.code}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    written = 0
    with response, gzip.GzipFile(fileobj=response, mode="rb") as source, tmp.open("wb") as handle:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            handle.write(chunk)
            written += len(chunk)
    if target.exists():
        backup = target.with_name(target.name + ".bkup")
        target.replace(backup)
        print(f"existing {target.name} renamed to {backup.name}")
    tmp.replace(target)
    return written
