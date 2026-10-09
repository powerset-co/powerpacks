"""Prepare optional import tools through their existing binary installers.

No account login, mailbox sync, WhatsApp pairing, or source-data changes.
Gmail and LinkedIn drive a browser with playwright-core: they need node/npm and
Google Chrome or Brave (`common/browser.js` picks between them).

Changelog:
  2026-10-03: LinkedIn is a source here; Brave counts as the browser.
"""
from __future__ import annotations

import argparse
import os
import shutil
from enum import Enum
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import emit
from packs.ingestion.primitives.discover.messages.wacli import binary
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked, PrimitiveFailed
from packs.ingestion.primitives.setup.automations import msgvault_home, shell


class ImportSource(str, Enum):
    GMAIL = "gmail"
    LINKEDIN = "linkedin"
    WHATSAPP = "whatsapp"


BROWSER_APPS = (Path("/Applications/Google Chrome.app"), Path("/Applications/Brave Browser.app"))


HOMEBREW_INSTALL_COMMAND = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'


class ImportTools:
    def __init__(self, *, sources: tuple[str, ...], desktop: bool = False) -> None:
        self.sources = tuple(ImportSource(source) for source in sources)
        # The desktop app signs in to LinkedIn and Google itself: no node, Chrome or gcloud, and
        # no Homebrew for them. Only msgvault, wacli, and qrencode where brew already exists.
        self.desktop = desktop

    def run(self) -> dict:
        # msgvault's installer and Homebrew put binaries here; a non-login
        # agent shell need not have loaded their profile PATH entries.
        paths = [Path.home() / ".local/bin", Path.home() / ".powerpacks/bin",
                 Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys([
            *os.environ.get("PATH", "").split(os.pathsep),
            *(str(path) for path in paths if path.is_dir()),
        ]))
        packages = []
        installed = {}
        # deep-context runs its pipeline in a tmux session under a second agent, on every Mac.
        if not shutil.which("tmux"):
            packages.append(("tmux", ["tmux"]))
        if ImportSource.GMAIL in self.sources and not shutil.which("gcloud"):
            packages.append(("gcloud", ["--cask", "gcloud-cli"]))
        if {ImportSource.GMAIL, ImportSource.LINKEDIN} & set(self.sources):
            if not shutil.which("node") or not shutil.which("npm"):
                packages.append(("node", ["node"]))
            if not any(app.is_dir() for app in BROWSER_APPS):
                packages.append(("chrome", ["--cask", "google-chrome"]))
        if ImportSource.WHATSAPP in self.sources and not shutil.which("qrencode"):
            packages.append(("qrencode", ["qrencode"]))
        if self.desktop:
            packages = [package for package in packages if package[0] == "qrencode" and shutil.which("brew")]
        if packages:
            brew = shutil.which("brew")
            if not brew:
                return {"status": "needs_user_action",
                        "message": "Your Mac password is needed to prepare import tools.",
                        "command": HOMEBREW_INSTALL_COMMAND}
            for name, args in packages:
                shell.progress(f"Installing {name}.")
                result = shell.run_command([brew, "install", *args], timeout=900)
                if not result.ok:
                    return {"status": "failed", "message": f"Could not install {name}: {shell.command_error(result)}"}
        if ImportSource.GMAIL in self.sources:
            shell.progress("Preparing Gmail import.")
            installed["msgvault"] = msgvault_home.ensure_msgvault(install=True)
            if not installed["msgvault"]["installed"]:
                return {"status": "failed", "message": "Could not install msgvault.", "tools": installed}
        if ImportSource.WHATSAPP in self.sources:
            try:
                installed["wacli"] = binary.ensure_wacli_report()
            except (PrimitiveBlocked, PrimitiveFailed, OSError) as exc:
                # The binary primitive also labels network/integrity errors
                # blocked. An agent can retry or investigate these itself.
                return {"status": "failed", "message": str(exc), "tools": installed}
        return {"status": "ok", "message": "Contact import tools are ready", "tools": installed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=[source.value for source in ImportSource], action="append", required=True)
    args = parser.parse_args()
    payload = ImportTools(sources=tuple(args.source)).run()
    emit(payload)
    raise SystemExit({"ok": 0, "needs_user_action": 10, "failed": 1}[payload["status"]])


if __name__ == "__main__":
    main()
