"""The WhatsApp login QR: detect the payload, render the page, redact the rest.

wacli emits a linked-device pairing string on stdout (or as a `qr_code` event on
stderr) that refreshes every ~20 seconds. `wa_qr_payload` recognizes both wire
forms and returns EXACTLY what WhatsApp must receive; `update_qr_page` renders
it to a PNG via `qrencode` and wraps it in a self-refreshing local HTML page.

A pairing payload is a live credential, so `redact_qr_payloads` scrubs it out of
every diagnostic string before any of it reaches a payload, a log line, or the
user's terminal.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`;
    the auth flow (`auth.py`) now calls into this module for QR rendering and
    redaction. Behavior unchanged.
"""

from __future__ import annotations

import html
import shutil
import subprocess
import sys
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli.runtime import (  # noqa: E402
    PrimitiveBlocked,
    PrimitiveFailed,
)

QR_REDACTION = "[whatsapp qr payload redacted]"
WA_QR_URL_MARKER = "wa.me/settings/linked_devices#2@"


def wa_qr_payload(text: str) -> str | None:
    """Return the exact QR payload to encode if text is a WhatsApp linked-device
    QR, else None. wacli <=0.11 emits a bare `2@...` ref; wacli 0.13 emits a
    `https://wa.me/settings/linked_devices#2@...` URL. WhatsApp must receive
    exactly what wacli emits, so encode the whole string either way (encoding
    only the trailing `2@` fragment of the 0.13 URL is what broke pairing)."""
    stripped = text.strip()
    if stripped.startswith("2@"):
        return stripped
    idx = stripped.find("https://wa.me/")
    if idx != -1 and WA_QR_URL_MARKER in stripped:
        return stripped[idx:].split()[0]
    return None


def redact_qr_payloads(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if wa_qr_payload(stripped) or '"event":"qr_code"' in stripped or '"event": "qr_code"' in stripped:
            lines.append(QR_REDACTION)
        else:
            lines.append(line)
    return "\n".join(lines)


def clear_qr_artifacts(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def write_qr_html(path: Path, png_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel_png = html.escape(png_path.name, quote=True)
    path.write_text(
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"2\">"
        "<title>WhatsApp QR</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "margin:0;background:#f7f7f7;color:#111}"
        "main{text-align:center}img{width:min(82vw,620px);height:auto;"
        "background:white;padding:24px;border-radius:12px}"
        "p{font-size:18px;margin:16px 0 0}</style>"
        "</head><body><main>"
        f"<img src=\"{rel_png}\" alt=\"WhatsApp QR code\">"
        "<p>Scan with WhatsApp > Settings > Linked Devices</p>"
        "</main></body></html>\n",
        encoding="utf-8",
    )


def update_qr_page(payload: str, png_path: Path, html_path: Path, *, open_page: bool) -> None:
    qrencode = shutil.which("qrencode")
    if not qrencode:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "qrencode is required to render the WhatsApp QR page. Install it with `brew install qrencode`, then rerun $import-messages.",
            "install_command": "brew install qrencode",
        })
    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([qrencode, "-s", "10", "-m", "4", "-o", str(png_path), payload], check=True)
    except subprocess.CalledProcessError as exc:
        raise PrimitiveFailed(f"failed to render WhatsApp QR page with qrencode: {exc}") from exc
    write_qr_html(html_path, png_path)
    if open_page and shutil.which("open"):
        subprocess.run(["open", str(html_path)], check=False)
