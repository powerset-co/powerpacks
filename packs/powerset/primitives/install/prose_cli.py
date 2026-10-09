"""Read the install script, or play it on the status page.

  bin/status-prose              print every event in run order: row, state, line, note
  bin/status-prose play         play a setup run on the real page from a scratch folder
  bin/status-prose play --every play every event, failures and waits included
  bin/status-prose play --wait  advance one event per Enter instead of a timer

`play` serves the page from a fresh folder under /tmp, never a real install, and
prints each event as the page shows it.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from packs.shared.web.server import start_server
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE, ROWS, render, source_counts
from packs.powerset.primitives.install.steps import DEFAULT_PLAN, InstallStep

_REPO = Path(__file__).resolve().parents[4]
# Synthetic values for every placeholder the script uses.
_SAMPLE = {
    "email": "casey@example.com", "network": "Personal Network", "count": 328, "code": 503,
    "read": 120, "total": 298, "done": 120, "connections": 294, "added": 294, "messages": 2645,
    "counts": source_counts({"gmail": 44, "messages": 120}), "people": 381, "follow_ups": "", "app": "Codex",
}
_PLAN = [*DEFAULT_PLAN, "sources", "linkedin_login", "gmail_tools", "gmail_login", "imessage_access",
         "whatsapp_tools", "whatsapp_login", "linkedin", "gmail_sync", "gmail_import", "imessage_import",
         "whatsapp_sync", "whatsapp_import", "deep_context", "enrich", "index", "validate", "ready"]
# A run that goes right, as the page sees it: (event, step for an event without its own, values).
_RUN = [
    "install.preparing_mac", "install.dependencies_ready", "install.adding_skills", "install.skills_ready",
    "account.signing_in", "account.connected", "credentials.preparing", "credentials.ready",
    "connection.connecting", "connection.done", "network.checking", "network.checking_one", "network.ready",
    "sources.selected",
    ("tools.preparing", InstallStep.LINKEDIN_LOGIN), ("tools.ready", InstallStep.LINKEDIN_LOGIN),
    ("tools.preparing", InstallStep.GMAIL_TOOLS), ("tools.ready", InstallStep.GMAIL_TOOLS),
    ("tools.preparing", InstallStep.WHATSAPP_TOOLS), ("tools.ready", InstallStep.WHATSAPP_TOOLS),
    "linkedin.login.checking", "linkedin.login.done",
    "gmail.checking", "gmail.app.starting", "gmail.app.sign_in", "gmail.app.naming", "gmail.app.permissions",
    "gmail.app.client", "gmail.app.ready", "gmail.allowing", "gmail.allowed", "gmail.connect", "gmail.connected",
    "imessage.checking", "imessage.permission", "imessage.allowed",
    "whatsapp.qr", "whatsapp.linked",
    "linkedin.reading", ("linkedin.reading.count", None, {"read": 60}), ("linkedin.reading.count", None, {"read": 180}),
    "linkedin.done.read",
    "gmail.syncing", "gmail.synced", "gmail.importing", "gmail.imported",
    "imessage.reading", "imessage.importing", "imessage.imported",
    "whatsapp.downloading", ("whatsapp.downloading.count", None, {"messages": 1200}),
    ("whatsapp.downloading.count", None, {"messages": 4800}), "whatsapp.syncing", "whatsapp.synced",
    "whatsapp.importing", "whatsapp.imported",
    "discover.linkedin", "discover.owner", "discover.people", "discover.reading", "discover.estimating",
    "discover.learning", ("discover.learning.count", None, {"done": 40}),
    ("discover.learning.count", None, {"done": 298}), "discover.duplicates", "discover.combining",
    "discover.grouping", "discover.done",
    "enrich.running", ("enrich.researching.count", None, {"done": 12, "total": 30}),
    ("enrich.judging.count", None, {"done": 9, "total": 21}), "enrich.done",
    "index.preparing", "index.building", "index.done",
    "validate.checking", "validate.done", "search.ready",
]


def _entry(item: str | tuple) -> tuple[str, InstallStep | None, dict]:
    """A `_RUN` item as (event, step, values)."""
    if isinstance(item, str):
        return item, None, {}
    event, step, *values = item
    return event, step, values[0] if values else {}


def print_script() -> None:
    """The script, row by row, then the events that belong to whichever step the run is on."""
    for row in ROWS:
        print(f"\n## {row.label}")
        for event, prose in PROSE.items():
            if prose.step in row.steps:
                _print_event(event)
    print("\n## Not shown in a row")
    for event, prose in PROSE.items():
        if prose.step and not any(prose.step in row.steps for row in ROWS):
            _print_event(event)
    print("\n## Any step (the step the run is on)")
    for event, prose in PROSE.items():
        if prose.step is None:
            _print_event(event)


def _print_event(event: str) -> None:
    prose = PROSE[event]
    print(f"  {event}  [{prose.state.value}{', ' + prose.action if prose.action else ''}]")
    print(f"      {prose.line}")
    if prose.note:
        print(f"      note: {prose.note}")


def play(*, every: bool, wait: bool, delay: float, port: int, open_browser: bool = True) -> None:
    root = Path(tempfile.mkdtemp(prefix="status-prose-"))
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO), os.environ.get("PYTHONPATH", "")]))
    status = InstallStatus(root)
    run = [(event, None, {}) for event in PROSE] if every else [_entry(item) for item in _RUN]
    for index, (event, step, values) in enumerate(run):
        # An event without its own step lands on the step the run is on.
        record = status.write(event, step=step, pid=os.getpid(), plan=_PLAN, retry_command="bin/onboard",
                              account_email=_SAMPLE["email"], network_name=_SAMPLE["network"],
                              person_count=_SAMPLE["count"], **{**_SAMPLE, **values})
        if index == 0:
            page = start_server(root, port=port, stage="install", open_browser=open_browser)
            print(f"Status page: {page['url']} (scratch folder {root})\n")
        print(f"{event:32} {record['message']}" + (f"\n{'':32} note: {record['note']}" if record["note"] else ""))
        if wait:
            input()
        else:
            time.sleep(delay)
    _, line, _ = render("search.ready", _SAMPLE)
    print(f"\nDone. The page stays up; stop it with: lsof -ti tcp:{port} | xargs kill  ({line})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["print", "play"], default="print")
    parser.add_argument("--every", action="store_true", help="play every event, not just a run that goes right")
    parser.add_argument("--wait", action="store_true", help="advance one event per Enter")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds per event")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--no-open", action="store_true", help="do not open the page in the browser")
    args = parser.parse_args()
    if args.command == "print":
        print_script()
        return
    play(every=args.every, wait=args.wait, delay=args.delay, port=args.port, open_browser=not args.no_open)
    sys.exit(0)


if __name__ == "__main__":
    main()
