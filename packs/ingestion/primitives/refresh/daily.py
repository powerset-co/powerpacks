#!/usr/bin/env python3
"""The native scheduled refresh: the local server runs it daily, weekly or monthly (it keeps running in the background when the window is closed).

One run is what Accounts' "Enrich imported data" does, unattended:

    renew expired Gmail sign-ins once with the saved Google session (as the Codex task did)
    (a Powerset sign-in that is gone stops the run here: setup's sign-in would wait for a browser)
    -> bin/onboard --refresh over the connected sources (desktop mode: a source that is not
       signed in is skipped, never waited on), which syncs and imports them, runs Deep Context
       (v2 keys every stage, so only what is new is processed) and rebuilds search only when
       people.csv changed
    -> what needs the person (an expired sign-in, Full Disk Access off, WhatsApp unlinked,
       LinkedIn signed out, a stopped run), each with the one thing to do about it.

Each run is one row of `.powerpacks/refresh/runs.sqlite` (`refresh_runs`): its trigger, status,
sources and accounts synced, people before and after, whether search was rebuilt, problems,
errors and timing. Settings (on/off, how often, the day and the time) are
`.powerpacks/refresh/settings.json`.

Schedule: daily, weekly (a day of the week) or monthly (a day of the month; the 29th to 31st fall
on a shorter month's last day), at the chosen time. A run the Mac was off or asleep for runs once
as soon as the app is open again, unless the next one is due within 6 hours (then it waits for
that one). However many were missed, they fold into that one run; none started before the refresh
was turned on or moved to another day count as missed. The server's scheduler thread (`scheduler`) checks every
minute; the run itself is a detached child (`run`), so it survives a server restart, and a
row whose process is gone is closed as stopped.

The Codex and Claude scheduled tasks (tasks.py) stay for anyone who wants them; the app no
longer offers or installs them.

Changelog:
  2026-11-05: a stopped refresh points at Scheduled tasks, not Accounts' import view, in its own words.
  2026-11-04: weekly (day of week) and monthly (day of month) schedules besides daily; a missed
    run catches up as soon as it can unless the next one is within 6 hours; new schedules are
    weekly on Monday at 9:00 AM (an older settings file stays daily).
  2026-11-04: Gmail's problem is one short line; the page is named Scheduled tasks again.
  2026-11-04: Gmail's problem points at the Refresh page, which reconnects each expired mailbox there.
  2026-11-04: a mailbox Google still wants the person for no longer stops the others: the run
    syncs the signed-in ones (its Gmail accounts) and reports the rest.
  2026-11-03: Gmail renewal also signs back in a mailbox whose token is gone; the problem says the
    automatic sign-in was tried.
  2026-11-03: created; replaces the app's Codex/Claude scheduled task with a native daily run.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from packs.ingestion.primitives.common.proc import alive

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# "daily" is every scheduled run, whatever the frequency (rows written before weekly existed).
Trigger = Literal["daily", "manual"]
Frequency = Literal["daily", "weekly", "monthly"]
FREQUENCIES: tuple[Frequency, ...] = ("daily", "weekly", "monthly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
Status = Literal["running", "ok", "needs_you", "failed"]
SourceName = Literal["linkedin", "gmail", "imessage", "whatsapp"]

DIRECTORY = Path(".powerpacks/refresh")
DATABASE = DIRECTORY / "runs.sqlite"
SETTINGS = DIRECTORY / "settings.json"
INSTALL_MANIFEST = Path(".powerpacks/install/manifest.json")
ONBOARD_LOCK = Path(".powerpacks/install/onboard.lock")
PEOPLE_CSV = Path(".powerpacks/search-index/people.csv")
INDEX_FINGERPRINT = Path(".powerpacks/search-index/people.sha256")
CONNECTIONS_CSV = Path(".powerpacks/network-import/discover/linkedin/Connections.csv")
IMESSAGE_CONTACTS = Path(".powerpacks/messages/imessage.contacts.csv")
MAX_RUNS = 60
TICK_SECONDS = 60
# A missed run waits for the next one instead when that is this close.
CATCH_UP_WINDOW = timedelta(hours=6)
SOURCES: tuple[SourceName, ...] = ("linkedin", "gmail", "imessage", "whatsapp")
# The install step that holds each source's import.
IMPORT_STEP: dict[SourceName, str] = {"linkedin": "linkedin", "gmail": "gmail_import",
                                      "imessage": "imessage_import", "whatsapp": "whatsapp_import"}
ACCOUNTS_HREF = "/accounts?source="
# Scheduled tasks (/tasks), which reconnects each expired Gmail mailbox in place.
REFRESH_HREF = "/tasks"
STOPPED_DETAIL = "It didn't finish. Refresh now picks up where it left off."

SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL DEFAULT 0,
    missed_days INTEGER NOT NULL DEFAULT 0,
    sources TEXT NOT NULL DEFAULT '[]',
    people_before INTEGER,
    people_after INTEGER,
    index_rebuilt INTEGER NOT NULL DEFAULT 0,
    problems TEXT NOT NULL DEFAULT '[]',
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT ''
)
"""


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    time: str = "09:00"
    # When it was last turned on or moved to another day: a slot before this is not a missed run.
    enabled_at: str = ""
    # New schedules are weekly; a settings file from before weekly existed was daily (read_settings).
    frequency: Frequency = "weekly"
    # Weekly: 0 is Monday. Monthly: 1 to 31.
    weekday: int = 0
    day: int = 1

    def __post_init__(self) -> None:
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.time) is None:
            raise ValueError("Time must be HH:MM in 24-hour time.")
        if self.frequency not in FREQUENCIES:
            raise ValueError("Repeat must be daily, weekly or monthly.")
        if not 0 <= self.weekday <= 6:
            raise ValueError("Day of the week must be 0 (Monday) to 6 (Sunday).")
        if not 1 <= self.day <= 31:
            raise ValueError("Day of the month must be 1 to 31.")

    def label(self) -> str:
        """The time alone: "6:00 AM"."""
        hour, minute = (int(part) for part in self.time.split(":"))
        return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"

    def summary(self) -> str:
        """"daily at 6:00 AM", "weekly on Monday at 6:00 AM", "monthly on the 1st at 6:00 AM"."""
        if self.frequency == "weekly":
            return f"weekly on {WEEKDAYS[self.weekday]} at {self.label()}"
        if self.frequency == "monthly":
            return f"monthly on the {ordinal(self.day)} at {self.label()}"
        return f"daily at {self.label()}"


def ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


@dataclass(frozen=True)
class SourceRun:
    source: SourceName
    status: Literal["synced", "skipped", "failed"]
    accounts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Problem:
    """Something only the person can fix, worded as what to do."""
    source: str
    title: str
    detail: str
    action: str
    href: str


@dataclass(frozen=True)
class Run:
    id: int
    trigger: Trigger
    status: Status
    started_at: str
    finished_at: str
    pid: int
    missed_days: int
    sources: list[SourceRun]
    people_before: int | None
    people_after: int | None
    index_rebuilt: bool
    problems: list[Problem]
    message: str
    error: str
    log: str


def now() -> datetime:
    return datetime.now().astimezone()


def _iso(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


def read_settings(root: Path) -> Settings:
    path = root / SETTINGS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Settings(bool(payload.get("enabled")), str(payload.get("time") or "09:00"),
                        str(payload.get("enabled_at") or ""), payload.get("frequency") or "daily",
                        int(payload.get("weekday") or 0), int(payload.get("day") or 1))
    except (OSError, ValueError, AttributeError, TypeError):
        return Settings()


def write_settings(root: Path, enabled: bool, time_of_day: str, frequency: Frequency | None = None,
                   weekday: int | None = None, day: int | None = None) -> Settings:
    """Save the schedule; a field left out keeps its saved value."""
    previous = read_settings(root)
    frequency = frequency or previous.frequency
    weekday = previous.weekday if weekday is None else weekday
    day = previous.day if day is None else day
    # Moving the schedule to another day starts it afresh: the new day's last slot (say the 1st,
    # when it's the 4th) is not a run that was missed.
    moved = (frequency, weekday, day) != (previous.frequency, previous.weekday, previous.day)
    keep = previous.enabled and enabled and previous.enabled_at and not moved
    enabled_at = previous.enabled_at if keep else _iso(now()) if enabled else ""
    settings = Settings(enabled, time_of_day, enabled_at, frequency, weekday, day)
    path = root / SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings)), encoding="utf-8")
    temporary.replace(path)
    return settings


def _connect(root: Path) -> sqlite3.Connection:
    path = root / DATABASE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def _decode(row: sqlite3.Row) -> Run:
    return Run(row["id"], row["trigger"], row["status"], row["started_at"], row["finished_at"], row["pid"],
               row["missed_days"], [SourceRun(**item) for item in json.loads(row["sources"])],
               row["people_before"], row["people_after"], bool(row["index_rebuilt"]),
               [Problem(**item) for item in json.loads(row["problems"])], row["message"], row["error"], row["log"])


def list_runs(root: Path, limit: int = MAX_RUNS) -> list[Run]:
    with closing(_connect(root)) as conn:
        rows = conn.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_decode(row) for row in rows]


def get_run(root: Path, run_id: int) -> Run | None:
    with closing(_connect(root)) as conn:
        row = conn.execute("SELECT * FROM refresh_runs WHERE id = ?", (run_id,)).fetchone()
    return _decode(row) if row else None


def _insert(root: Path, trigger: Trigger, missed_days: int) -> int:
    with closing(_connect(root)) as conn, conn:
        cursor = conn.execute("INSERT INTO refresh_runs (trigger, status, started_at, missed_days) VALUES (?, ?, ?, ?)",
                              (trigger, "running", _iso(now()), missed_days))
        return int(cursor.lastrowid or 0)


def _update(root: Path, run_id: int, **values: Any) -> None:
    encoded = {key: json.dumps([asdict(item) for item in value]) if key in ("sources", "problems") else value
               for key, value in values.items()}
    assignments = ", ".join(f"{key} = ?" for key in encoded)
    with closing(_connect(root)) as conn, conn:
        conn.execute(f"UPDATE refresh_runs SET {assignments} WHERE id = ?", (*encoded.values(), run_id))


# ---------------------------------------------------------------- schedule


def _slot_on(date: datetime, settings: Settings) -> datetime | None:
    """The scheduled moment on `date`'s day, or None when the schedule skips that day."""
    if settings.frequency == "weekly" and date.weekday() != settings.weekday:
        return None
    if settings.frequency == "monthly":
        last_day = calendar.monthrange(date.year, date.month)[1]
        if date.day != min(settings.day, last_day):
            return None
    hour, minute = (int(part) for part in settings.time.split(":"))
    return date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_slot(moment: datetime, settings: Settings) -> datetime | None:
    for ahead in range(63):
        slot = _slot_on(moment + timedelta(days=ahead), settings)
        if slot is not None and slot > moment:
            return slot
    return None


def latest_slot(moment: datetime, settings: Settings) -> datetime | None:
    """The last slot at or before `moment`."""
    for back in range(32):
        slot = _slot_on(moment - timedelta(days=back), settings)
        if slot is not None and slot <= moment:
            return slot
    return None


def due(moment: datetime, settings: Settings, last_started: datetime | None) -> bool:
    """Whether a scheduled run should start at `moment`: the last slot passed with nothing started
    since (nor since the refresh was turned on or moved), and the next slot is not so close that
    it should simply wait for that one."""
    if not settings.enabled:
        return False
    slot = latest_slot(moment, settings)
    if slot is None:
        return False
    since = [stamp for stamp in (last_started, _parse(settings.enabled_at)) if stamp is not None]
    if since and max(since) >= slot:
        return False
    upcoming = _next_slot(moment, settings)
    return upcoming is None or upcoming - moment >= CATCH_UP_WINDOW


def next_run_at(moment: datetime, settings: Settings, last_started: datetime | None) -> datetime | None:
    if not settings.enabled:
        return None
    if due(moment, settings, last_started):
        return moment
    return _next_slot(moment, settings)


def missed_days(moment: datetime, last_started: datetime | None) -> int:
    """Daily runs skipped since the last one (the app was closed), folded into this run."""
    if last_started is None:
        return 0
    return max(0, (moment.date() - last_started.astimezone().date()).days - 1)


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp).astimezone() if stamp else None
    except ValueError:
        return None


def last_started(root: Path) -> datetime | None:
    runs = list_runs(root, 1)
    return _parse(runs[0].started_at) if runs else None


def reconcile(root: Path) -> None:
    """Close a run whose process is gone (the app quit or the Mac slept through it)."""
    for run in list_runs(root, 5):
        if run.status == "running" and not alive(run.pid):
            started = _parse(run.started_at)
            # A run that was just inserted gets a moment to record its process.
            if run.pid == 0 and started and now() - started < timedelta(minutes=2):
                continue
            _update(root, run.id, status="failed", finished_at=_iso(now()),
                    error="The refresh stopped before it finished (the app closed or restarted).",
                    problems=[Problem("refresh", "The last refresh didn't finish",
                                      "It stopped when the app closed. Refresh again to pick up where it left off.",
                                      "Refresh now", "/tasks")])


def running(root: Path) -> Run | None:
    reconcile(root)
    latest = list_runs(root, 1)
    return latest[0] if latest and latest[0].status == "running" else None


def setup_busy(root: Path) -> bool:
    """Whether setup or an Enrich run holds the onboard lock right now."""
    try:
        import fcntl
        with (root / ONBOARD_LOCK).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def project_python(root: Path) -> str:
    """The project's environment (the server itself may run on the bootstrap Python)."""
    venv = root / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def start(root: Path, trigger: Trigger) -> Run:
    """Start a run in a detached child; the one already running when there is one."""
    current = running(root)
    if current:
        return current
    if setup_busy(root):
        raise RuntimeError("An import is already running. The refresh starts once it finishes.")
    moment = now()
    daily_schedule = trigger == "daily" and read_settings(root).frequency == "daily"
    run_id = _insert(root, trigger, missed_days(moment, last_started(root)) if daily_schedule else 0)
    log = root / DIRECTORY / f"run-{run_id}.log"
    with log.open("ab") as out:
        child = subprocess.Popen([project_python(root), "-m", "packs.ingestion.primitives.refresh.daily", "run",
                                  "--run-id", str(run_id), "--root", str(root)],
                                 cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    _update(root, run_id, pid=child.pid, log=str(log))
    threading.Thread(target=child.wait, daemon=True).start()  # reap it
    return get_run(root, run_id)  # type: ignore[return-value]


def scheduler(root: Path, *, tick: float = TICK_SECONDS) -> None:
    """The server's daily clock: start a run when one is due. Never raises."""
    while True:
        try:
            settings = read_settings(root)
            if settings.enabled and not running(root) and due(now(), settings, last_started(root)) \
                    and not setup_busy(root):
                start(root, "daily")
        except Exception:  # noqa: BLE001 - the clock keeps ticking; the next tick tries again
            traceback.print_exc()
        time.sleep(tick)


# ---------------------------------------------------------------- one run (the child)


def _count_people(path: Path) -> int | None:
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        return max(0, sum(1 for _ in csv.reader(handle)) - 1)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def connected_sources(root: Path) -> list[SourceName]:
    """The sources set up on this Mac: each has data from an earlier import."""
    from packs.ingestion.primitives.accounts.accounts import MSGVAULT_HOME
    from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_STORE
    from packs.ingestion.primitives.setup.automations.accounts import stored_mailboxes

    found: list[SourceName] = []
    if (root / CONNECTIONS_CSV).is_file():
        found.append("linkedin")
    if stored_mailboxes(MSGVAULT_HOME):
        found.append("gmail")
    if (root / IMESSAGE_CONTACTS).is_file():
        found.append("imessage")
    store = DEFAULT_STORE if DEFAULT_STORE.is_absolute() else root / DEFAULT_STORE
    if (store / "wacli.db").is_file():
        found.append("whatsapp")
    return found


def renew_gmail() -> None:
    """Sign each Gmail mailbox that needs it (expired, or its token gone) back in once with the saved
    Google session, approving the consent automatically; one Google still wants the person for
    stays signed out and is reported."""
    from packs.ingestion.primitives.accounts.accounts import MSGVAULT_HOME
    from packs.ingestion.primitives.setup.automations import accounts
    from packs.ingestion.primitives.setup.automations.accounts import stored_mailboxes

    emails = list(stored_mailboxes(MSGVAULT_HOME))
    if not emails:
        return
    check = accounts.check_accounts_payload(MSGVAULT_HOME, emails)
    for email in check["accounts_to_authorize"]:
        accounts.add_account(MSGVAULT_HOME, email, "", headless=False, force=email in check["expired_accounts"])


def problems_after(connected: list[SourceName], manifest: dict, *, gmail: dict[str, str],
                   linkedin: bool, imessage: bool, whatsapp: bool) -> list[Problem]:
    """What the person must do, from each connected source's sign-in after the run and the run's record."""
    found: list[Problem] = []
    expired = sorted(email for email, state in gmail.items() if state in ("missing_token", "reauthorization_required"))
    if "gmail" in connected and expired:
        who = ", ".join(expired)
        found.append(Problem("gmail", f"Gmail sign-in expired for {who}",
                             "Reconnect to sync the mail since.",
                             "Reconnect Gmail", REFRESH_HREF))
    if "imessage" in connected and not imessage:
        found.append(Problem("imessage", "Messages can't be read",
                             "Turn Full Disk Access on for Powerpacks in System Settings, then refresh again.",
                             "Fix Messages", ACCOUNTS_HREF + "imessage"))
    if "whatsapp" in connected and not whatsapp:
        found.append(Problem("whatsapp", "WhatsApp unlinked this Mac",
                             "Scan the QR code again on Accounts to keep WhatsApp in sync.",
                             "Link WhatsApp", ACCOUNTS_HREF + "whatsapp"))
    if "linkedin" in connected and not linkedin:
        found.append(Problem("linkedin", "LinkedIn signed out",
                             "Sign in to LinkedIn again on Accounts so new connections are read.",
                             "Sign in to LinkedIn", ACCOUNTS_HREF + "linkedin"))
    if manifest.get("status") in ("failed", "waiting"):
        stopped = manifest["status"] == "failed"
        message = str(manifest.get("message") or "")
        # The install record's generic stop line speaks of installing; a refresh says it in its own words.
        if not message or message.startswith("Installation stopped"):
            message = STOPPED_DETAIL if stopped else "Open Scheduled tasks to see what it needs."
        found.append(Problem("refresh", "The refresh stopped" if stopped else "The refresh needs you",
                             message, "Open Scheduled tasks", REFRESH_HREF))
    return found


def sources_after(connected: list[SourceName], manifest: dict, mailboxes: tuple[str, ...]) -> list[SourceRun]:
    steps = manifest.get("steps") or {}
    result: list[SourceRun] = []
    for source in connected:
        state = (steps.get(IMPORT_STEP[source]) or {}).get("status", "")
        status = "synced" if state == "completed" else "failed" if state == "failed" else "skipped"
        result.append(SourceRun(source, status, list(mailboxes) if source == "gmail" and status == "synced" else []))
    return result


def powerset_signed_in(root: Path) -> bool:
    """Whether the Powerset sign-in is still good (refreshed when it expired). Setup's account step
    would otherwise wait for a browser sign-in nobody is there to finish."""
    from packs.powerset.primitives.auth import auth
    from packs.shared.web.account import config, credentials_path

    settings = config(root)
    try:
        auth._credentials_with_fresh_token(credentials_path(root), settings.get("POWERPACKS_AUTH0_DOMAIN", ""),
                                           settings.get("POWERPACKS_AUTH0_CLIENT_ID", ""))
    except SystemExit:
        return False
    return True


def run_once(root: Path, run_id: int) -> Run:
    """The child: check the Powerset sign-in, renew Gmail's, run onboard --refresh, then record what happened."""
    from packs.ingestion.primitives.accounts.accounts import gmail_signins, signed_in
    from packs.powerset.primitives.install.status import InstallStatus

    _update(root, run_id, pid=os.getpid())
    people_before = _count_people(root / PEOPLE_CSV)
    fingerprint = _read(root / INDEX_FINGERPRINT)
    connected = connected_sources(root)
    _update(root, run_id, people_before=people_before)
    if not connected:
        _update(root, run_id, status="failed", finished_at=_iso(now()), message="No sources are connected yet.",
                problems=[Problem("refresh", "Nothing to refresh yet",
                                  "Connect LinkedIn, Gmail, Messages or WhatsApp on Accounts.",
                                  "Open Accounts", "/accounts")])
        return get_run(root, run_id)  # type: ignore[return-value]
    if not powerset_signed_in(root):
        _update(root, run_id, status="needs_you", finished_at=_iso(now()), message="Not signed in to Powerset.",
                problems=[Problem("powerset", "Signed out of Powerset",
                                  "Sign in from the account menu at the bottom of the sidebar, then refresh again.",
                                  "Open Accounts", "/accounts")])
        return get_run(root, run_id)  # type: ignore[return-value]
    error = ""
    try:
        if "gmail" in connected:
            renew_gmail()
        ready = signed_in(sources=tuple(connected))
        command = [project_python(root), "-m", "packs.powerset.primitives.install.onboard", "--root", str(root),
                   "--refresh", *(arg for source in connected for arg in ("--source", source))]
        completed = subprocess.run(command, cwd=root, env={**os.environ, "POWERPACKS_DESKTOP": "1"},
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
        print(completed.stdout, completed.stderr, sep="\n", flush=True)
        if "already running" in completed.stdout:
            error = "An import was already running, so this refresh didn't start."
        elif completed.returncode < 0:
            error = "The import was stopped before it finished (another import started)."
    except Exception as caught:  # noqa: BLE001 - recorded on the run
        traceback.print_exc()
        error = f"{type(caught).__name__}: {caught}"
        ready = None
    manifest = InstallStatus(root).read() if not error else {"status": "failed", "message": error, "steps": {}}
    signins = gmail_signins()
    after = signed_in(signins, sources=tuple(connected))
    problems = problems_after(connected, manifest, gmail=signins if "gmail" in connected else {},
                              linkedin=after.linkedin, imessage=after.imessage, whatsapp=after.whatsapp)
    sources = sources_after(connected, manifest, ready.healthy_mailboxes if ready else ())
    failed = manifest.get("status") == "failed" or bool(error)
    status: Status = "failed" if failed else "needs_you" if problems else "ok"
    _update(root, run_id, status=status, finished_at=_iso(now()), sources=sources, problems=problems,
            people_after=_count_people(root / PEOPLE_CSV),
            index_rebuilt=int(_read(root / INDEX_FINGERPRINT) != fingerprint and bool(_read(root / INDEX_FINGERPRINT))),
            message=str(manifest.get("message") or ""), error=error)
    return get_run(root, run_id)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("run", "start", "status"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id", type=int, default=0)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    os.chdir(root)
    if args.command == "start":
        print(json.dumps(asdict(start(root, "manual")), indent=2))
        return 0
    if args.command == "status":
        print(json.dumps([asdict(run) for run in list_runs(root, 5)], indent=2))
        return 0
    run_id = args.run_id or _insert(root, "manual", 0)
    try:
        run = run_once(root, run_id)
    except Exception as caught:  # noqa: BLE001 - the row must not stay running
        traceback.print_exc()
        _update(root, run_id, status="failed", finished_at=_iso(now()), error=f"{type(caught).__name__}: {caught}",
                problems=[Problem("refresh", "The refresh stopped", f"{caught}", "Open Scheduled tasks", REFRESH_HREF)])
        return 1
    return 1 if run.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
