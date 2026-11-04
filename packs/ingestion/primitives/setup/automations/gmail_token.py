"""Whether a mailbox's saved Google grant still works, asked of Google, not of msgvault.

msgvault opens (and migrates) its whole database on every command, so `msgvault verify` takes
seconds on a large archive and can time out, and a time-out read as "not connected". Whether a
mailbox is signed in needs only its token file and the OAuth client that issued it:

    token     <data_dir>/tokens/<email>.json   msgvault's oauth2.Token plus `client_id`
    client    the client_secrets JSON in config.toml whose client_id matches the token's

An access token that is still valid answers without the network. Otherwise one refresh-token
grant at Google's token endpoint decides: success is healthy, `invalid_grant` (a 7-day
Testing-status grant lapsing, or a revoked one) needs the user, and anything else is transient.
The refreshed access token is thrown away: msgvault owns the token file, and Google keeps the
refresh token valid.

`None` means this module can't decide (no token file, an unknown client, a token kept by a
command), and the caller asks msgvault instead.

Changelog:
  2026-11-04: created, so the Accounts and Connect sign-in check never opens the archive.
"""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from packs.ingestion.primitives.setup.automations.msgvault_home import config_path, parse_client_secret_paths

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# A token this close to expiring counts as expired: it could lapse before the next sync uses it.
EXPIRY_MARGIN = timedelta(minutes=2)
REFRESH_TIMEOUT_SECONDS = 15

Verdict = Literal["healthy", "reauthorization_required", "transient_error"]


@dataclass(frozen=True)
class TokenCheck:
    status: Verdict
    error: str = ""
    network_called: bool = False


def check_token(home: Path, email: str, *, now: datetime | None = None) -> TokenCheck | None:
    """This mailbox's grant, from its token file and Google; None when msgvault has to answer."""
    token = _read_json(tokens_dir(home) / f"{_safe_email(email)}.json")
    if not token or not (token.get("refresh_token") or token.get("access_token")):
        return None
    if token.get("access_token") and _unexpired(token.get("expiry"), now or datetime.now(timezone.utc)):
        return TokenCheck("healthy")
    client = _client_for(home, str(token.get("client_id") or ""))
    if client is None or not token.get("refresh_token"):
        return None
    client_id, client_secret, token_url = client
    return _refresh(token_url, client_id, client_secret, str(token["refresh_token"]))


def tokens_dir(home: Path) -> Path:
    """msgvault's token folder: `tokens` under config.toml's data_dir, which defaults to the home."""
    data_dir = home
    try:
        configured = tomllib.loads(config_path(home).read_text()).get("data", {}).get("data_dir")
    except (OSError, tomllib.TOMLDecodeError):
        configured = None
    if configured:
        data_dir = Path(configured).expanduser()
        if not data_dir.is_absolute():
            data_dir = home / data_dir
    return data_dir / "tokens"


def _safe_email(email: str) -> str:
    """msgvault's sanitizeEmail: the token file's name for an address."""
    return email.replace("/", "_").replace("\\", "_").replace("..", "_")


def _unexpired(expiry: object, now: datetime) -> bool:
    if not isinstance(expiry, str) or not expiry:
        return False
    try:
        # Go writes RFC 3339 with up to nanoseconds; Python reads microseconds.
        head, _, rest = expiry.partition(".")
        if rest:
            digits = "".join(ch for ch in rest if ch.isdigit())
            zone = rest[len(digits):]
            expiry = f"{head}.{digits[:6]}{zone}"
        when = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None or when.year < 2000:  # Go's zero time means "never set"
        return False
    return when - EXPIRY_MARGIN > now


def _client_for(home: Path, client_id: str) -> tuple[str, str, str] | None:
    """The configured OAuth client that issued the token (the only one if the token doesn't say)."""
    clients = []
    for path in parse_client_secret_paths(config_path(home)).values():
        installed = (_read_json(Path(path).expanduser()) or {}).get("installed")
        if isinstance(installed, dict) and installed.get("client_id") and installed.get("client_secret"):
            clients.append((str(installed["client_id"]), str(installed["client_secret"]),
                            str(installed.get("token_uri") or GOOGLE_TOKEN_URL)))
    if client_id:
        return next((client for client in clients if client[0] == client_id), None)
    return clients[0] if len(clients) == 1 else None


def _refresh(token_url: str, client_id: str, client_secret: str, refresh_token: str) -> TokenCheck:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()
    request = urllib.request.Request(token_url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT_SECONDS) as response:
            response.read()
        return TokenCheck("healthy", network_called=True)
    except urllib.error.HTTPError as error:
        try:
            reason = json.loads(error.read() or b"{}").get("error", "")
        except (json.JSONDecodeError, AttributeError):
            reason = ""
        if reason == "invalid_grant":
            return TokenCheck("reauthorization_required", "Google: invalid_grant (the sign-in expired or was revoked)",
                              network_called=True)
        return TokenCheck("transient_error", f"Google token endpoint: HTTP {error.code} {reason}".strip(),
                          network_called=True)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return TokenCheck("transient_error", f"Google token endpoint unreachable: {error}", network_called=True)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
