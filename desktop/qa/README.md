# desktop/qa 🧪

Created: 2026-10-29

Change log:
- 2026-10-29: First version: the QA hook, the runner, People and set-picker scenarios.
- 2026-11-06: `--browser`: the same scenarios headless in WebKit, on any machine.

Click through the real app window (WebKit, the Tauri shell, the local page server) from the
terminal, so a UI change is checked where people use it instead of by hand.

| File | Role |
| --- | --- |
| `build.sh` | Builds `Powerpacks QA.app`: the debug-menu build with the QA hook, its own app id |
| `remote.sh` | Runs the scenarios on casper-mini and brings the pictures back |
| `run.py` | Opens the QA app on a Powerpacks folder, runs scenarios, prints PASS/FAIL with timings |
| `browser.py` | The headless driver for `--browser`: Playwright's WebKit on a page server of its own |
| `app.py` | The driver: find, click, type, wait within a budget, WebKit's picture |
| `scenarios.py` | One function per flow; each check names the bug it guards |
| `../src-tauri/src/qa.rs` | The hook: on only in a debug-menu build started with `POWERPACKS_QA_PORT` |

How a check works:

- **Clicks and keys are native.** The hook sends macOS mouse and key events to the window, so
  WebKit handles them as a person's (`isTrusted`). Before clicking, the driver checks the element
  is what sits on top at its centre; a menu under a panel fails as "covered by …".
- **Every wait has a budget** (`PAGE`, `CLICK`, `WRITE` in `scenarios.py`). A miss prints the
  page's slowest requests since the action.
- **Console errors fail the scenario.**
- **Links that leave the app are recorded**, not opened, so a scenario can check one went out.
- **Pictures come from WebKit**, saved for every failure; no Screen Recording permission needed.

## Run

Runs go to the QA Mac (casper-mini), never the Mac you work on: the QA app shares the local page's
port with an installed Powerpacks, takes the front window, and needs to stay the key window
(WebKit pauses animations in a background window and the first click only activates it).

```bash
desktop/qa/build.sh
desktop/qa/remote.sh ~/workspace/pp-qa-app people-menus people-rename
```

`build.sh` runs where Rust is; rebuild only when `desktop/src-tauri` changes. `remote.sh` copies
the app and the `--root` folder (the page and its `.powerpacks/` data) to
`casper-mini:/Volumes/ssd1/qa/desktop-qa`, starts every run from
`.powerpacks/deep-context-baseline.sqlite`, and brings the pictures back to `qa-out/`. The first
launch on a fresh box downloads the browser and makes a `.venv` (a few minutes).

The root needs `.powerpacks/desktop/skip-setup` (launch opens the app, not setup) and a People
store. Scenarios keep, skip, rename and delete, so it is always a copy. Today's is a copy of
a large real store (about 6k people, realized), the size where the People page lagged.

The chat's set picker scenario needs ChatGPT signed in on the QA box's app (Accounts → Connect
ChatGPT, once).

## Run headless

```bash
uv run --with playwright playwright install webkit   # once
uv run --with playwright python desktop/qa/run.py --browser --root ~/workspace/pp-qa-app [scenario ...]
```

Playwright's WebKit (the engine under the app's window, not the window itself) opens the page
from a page server it starts on a free port for `--root`, so it runs on the Mac you work on, in
a cloud thread or in CI without touching the installed Powerpacks or the screen. Each run starts
from `.powerpacks/deep-context-baseline.sqlite` when the root has one. A plain browser has no
desktop bridge, so chat (`search-set-picker`), sign-ins and links the app hands to the browser
are checked only in the native run; a link that leaves the page is recorded the same way.

The page server runs signed out: it reads the Powerset sign-in from a missing file in the root,
never `~/.powerpacks/credentials.json`, so its background work can't reach Powerset as you. The
scenarios need nothing from `.env`. Failure pictures and messages show the store's people, so in a
cloud thread or CI use a root with synthetic people.

## Add a scenario

Write a function in `scenarios.py` that drives the flow the way a person would (`app.click`,
`app.type`, `app.key`) and checks the result with `app.wait_for(…, budget, "what should
happen")`; add it to `SCENARIOS`.
