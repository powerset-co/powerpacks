"""Who is signed in to Powerset on this machine, for the side nav's footer: the email, name and
picture the login stored with its credentials (packs/powerset/primitives/auth/auth.py).

Standard library only, like the server's persistent handler: it answers before the project's
Python environment exists. Token values never leave this module.
"""
from __future__ import annotations

import os
from pathlib import Path

from packs.powerset.primitives.auth import auth
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import _read_env_file

ENV_TEMPLATE = Path("packs/powerset/templates/env.powerset.example")


def is_team(email: object) -> bool:
    return isinstance(email, str) and email.lower().endswith("@powerset.co")


def config(root: Path) -> dict[str, str]:
    """The checkout's settings: the template's defaults, its `.env`, then the real environment."""
    values = _read_env_file(root / ENV_TEMPLATE)
    values.update(_read_env_file(root / ".env"))
    values.update(os.environ)
    return values


def credentials_path(root: Path) -> Path:
    return Path(config(root).get("POWERPACKS_CREDENTIALS_PATH", str(auth.DEFAULT_CREDENTIALS_PATH)))


def read(root: Path) -> dict[str, object]:
    """`{"signed_in", "email", "name", "picture"}`; the name and picture are null for a login that
    stored none, and the name too when it is only the email again."""
    credentials = auth._load_credentials(credentials_path(root)) or {}
    email = credentials.get("email")
    if not isinstance(email, str) or not email:
        return {"signed_in": False, "email": None, "name": None, "picture": None}
    name = credentials.get("name")
    return {"signed_in": True, "email": email, "name": name if name != email else None,
            "picture": credentials.get("picture")}
