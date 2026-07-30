"""The pinned wacli GO BINARY: resolve it, install it, verify it, invoke it.

Powerpacks runs a pinned powerset-co fork of wacli that forces a full history
sync at pairing. This module is the only place that knows where that binary
lives, which tag it must be built from, how it is downloaded and sha256-verified
before it is trusted, and how a `wacli --json <subcommand>` call is made against
a store. Every other wacli module goes through `binary.wacli_json` /
`binary.wacli_bin` instead of assembling its own binary path.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`
    with the version/asset/download/verify/install constants that only these
    functions read. Behavior unchanged.
  2026-07-26 (binary integrity + honest install flag): the pinned release
    download is verified against per-asset sha256 pins (`WACLI_ASSET_SHA256`,
    next to the version pin) after download and BEFORE the binary is made
    executable or run — a mismatch deletes the file and blocks with a clear
    error. `ensure_wacli_installed(install=False)` now means it: `status` and
    `logout` report an existing binary as-is (even a stale pin) and a missing
    one raises `PrimitiveBlocked` naming the install path instead of silently
    pulling the ~33MB asset.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli import runtime  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.runtime import (  # noqa: E402
    PrimitiveBlocked,
    PrimitiveFailed,
)

# Pinned powerset-co fork of wacli that forces a full history sync at pairing
# (RequireFullSync). We download a prebuilt binary from the fork's GitHub Release
# for this tag — no Go toolchain on the user's machine — and keep it off the
# upstream Homebrew tap so we control the version.
WACLI_REPO = "powerset-co/wacli"
WACLI_PINNED_VERSION = os.environ.get("POWERPACKS_WACLI_VERSION", "v0.14.0-fullsync")
# sha256 of every published release asset for the pinned version above, verified
# after download and BEFORE the binary is made executable or run (see
# `verify_wacli_download`). Computed from the public GitHub release assets; the
# darwin-arm64 pin also matches the binary installs had already fetched. An
# env-overridden POWERPACKS_WACLI_VERSION has no entry here and installs
# unverified — an explicit dev escape hatch, never the shipped default.
WACLI_ASSET_SHA256: dict[str, dict[str, str]] = {
    "v0.14.0-fullsync": {
        "wacli-darwin-arm64": "3cc6e1b31248ef59a37522b52603a4d71bbe87018ec9170e3452d1d0c19d3815",
        "wacli-darwin-amd64": "61e0a9074c35e376739f8b292c69daeb189928d60c2dd117a5cf7ff20c3c25b0",
        "wacli-linux-arm64": "2fc5f45f7082fcac87baff486b718e93e80cd4879c4e2605437a33dc41249cdc",
        "wacli-linux-amd64": "c5e04837578fc48eac031d710d9db7b29d8566f5b4c0e696cc6524b5f4df3a10",
    },
}
WACLI_RELEASE_BASE = os.environ.get(
    "POWERPACKS_WACLI_RELEASE_BASE",
    f"https://github.com/{WACLI_REPO}/releases/download",
)
WACLI_BIN_DIR = Path(os.environ.get("POWERPACKS_WACLI_BIN_DIR", str(Path.home() / ".powerpacks" / "bin")))
WACLI_PINNED_BIN = WACLI_BIN_DIR / "wacli"
# Records which pinned tag the installed binary was built from. `wacli --version`
# only reports the upstream semver (e.g. "0.13.0"), not our fork tag, so we can't
# read the pin off the binary — stamp it at install time and compare on every run
# so a bumped WACLI_PINNED_VERSION triggers a rebuild instead of silently using
# the stale binary.
WACLI_VERSION_STAMP = WACLI_BIN_DIR / ".wacli-version"


def wacli_bin() -> str | None:
    """Resolve the wacli binary, preferring our pinned fork install so a stray
    PATH wacli (e.g. the upstream Homebrew tap) can never shadow it."""
    if WACLI_PINNED_BIN.exists() and os.access(WACLI_PINNED_BIN, os.X_OK):
        return str(WACLI_PINNED_BIN)
    return shutil.which("wacli")


def installed_wacli_version() -> str | None:
    try:
        return WACLI_VERSION_STAMP.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def wacli_pinned_current() -> bool:
    """True when the pinned fork binary is present AND was built from the version
    we currently pin to (so a bumped pin counts as needing a reinstall)."""
    return (
        WACLI_PINNED_BIN.exists()
        and os.access(WACLI_PINNED_BIN, os.X_OK)
        and installed_wacli_version() == WACLI_PINNED_VERSION
    )


def wacli_version(timeout: int = 30) -> dict[str, Any]:
    exe = wacli_bin()
    if not exe:
        raise PrimitiveFailed("wacli is not installed")
    result = runtime.run_command([exe, "--version"], timeout=timeout)
    version = (result.get("stdout") or "").strip()
    if result["returncode"] != 0 or not version:
        raise PrimitiveFailed(((result.get("stderr") or result.get("stdout") or "").strip())[-1000:])
    return {"path": exe, "version": version, "pinned": exe == str(WACLI_PINNED_BIN)}


def wacli_asset_name() -> str | None:
    """Release asset name for this platform, e.g. `wacli-darwin-arm64`, or None
    if we don't publish a prebuilt for it."""
    os_name = {"darwin": "darwin", "linux": "linux"}.get(platform.system().lower())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(platform.machine().lower())
    if not os_name or not arch:
        return None
    return f"wacli-{os_name}-{arch}"


def wacli_download_url() -> str | None:
    asset = wacli_asset_name()
    return f"{WACLI_RELEASE_BASE}/{WACLI_PINNED_VERSION}/{asset}" if asset else None


def download_file(url: str, dest: Path, *, timeout: int = 120) -> None:
    """Stream a URL to dest via a temp file + atomic replace (GitHub release URLs
    redirect to blob storage; urlopen follows redirects)."""
    tmp = dest.with_name(dest.name + ".download")
    request = urllib.request.Request(url, headers={"User-Agent": "powerpacks-whatsapp-wacli"})
    with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(dest)


def file_sha256(path: Path) -> str:
    """Streaming sha256 of a file (the ~33MB binary is never loaded whole)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_wacli_download(dest: Path) -> None:
    """Integrity gate between download and trust (chmod +x / running it).

    Compares the downloaded asset against the sha256 pinned next to the version
    pin (`WACLI_ASSET_SHA256`); a mismatch DELETES the file and blocks with a
    clear error, so a corrupted or tampered asset is never made executable. A
    version with no pinned hashes (env-overridden POWERPACKS_WACLI_VERSION)
    installs unverified — the documented dev escape hatch."""
    asset = wacli_asset_name() or ""
    expected = WACLI_ASSET_SHA256.get(WACLI_PINNED_VERSION, {}).get(asset, "")
    if not expected:
        return
    actual = file_sha256(dest)
    if actual == expected:
        return
    dest.unlink()
    raise PrimitiveBlocked({
        "status": "blocked_user_action",
        "message": (
            f"Downloaded wacli asset {asset} @ {WACLI_PINNED_VERSION} failed sha256 "
            f"verification (got {actual}, pinned {expected}). The file was deleted: "
            "this is a corrupted download or a tampered release asset. Retry, and if "
            "it persists stop and investigate the release before installing."
        ),
    })


def ensure_wacli_installed(*, install: bool = True) -> dict[str, Any]:
    """Resolve the pinned wacli binary, installing/refreshing it when allowed.

    The pinned fork at the currently-pinned version is the only thing that gives
    the full history sync; the prebuilt binary for this platform comes from the
    fork's GitHub Release, sha256-verified before it is trusted.

    `install=False` (status/logout, `--no-install`) means it: an already-present
    binary is reported as-is — even one built from an older pin — and a missing
    one raises `PrimitiveBlocked` naming the install path. Report-only surfaces
    never trigger the ~33MB download (they used to: the flag was a documented
    no-op, so a bare `status` on a fresh machine downloaded the binary)."""
    if wacli_pinned_current():
        return wacli_version()
    if not install:
        if wacli_bin():
            return wacli_version()
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": (
                f"wacli is not installed at {WACLI_PINNED_BIN}. Run the "
                "ensure-wacli subcommand (or $import-messages) to install the "
                f"pinned {WACLI_PINNED_VERSION} binary."
            ),
        })
    stale = WACLI_PINNED_BIN.exists()  # present, but a different (older) pinned tag
    url = wacli_download_url()
    if not url:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": (
                f"No prebuilt wacli for this platform "
                f"({platform.system()}/{platform.machine()}). Build it from "
                f"{WACLI_REPO} @ {WACLI_PINNED_VERSION}, place it at {WACLI_PINNED_BIN}, "
                f"and write {WACLI_PINNED_VERSION} to {WACLI_VERSION_STAMP}."
            ),
        })
    runtime.emit_status(f"{'Updating' if stale else 'Installing'} WhatsApp sync helper ({WACLI_PINNED_VERSION}).")
    WACLI_BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        download_file(url, WACLI_PINNED_BIN)
    except Exception as exc:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": (
                f"Failed to download the pinned wacli binary from {url}: {exc}. "
                "Check network access, then rerun $import-messages."
            ),
            "install_command": f"curl -fsSL {url} -o {WACLI_PINNED_BIN} && chmod +x {WACLI_PINNED_BIN}",
        }) from exc
    verify_wacli_download(WACLI_PINNED_BIN)
    WACLI_PINNED_BIN.chmod(0o755)
    info = wacli_version()  # verify the download actually runs before trusting it
    WACLI_VERSION_STAMP.write_text(WACLI_PINNED_VERSION + "\n", encoding="utf-8")
    return info


def wacli_json(store: Path, args: list[str], *, timeout: int = 300) -> dict[str, Any]:
    cmd = [wacli_bin() or "wacli", "--store", str(store), "--json", *args]
    result = runtime.run_command(cmd, timeout=timeout)
    payload = result.get("json")
    if result["returncode"] != 0:
        raise PrimitiveFailed(((result.get("stderr") or result.get("stdout") or "").strip())[-1000:])
    return payload if isinstance(payload, dict) else {}


def ensure_wacli_report() -> dict[str, Any]:
    """Download/refresh the pinned wacli binary to the current pin. Idempotent
    (a no-op when already current). Called by $update-powerpacks so a pin bump
    reaches the machine without running an import."""
    already_current = wacli_pinned_current()
    info = ensure_wacli_installed()
    return {
        "status": "ok",
        "action": "current" if already_current else "downloaded",
        "pinned_version": WACLI_PINNED_VERSION,
        "wacli": info,
    }
