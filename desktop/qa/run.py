"""Run QA scenarios in the real app window.

    python3 desktop/qa/run.py --root ~/qa/powerpacks [people-menus search-set-picker ...]
    uv run --with playwright python desktop/qa/run.py --browser --root ~/qa/powerpacks [...]

Opens the QA build (`desktop/qa/build.sh`) on the Powerpacks folder `--root`: its web page,
its `.powerpacks` data. Scenarios write (keep, skip, rename, delete), so the root holds a copy
made for QA, never your own data. The installed Powerpacks uses the same local port, so it is
quit for the run and opened again after. With --browser it runs headless in WebKit on a page
server of its own on a free port instead, leaving the installed app alone (browser.py). Pictures
land in `--out`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from app import App, Failed
from scenarios import NATIVE_ONLY, SCENARIOS

HERE = Path(__file__).resolve().parent
QA_APP = HERE.parent / "src-tauri/target/release/bundle/macos/Powerpacks QA.app"
PORT = 8813


def installed_running() -> bool:
    return subprocess.run(["pgrep", "-f", "/Applications/Powerpacks.app/"], capture_output=True).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", help=f"default: all ({', '.join(SCENARIOS)})")
    parser.add_argument("--root", required=True, type=Path, help="Powerpacks folder with QA data")
    parser.add_argument("--app", type=Path, default=QA_APP)
    parser.add_argument("--out", type=Path, default=Path("qa-out"))
    parser.add_argument("--browser", action="store_true", help="headless, on a page server of its own")
    parser.add_argument("--engine", default="webkit", choices=("webkit", "chromium"), help="with --browser")
    args = parser.parse_args()
    names = args.scenarios or [name for name in SCENARIOS if not (args.browser and name in NATIVE_ONLY)]
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        parser.error(f"no scenario {unknown}; have {list(SCENARIOS)}")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.browser:
        return run(names, *browser(args))

    reopen = installed_running()
    if reopen:
        subprocess.run(["osascript", "-e", 'quit app "Powerpacks"'], check=True)
        while installed_running():
            time.sleep(0.5)
    env = {**os.environ, "POWERPACKS_QA_PORT": str(PORT), "POWERPACKS_REPO_ROOT": str(args.root.resolve())}
    log = (args.out / "app.log").open("w")
    process = subprocess.Popen([str(args.app / "Contents/MacOS/powerpacks-desktop")], env=env, stdout=log, stderr=log)
    try:
        failures = run(names, App(PORT, args.out), process)
    finally:
        if reopen:
            subprocess.run(["open", "-a", "Powerpacks"])
    return failures


def browser(args: argparse.Namespace):
    """A page server on a free port for --root, from the baseline store when there is one."""
    root = args.root.resolve()
    store = root / ".powerpacks/deep-context/deep-context-v2.sqlite"
    baseline = root / ".powerpacks/deep-context-baseline.sqlite"
    if baseline.exists():
        shutil.copyfile(baseline, store)
        for extra in ("-wal", "-shm"):
            Path(f"{store}{extra}").unlink(missing_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = (args.out / "server.log").open("w")
    server = subprocess.Popen(["uv", "run", "--project", str(root), "python", "-m", "packs.shared.web.server",
                               "serve", "--port", str(port)], cwd=root, stdout=log, stderr=log,
                              # Signed out: never the sign-in in ~/.powerpacks, so background work can't reach Powerset.
                              env=os.environ | {"POWERPACKS_CREDENTIALS_PATH": str(root / ".powerpacks/qa-signed-out.json")})
    deadline = time.time() + 600  # the first run makes the copy's .venv
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except OSError:
            if server.poll() is not None:
                raise SystemExit(f"the page server stopped; see {args.out / 'server.log'}")
            time.sleep(0.5)
    else:
        raise SystemExit("the page server did not answer in 10 minutes")
    from browser import Browser

    return Browser(f"http://127.0.0.1:{port}", args.out, args.engine), server


def run(names: list[str], app: App, process: subprocess.Popen) -> int:
    failures = 0
    try:
        app.ready()
        for name in names:
            started = time.time()
            try:
                SCENARIOS[name](app)
                errors = app.errors()
                if errors:
                    raise Failed(f"console errors: {errors[:3]}")
                print(f"PASS  {name}  {time.time() - started:.1f}s")
            except Failed as failure:
                failures += 1
                print(f"FAIL  {name}: {failure}  (picture: {app.picture(name)})")
            except Exception:
                failures += 1
                print(f"ERROR {name}\n{traceback.format_exc()}")
            # The next scenario starts from a fresh page.
            app.reload()
    finally:
        if hasattr(app, "close"):
            app.close()
        process.terminate()
        process.wait(timeout=30)
    print(f"{len(names) - failures}/{len(names)} passed; pictures and logs in {app.pictures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
