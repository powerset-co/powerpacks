"""Run QA scenarios in the real app window.

    python3 desktop/qa/run.py --root ~/qa/powerpacks [people-menus search-set-picker ...]

Opens the QA build (`desktop/qa/build.sh`) on the Powerpacks folder `--root`: its web page,
its `.powerpacks` data. Scenarios write (keep, skip, rename, delete), so the root holds a copy
made for QA, never your own data. The installed Powerpacks uses the same local port, so it is
quit for the run and opened again after. Pictures land in `--out`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from app import App, Failed
from scenarios import SCENARIOS

HERE = Path(__file__).resolve().parent
QA_APP = HERE.parent / "src-tauri/target/release/bundle/macos/Powerpacks QA.app"
PORT = 8799


def installed_running() -> bool:
    return subprocess.run(["pgrep", "-f", "/Applications/Powerpacks.app/"], capture_output=True).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", help=f"default: all ({', '.join(SCENARIOS)})")
    parser.add_argument("--root", required=True, type=Path, help="Powerpacks folder with QA data")
    parser.add_argument("--app", type=Path, default=QA_APP)
    parser.add_argument("--out", type=Path, default=Path("qa-out"))
    args = parser.parse_args()
    names = args.scenarios or list(SCENARIOS)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        parser.error(f"no scenario {unknown}; have {list(SCENARIOS)}")
    args.out.mkdir(parents=True, exist_ok=True)

    reopen = installed_running()
    if reopen:
        subprocess.run(["osascript", "-e", 'quit app "Powerpacks"'], check=True)
        while installed_running():
            time.sleep(0.5)
    env = {**os.environ, "POWERPACKS_QA_PORT": str(PORT), "POWERPACKS_REPO_ROOT": str(args.root.resolve())}
    log = (args.out / "app.log").open("w")
    process = subprocess.Popen([str(args.app / "Contents/MacOS/powerpacks-desktop")], env=env, stdout=log, stderr=log)
    failures = 0
    try:
        app = App(PORT, args.out)
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
        process.terminate()
        process.wait(timeout=30)
        if reopen:
            subprocess.run(["open", "-a", "Powerpacks"])
    print(f"{len(names) - failures}/{len(names)} passed; pictures and app.log in {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
