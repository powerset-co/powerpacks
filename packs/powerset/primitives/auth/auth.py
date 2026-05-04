#!/usr/bin/env python3
"""Auth0 PKCE login for Powerset / search-api access. Stdlib-only.

Subcommands:
    login    Open browser, capture Auth0 callback, save JWT to disk.
    whoami   Print stored credential info (no refresh).
    token    Print a fresh access token, refreshing if needed.
    logout   Remove stored credentials.

Credentials live at `~/.powerpacks/credentials.json` (mode 0600). They are
intentionally separate from `contact-exporter`'s `~/.powerset/credentials.json`
so the two tools don't fight over token state.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.server
import json
import os
import secrets
import string
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Configuration (env-overridable so the primitive can target staging clients)
# ---------------------------------------------------------------------------

DEFAULT_AUTH0_DOMAIN = os.environ.get("POWERPACKS_AUTH0_DOMAIN", "aleph-mvp.us.auth0.com")
DEFAULT_AUTH0_CLIENT_ID = os.environ.get(
    "POWERPACKS_AUTH0_CLIENT_ID", "U7p09NWeJ0jy9M4GiaWa4cz0YVCdDVBl"
)
DEFAULT_AUTH0_AUDIENCE = os.environ.get(
    "POWERPACKS_AUTH0_AUDIENCE", "https://api.powerset.dev"
)
DEFAULT_AUTH0_SCOPES = os.environ.get(
    "POWERPACKS_AUTH0_SCOPES", "openid profile email offline_access"
)
DEFAULT_CALLBACK_PORT = int(os.environ.get("POWERPACKS_AUTH_CALLBACK_PORT", "9876"))
# Auth0 application config whitelists `http://localhost:9876/callback` as the
# allowed redirect URI; using `127.0.0.1` instead would be rejected by Auth0
# even though it resolves to the same address. The HTTP server still binds on
# 127.0.0.1 to avoid IPv6/`::1` ambiguity on some systems; the browser hits
# `localhost`, which the OS resolves to 127.0.0.1.
DEFAULT_CALLBACK_HOST = os.environ.get("POWERPACKS_AUTH_CALLBACK_HOST", "localhost")
DEFAULT_CREDENTIALS_PATH = Path(
    os.environ.get(
        "POWERPACKS_CREDENTIALS_PATH",
        str(Path.home() / ".powerpacks" / "credentials.json"),
    )
)
DEFAULT_LOGIN_TIMEOUT = 180


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _auth0_url(domain: str, path: str) -> str:
    """Resolve a URL for an Auth0-shaped host.

    `domain` may be:
      - a bare hostname (`example.us.auth0.com`) → https:// prefix added
      - a full URL with scheme (`http://127.0.0.1:1234`) → used verbatim, useful
        for tests against a local fake Auth0
    """
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain.rstrip("/") + path
    return f"https://{domain}{path}"


def _post_json(url: str, payload: dict[str, Any], timeout: int = 30) -> tuple[int, dict[str, Any] | None, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8")), ""
            except json.JSONDecodeError:
                return resp.status, None, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed, raw
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def _generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying the signature.

    Safe enough for inspection: we just received the token over HTTPS from
    Auth0 and we are only reading our own claims.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def _decode_jwt_email(token: str) -> str | None:
    payload = _decode_jwt_payload(token) or {}
    return (
        payload.get("email")
        or payload.get("https://api.powerset.dev/email")
        or None
    )


# Auth0 may surface roles under a custom namespace, in `permissions`, or via
# the standard `scope` string. Check all of them so we don't miss a role.
_ROLE_CLAIMS = (
    "https://api.powerset.dev/roles",
    "https://api.powerset.dev/role",
    "https://powerset.dev/roles",
    "roles",
)


def _decode_jwt_roles(token: str) -> list[str]:
    payload = _decode_jwt_payload(token) or {}
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str):
            for raw in value.split():
                role = raw.strip().lower()
                if role and role not in seen:
                    seen.add(role)
                    out.append(role)
        elif isinstance(value, list):
            for item in value:
                _add(item)

    for claim in _ROLE_CLAIMS:
        if claim in payload:
            _add(payload[claim])

    permissions = payload.get("permissions")
    if isinstance(permissions, list):
        for raw in permissions:
            role = str(raw).strip().lower()
            if role and role not in seen:
                seen.add(role)
                out.append(role)

    scope = payload.get("scope")
    _add(scope)
    return out


_REQUIRED_ROLES = ("user", "admin")


def _classify_authorization(roles: list[str]) -> str:
    if "admin" in roles:
        return "admin"
    if "user" in roles:
        return "user"
    return "unauthorized"


# ---------------------------------------------------------------------------
# Credentials file IO (0700 dir, 0600 file)
# ---------------------------------------------------------------------------

def _save_credentials(path: Path, creds: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(json.dumps(creds, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_credentials(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _refresh_credentials(creds: dict[str, Any], domain: str, client_id: str) -> dict[str, Any]:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise SystemExit("session expired and no refresh token; run login")
    status, payload, raw = _post_json(
        _auth0_url(domain, "/oauth/token"),
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"token refresh failed (HTTP {status}): {raw[:200]}")
    return {
        **creds,
        "access_token": payload["access_token"],
        "expires_at": time.time() + int(payload.get("expires_in", 86400)),
        # Auth0 may rotate refresh tokens.
        "refresh_token": payload.get("refresh_token", refresh_token),
        "refreshed_at": now_iso(),
    }


def _credentials_with_fresh_token(
    path: Path, domain: str, client_id: str
) -> dict[str, Any]:
    creds = _load_credentials(path)
    if not creds:
        raise SystemExit("not logged in; run login")
    if time.time() > float(creds.get("expires_at", 0)) - 60:
        creds = _refresh_credentials(creds, domain, client_id)
        _save_credentials(path, creds)
    return creds


# ---------------------------------------------------------------------------
# OAuth callback HTML
# ---------------------------------------------------------------------------

_PAGE_STYLE = """
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0a0a0a;color:#fafafa}
  .card{text-align:center;padding:3rem 4rem;border-radius:16px;background:#141414;
        border:1px solid #262626;max-width:420px}
  .icon{font-size:3rem;margin-bottom:1rem}
  h1{font-size:1.5rem;font-weight:600;margin-bottom:.5rem}
  p{color:#a1a1aa;font-size:.95rem;line-height:1.5}
  .subtle{margin-top:1.5rem;font-size:.8rem;color:#52525b}
</style>
"""

_SUCCESS_HTML = (
    f"<!doctype html><html><head><meta charset='utf-8'><title>Powerpacks</title>{_PAGE_STYLE}</head>"
    "<body><div class='card'><div class='icon'>✅</div><h1>You're in.</h1>"
    "<p>Authentication complete. Head back to your terminal.</p>"
    "<p class='subtle'>You can close this tab.</p></div></body></html>"
)

_ERROR_TEMPLATE = string.Template(
    f"<!doctype html><html><head><meta charset='utf-8'><title>Powerpacks</title>{_PAGE_STYLE}</head>"
    "<body><div class='card'><div class='icon'>❌</div><h1>Login failed</h1>"
    "<p>$ERROR</p><p class='subtle'>Re-run the login primitive.</p></div></body></html>"
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code: str | None = None
    error: str | None = None
    expected_state: str | None = None

    def log_message(self, format, *args):  # noqa: A002 - silence default logging
        return

    def do_GET(self):  # noqa: N802
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        received_state = params.get("state", [None])[0]
        if self.expected_state and received_state != self.expected_state:
            _CallbackHandler.error = "Invalid state parameter (possible CSRF)"
            self._respond(400, _ERROR_TEMPLATE.substitute(ERROR=_CallbackHandler.error))
        elif "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self._respond(200, _SUCCESS_HTML)
        else:
            err = params.get("error_description", [params.get("error", ["unknown"])[0]])[0]
            _CallbackHandler.error = err
            self._respond(400, _ERROR_TEMPLATE.substitute(ERROR=html.escape(err)))
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _respond(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> int:
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)
    callback_url = f"http://{args.callback_host}:{args.callback_port}/callback"

    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None
    _CallbackHandler.expected_state = state

    authorize_params: dict[str, str] = {
        "response_type": "code",
        "client_id": args.client_id,
        "redirect_uri": callback_url,
        "scope": args.scopes,
        "audience": args.audience,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if args.force_account:
        # `prompt=login` forces Auth0 to always show its login screen, even when
        # an SSO cookie already exists. Use this when switching between
        # multiple Powerset/Aleph accounts on the same browser.
        authorize_params["prompt"] = "login"
    authorize_url = _auth0_url(args.auth0_domain, "/authorize?") + urllib.parse.urlencode(
        authorize_params
    )

    try:
        server = http.server.HTTPServer(("127.0.0.1", args.callback_port), _CallbackHandler)
    except OSError as exc:
        emit({
            "primitive": "powerset_auth",
            "command": "login",
            "status": "failed",
            "error": f"could not bind callback server on port {args.callback_port}: {exc}",
        })
        return 1

    if not args.no_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

    print(f"open this URL if your browser did not launch: {authorize_url}", file=sys.stderr)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thread.join(timeout=args.timeout)
    if thread.is_alive():
        server.shutdown()
        server.server_close()
        emit({
            "primitive": "powerset_auth",
            "command": "login",
            "status": "failed",
            "error": "login timed out",
        })
        return 1
    server.server_close()

    if _CallbackHandler.error:
        emit({
            "primitive": "powerset_auth",
            "command": "login",
            "status": "failed",
            "error": _CallbackHandler.error,
        })
        return 1
    if not _CallbackHandler.auth_code:
        emit({
            "primitive": "powerset_auth",
            "command": "login",
            "status": "failed",
            "error": "no authorization code received",
        })
        return 1

    status, token_payload, raw = _post_json(
        _auth0_url(args.auth0_domain, "/oauth/token"),
        {
            "grant_type": "authorization_code",
            "client_id": args.client_id,
            "code": _CallbackHandler.auth_code,
            "code_verifier": verifier,
            "redirect_uri": callback_url,
        },
    )
    if status != 200 or not isinstance(token_payload, dict):
        emit({
            "primitive": "powerset_auth",
            "command": "login",
            "status": "failed",
            "error": f"token exchange failed (HTTP {status})",
            "details": raw[:500] if raw else None,
        })
        return 1

    access_token = token_payload["access_token"]
    creds = {
        "access_token": access_token,
        "refresh_token": token_payload.get("refresh_token"),
        "expires_at": time.time() + int(token_payload.get("expires_in", 86400)),
        "email": _decode_jwt_email(access_token),
        "audience": args.audience,
        "auth0_domain": args.auth0_domain,
        "client_id": args.client_id,
        "logged_in_at": now_iso(),
    }
    _save_credentials(args.credentials_path, creds)

    emit({
        "primitive": "powerset_auth",
        "command": "login",
        "status": "ok",
        "credentials_path": str(args.credentials_path),
        "email": creds["email"],
        "expires_at": creds["expires_at"],
    })
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    creds = _load_credentials(args.credentials_path)
    if not creds:
        emit({
            "primitive": "powerset_auth",
            "command": "whoami",
            "status": "anonymous",
            "credentials_path": str(args.credentials_path),
        })
        return 1
    expires_at = float(creds.get("expires_at", 0))
    seconds_remaining = max(0, int(expires_at - time.time()))
    emit({
        "primitive": "powerset_auth",
        "command": "whoami",
        "status": "logged_in",
        "credentials_path": str(args.credentials_path),
        "email": creds.get("email"),
        "expires_at": expires_at,
        "seconds_remaining": seconds_remaining,
        "expired": seconds_remaining == 0,
        "auth0_domain": creds.get("auth0_domain"),
        "audience": creds.get("audience"),
    })
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    try:
        creds = _credentials_with_fresh_token(
            args.credentials_path,
            args.auth0_domain,
            args.client_id,
        )
    except SystemExit as exc:
        emit({
            "primitive": "powerset_auth",
            "command": "token",
            "status": "failed",
            "error": str(exc),
        })
        return 1
    if args.bearer_only:
        # Plain text on stdout, suitable for `--header "Authorization: Bearer $(... token --bearer-only)"`.
        print(creds["access_token"])
        return 0
    emit({
        "primitive": "powerset_auth",
        "command": "token",
        "status": "ok",
        "access_token": creds["access_token"],
        "expires_at": creds.get("expires_at"),
        "email": creds.get("email"),
    })
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show identity + role classification from the cached JWT.

    Refreshes the token if needed, then decodes it locally to surface email,
    roles, and a coarse `authorization` (admin / user / unauthorized) so
    `powerset-login` can decide what to do next.
    """
    try:
        creds = _credentials_with_fresh_token(
            args.credentials_path, args.auth0_domain, args.client_id
        )
    except SystemExit as exc:
        emit({
            "primitive": "powerset_auth",
            "command": "inspect",
            "status": "failed",
            "error": str(exc),
        })
        return 1

    token = creds["access_token"]
    payload = _decode_jwt_payload(token) or {}
    roles = _decode_jwt_roles(token)
    authorization = _classify_authorization(roles)

    out = {
        "primitive": "powerset_auth",
        "command": "inspect",
        "status": "ok" if authorization != "unauthorized" else "unauthorized",
        "credentials_path": str(args.credentials_path),
        "email": _decode_jwt_email(token),
        "sub": payload.get("sub"),
        "audience": payload.get("aud"),
        "issuer": payload.get("iss"),
        "expires_at": creds.get("expires_at"),
        "roles": roles,
        "authorization": authorization,
        "required_any_of": list(_REQUIRED_ROLES),
    }
    emit(out)
    if authorization == "unauthorized" and not args.allow_unauthorized:
        return 2
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    existed = args.credentials_path.exists()
    if existed:
        try:
            args.credentials_path.unlink()
        except OSError as exc:
            emit({
                "primitive": "powerset_auth",
                "command": "logout",
                "status": "failed",
                "error": str(exc),
            })
            return 1
    emit({
        "primitive": "powerset_auth",
        "command": "logout",
        "status": "ok",
        "removed": existed,
        "credentials_path": str(args.credentials_path),
    })
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credentials-path", type=Path, default=DEFAULT_CREDENTIALS_PATH)
    parser.add_argument("--auth0-domain", default=DEFAULT_AUTH0_DOMAIN)
    parser.add_argument("--client-id", default=DEFAULT_AUTH0_CLIENT_ID)


def main() -> None:
    parser = argparse.ArgumentParser(description="Powerset Auth0 PKCE login")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Run the Auth0 PKCE login flow")
    add_common_args(login)
    login.add_argument("--audience", default=DEFAULT_AUTH0_AUDIENCE)
    login.add_argument("--scopes", default=DEFAULT_AUTH0_SCOPES)
    login.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    login.add_argument("--callback-host", default=DEFAULT_CALLBACK_HOST,
                       help="Hostname Auth0 redirects to (default: localhost). "
                            "Must match the Auth0 application's allowed callback URL exactly.")
    login.add_argument("--timeout", type=int, default=DEFAULT_LOGIN_TIMEOUT)
    login.add_argument("--no-browser", action="store_true",
                       help="Do not auto-open the system browser; print the URL only.")
    login.add_argument("--force-account", action="store_true",
                       help="Force Auth0 to show the login screen even if an SSO "
                            "cookie exists (use when switching accounts).")
    login.set_defaults(func=cmd_login)

    whoami = sub.add_parser("whoami", help="Print stored credential metadata")
    add_common_args(whoami)
    whoami.set_defaults(func=cmd_whoami)

    token = sub.add_parser("token", help="Print a fresh access token (auto-refresh if expiring)")
    add_common_args(token)
    token.add_argument("--bearer-only", action="store_true", help="Print just the access token, no JSON")
    token.set_defaults(func=cmd_token)

    inspect = sub.add_parser(
        "inspect",
        help="Decode the cached JWT, return email + roles + authorization class",
    )
    add_common_args(inspect)
    inspect.add_argument(
        "--allow-unauthorized",
        action="store_true",
        help="Exit 0 even when the user has neither user nor admin role.",
    )
    inspect.set_defaults(func=cmd_inspect)

    logout = sub.add_parser("logout", help="Delete the stored credentials")
    add_common_args(logout)
    logout.set_defaults(func=cmd_logout)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
