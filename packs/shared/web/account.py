"""Who is signed in to Powerset on this machine, for the side nav's footer: the stored
credentials' email (packs/powerset/primitives/auth/auth.py), with the name when the token
carries one, and the sign-in the footer starts when nobody is.

Standard library only, like the server's persistent handler: it answers before the project's
Python environment exists. Token values never leave this module.
"""
from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path

from packs.powerset.primitives.auth import auth
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import _read_env_file

ENV_TEMPLATE = Path("packs/powerset/templates/env.powerset.example")
NAME_CLAIMS = ("name", "https://api.powerset.dev/name", "nickname")
START_TIMEOUT_SECONDS = 10


def config(root: Path) -> dict[str, str]:
    """The checkout's settings: the template's defaults, its `.env`, then the real environment."""
    values = _read_env_file(root / ENV_TEMPLATE)
    values.update(_read_env_file(root / ".env"))
    values.update(os.environ)
    return values


def credentials_path(root: Path) -> Path:
    return Path(config(root).get("POWERPACKS_CREDENTIALS_PATH", str(auth.DEFAULT_CREDENTIALS_PATH)))


def read(root: Path) -> dict[str, object]:
    """`{"signed_in", "email", "name"}`; the name is the token's own claim, or null."""
    credentials = auth._load_credentials(credentials_path(root)) or {}
    email = credentials.get("email")
    if not isinstance(email, str) or not email:
        return {"signed_in": False, "email": None, "name": None}
    payload = auth._decode_jwt_payload(str(credentials.get("access_token") or "")) or {}
    name = next((payload[claim] for claim in NAME_CLAIMS
                 if isinstance(payload.get(claim), str) and payload[claim] and payload[claim] != email), None)
    return {"signed_in": True, "email": email, "name": name}


class SignIn:
    """One Powerset sign-in at a time. `start` returns the sign-in page's URL once the login's
    callback server listens; the login finishes on its own when that page reaches the callback
    (the desktop app shows it in its sign-in pane and closes there)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> str:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return self.url
            values = config(self.root)
            args = argparse.Namespace(
                auth0_domain=values.get("POWERPACKS_AUTH0_DOMAIN"), client_id=values.get("POWERPACKS_AUTH0_CLIENT_ID"),
                audience=values.get("POWERPACKS_AUTH0_AUDIENCE"), scopes=auth.DEFAULT_AUTH0_SCOPES,
                callback_host=auth.DEFAULT_CALLBACK_HOST, callback_port=auth.DEFAULT_CALLBACK_PORT,
                force_account=False, no_browser=True, timeout=auth.DEFAULT_LOGIN_TIMEOUT,
                credentials_path=credentials_path(self.root))
            ready = threading.Event()
            outcome: dict[str, str] = {}

            def show(url: str) -> None:
                outcome["url"] = url
                ready.set()

            def run() -> None:
                auth.cmd_login(args, on_authorize_url=show)
                ready.set()

            thread = threading.Thread(target=run, name="powerset-sign-in", daemon=True)
            thread.start()
            ready.wait(START_TIMEOUT_SECONDS)
            if "url" not in outcome:
                raise RuntimeError("The Powerset sign-in could not start. Check the server log and try again.")
            self.thread, self.url = thread, outcome["url"]
            return self.url
