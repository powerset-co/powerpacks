# Repair a Powerpacks crash

Created: 2026-10-14

Change log:
- 2026-10-14: created. The desktop app runs it with `codex exec` when Powerpacks breaks
  (desktop/src-tauri/src/crash.rs); the app files your last message with Powerset.

Powerpacks broke on this Mac, and the app handed you what failed. This folder is the installed
Powerpacks; the app restarts its page server after you finish. Your last message is the report.

## Rules

- Never change, move or delete anything in `.powerpacks/` (the user's data) or `.env`.
- Do not start, stop or kill Powerpacks's page server or the app.
- Find the cause in the error, the details, `.powerpacks/install/server.log` (the page server's
  log) and the code.
- Repair the local setup when it is the cause: for missing or broken packages run
  `uv sync --frozen --no-dev`.
- Change code only when Powerpacks cannot start or a core page cannot load: the smallest change
  that fixes it. Copy each file to `$TMPDIR` before you edit it, and verify the fix, for the page
  server with `.venv/bin/python -c "import packs.shared.web.server"` and
  `.venv/bin/python -m packs.shared.web.server --help`. The next Powerpacks update replaces the
  code, so your report is how the fix reaches everyone.
- Anything else, such as a page that throws: find the cause and report it; change nothing.
  `web/dist` is built JavaScript; never edit it.

## Report

Your last message, plain text, under 3,000 characters:

```text
What broke: <one line>
Cause: <file:line and why>
Changed: <nothing | what, with `diff -u <copy> <file>`>
Verified: <the command and what it printed>
```

Leave out credentials, emails, names of people or sets, queries, message text and home-directory
paths (write `~`).
