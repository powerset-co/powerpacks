---
name: install-powerpacks
description: Set up Powerpacks from one pasted URL. Install locally, open live progress, connect the user's Powerset account, and verify their network in the same session. Use for "Download and install the Powerpacks skill from https://powerset.dev/powerpacks", "install Powerpacks", or first-time Powerpacks setup.
license: MIT
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.3.0
  summary: Install, connect your account, and check your network from one sentence
  download-url: https://powerset.dev/powerpacks
  tags:
    - powerpacks
    - install
    - network-search
---

# Set up Powerpacks

<!-- Changelog: 2026-10-03 — continue with default source imports on the same
page; the original command owns login, permission waits, and import continuation. -->

The user pastes:

> Download and install the Powerpacks skill from https://powerset.dev/powerpacks

Carry that request through to a working account and a verified network. Do not
stop after saving this file, ask them to type another skill, or hand them an
installation checklist. The scripts do the work; you watch progress,
handle routine recovery, and involve the user only for missing identity or actions
requiring them. Open with: "I’ll set this up here. Feel free to ask questions or
tell me what you want as it runs."

## Run one command

This needs a shell on the user's Mac. If no local shell is available, explain:
"Open Codex or Claude Code on your Mac and paste the same instruction there."
Do not claim a cloud installation changed their computer. Respect existing
permissions; a denied command is not permission to bypass the restriction.

Save this skill in the current agent's personal skills folder if needed
(Codex: `~/.agents/skills/install-powerpacks/SKILL.md`; Claude Code:
`~/.claude/skills/install-powerpacks/SKILL.md`). Then continue immediately:

```bash
curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/main/bin/bootstrap | bash -s -- --powerset --no-tools
```

Run asynchronously so you can show progress while it works. In Codex, use a
short shell `yield_time_ms`; in Claude Code, use background Bash. Keep ownership
of that process until its verified outcome. Browser consent, QR scans, and
Messages permission changes resume the same command automatically; the user
should not need to say "done" or ask you to poll. Watch command output and saved
state using bounded waits. A background process is not a completed workflow.

If the user requests a worker and advisor, give the worker command execution
and keep the original chat responsible for steering and recovery. Relay chat
choices to the worker; never launch a competing installer. Do not leave an
exited workflow waiting on a future user message to resume it.

No project directory or manual clone is needed. Bootstrap downloads into
`~/powerpacks` or reuses the existing installation. When it prints
`powerpacks: repo: <path>`, read that checkout's `AGENTS.md` for usage guidance
and `.codex/AGENTS.md` if present for remembered user context. Run subsequent
commands there; don't assume the chat is inside the checkout or write global
agent instructions into the user's unrelated projects.

This installs Powerpacks, preserves existing configuration, connects the
Powerset account, checks access to search, and checks the selected network.
Already-valid login is skipped. The normal pasted instruction authorizes this
setup, including local credential configuration and the default local Gmail,
iMessage, and WhatsApp imports below. Browser sign-in still belongs to the user.
It does not authorize uploading contacts or paid research.

- Explicit "install only", "don't log in", or custom/local-only setup: omit
  `--powerset`. Follow that narrower request; don't report account readiness.
- Keep `--no-tools` for initial setup. Source imports prepare their own tools
  in the same command. Report hosted search as usable only after verification.
- Reruns update the existing checkout within its selected release channel and
  preserve configuration and network data. Fresh installs follow the published
  release, not the current PR or `main`. When explicitly testing a PR, run its
  checkout's `bin/bootstrap` instead of the public launcher and verify its commit.
  Do not switch channels or edit application code to repair setup.

## Show the live page

Watch for `STATUS PAGE: <url>` and open that exact URL immediately, before
waiting for the command to finish. Reuse the tab on retries.

- If the host exposes a browser-pane tool, use it to open the page beside chat.
  Use `open_in_codex` only if that tool is actually available.
- Otherwise open the URL in the Mac's default browser with `open "<url>"`.
  Codex CLI can use this browser tab; it cannot render a custom animation in chat.
- A printed URL or queued open request is not proof of a rendered page. Check
  the returned browser state when available, or the page's HTTP response.

Keep the progress pane on this URL. Let the login primitive open sign-in in the
system's default browser; do not pass `--no-browser` or open sign-in or callback
URLs in the in-app browser. If automatic launch fails, use `open "<sign-in URL>"`
on the printed URL. The user completes sign-in there while progress stays here.

The animation and step labels come from
`.powerpacks/install/manifest.json` and the source primitives' existing artifacts.
Installation, source imports, and processing share the same page.
Never fabricate progress, write the manifest by hand, or treat the animation as
proof of success. The page stays open after the command ends.

Keep chat short: an opening sentence, necessary user actions, and the final
result. Do not repeat the page's checklist after every step or paste logs into
chat. If the page is unavailable, give one short line when the phase changes.

## Continue or recover

Keep watching the original command until it exits. Read its last status line
and the saved progress; do not start a second installer while it is running.

| Script result | Agent action |
| --- | --- |
| `DONE:` | Verify the saved result. Bootstrap already ran the selected imports; do not start a second onboarding command. Respect an explicit install-only request. |
| `NEEDS YOU:` | Read the saved action. Live sign-in, QR, and permission waits continue automatically. If the command exited for missing app configuration or identity, handle its action yourself and resume the saved command. At the processing handoff, run the free checks and obtain approval before paid work. |
| `STOP:` | Explain where to run the instruction. Do not continue in the wrong environment. |
| `FAILED:` | Read the saved error, fix the cause within the authorized setup, and rerun. Don't ask "should I try to fix it?" |

Use the same command and options on retries. Completed files and cached
credentials are reused, and live checks are repeated. Do not delete `.powerpacks`
or overwrite existing credentials to start over.

Specific recovery:

- **Download or dependency failure:** inspect `.powerpacks/install/install.log`.
  Retry a transient connection failure; for an SSH authentication failure on
  the public repository, use its HTTPS URL. Repair installation/configuration,
  not application code. Preserve local work and data.
- **Login missing or expired:** let the script refresh credentials or open the
  existing sign-in flow. A network timeout is not proof the account is wrong.
- **Search connection fails after login:** let the script verify direct service
  access and refresh the agent connection. Don't force a session restart merely
  because newly registered tools aren't visible yet. Never claim a connection
  works until its check succeeds.
- **Hosted search is unprovisioned, empty, or unindexed:** the command continues local imports and retains the actual search check in Details. Do not ask the user to repair hosting before connecting sources or claim search is usable.
- **Personal network has 0 people:** name the actual signed-in email and continue
  the default contact imports. If the user requests an account switch, use the
  installed `auth.py login --force-account` primitive, then rerun onboarding.
  Don't guess another identity or silently switch to a shared network.
- **Personal network has 1–9 people:** continue when search works; mention the
  count and that another account or network may contain more people. This is a
  suggestion, not a gate. If all accessible networks are empty, say so.
- **Progress page fails:** check the printed URL, `/healthz`, `/api/install`, and
  `.powerpacks/install/server.log`. Reuse or restart the same server via the
  printed retry command; don't open duplicate tabs. A browser-pane error with
  healthy HTTP responses is a display problem, not an installation failure.
- **The same failure returns after a targeted repair:** give the specific
  remaining problem and action. Do not loop indefinitely or claim success.

Internal IDs, "sets", provider keys, MCP, and database names belong in
troubleshooting details. Say "your personal network", the network's actual
name, and the signed-in email in normal conversation.

## Continue on the same page

Bootstrap connects the account and runs Gmail, iMessage, and WhatsApp in one
command. Inform the user once: "I’ll import Gmail from the past year, iMessage,
and WhatsApp. Tell me here if you want a different account, history, or to skip
one." Apply choices already given; do not ask again or wait for default-source
confirmation. Pass existing `--source`, `--gmail-email`, `--sync-after`, and
`--wacli-store` options to bootstrap when supplied. A named Gmail import account
is independent of the Powerset account; do not force a Powerset account switch
because the emails differ. LinkedIn is included only when requested.

For chat choices, run the same script with one `--source gmail|imessage|whatsapp|linkedin`
per selected source, or `--source skip`. Gmail accepts repeatable
`--gmail-email <address>` and `--sync-after <YYYY-MM-DD>` overrides. The script
preserves source, account, history, and store choices from the manifest's
`retry_command`; otherwise Gmail uses the verified signed-in email or the only
configured msgvault account and defaults to one year. If neither identifies one
account, ask only which Gmail account to use. Never select an arbitrary account
from several. The script prepares the sources' tools and reuses existing imports.
Use `--refresh` only when the user asks to sync again.

Gmail authorization and WhatsApp linking stay inside the original command;
iMessage access checks resume when permission becomes readable. OAuth app
creation, absent Messages data, and missing identity can still return a saved
action for the agent. Keep handling those actions without a new user instruction.

Watch the saved manifest and the original process. If a panel action already
started it, follow that process rather than launching a duplicate. Read `action`
while a live command waits, and the saved error when it fails:

- Run the relevant `action.command` from the checkout yourself within the chosen
  source's scope. Handle dependency repairs, OAuth app preparation, and retries;
  don't hand commands or skill names to the user.
- Browser sign-in stays outside the progress pane. Let the login primitive open
  the system browser; if needed, open its URL there. Only the user completes
  account sign-in, consent screens, or a WhatsApp QR scan. The page shows the QR.
- For Messages permission, POST the same server's `/api/install/permissions`
  endpoint. It opens Full Disk Access and highlights the detected app in Finder
  when available. Tell the user which app was actually identified; don't assume
  Terminal, Ghostty, or Codex. The user grants access, then you retry.
- For LinkedIn, open the export URL in the system browser and wait for the CSV
  in chat. Preserve any existing input before replacing it. Receiving the CSV
  does not authorize paid processing or an upload.

If the command exited for an agent action, rerun the manifest's `retry_command` with the same exact source,
account, history, and store choices. Existing files and sessions are reused;
never log out, clear stores, or delete data to rehearse a fresh install. Repair a
failed step and retry it; if the same failure persists, explain the remaining
cause and the single action needed. Leave technical details on the page.

"Skip Gmail for now" explicitly authorizes skipping that source; the same applies
to iMessage, WhatsApp, and LinkedIn. Stop only the known, owned work for that source
if it is still running, including its auth or sync worker, and cancel any queued
continuation that would resume it. Check the original command and saved process
before stopping it; never stop unrelated processes. Onboarding leaves active work
untouched, so wait for the owned worker to exit before retrying.

Append `--skip-source gmail|imessage|whatsapp|linkedin` to the exact original
`retry_command` and run it yourself. Keep every original `--source`, account,
history, store, refresh, and prior skip option. The source's stages stay on the
page as Skipped, and the script continues with the next selected source. When
all selected sources are skipped, it finishes at Ready without starting
processing. Existing imports, data, and login are preserved. Repeating the command
keeps the source skipped; if the user asks to resume it, remove only its
`--skip-source` option and rerun. Never ask the user to run commands.

When sources are ready, the page points to processing. Follow the installed
`deep-context` skill when the user requested it, retaining this server and tab.
Its normal commands update the same installation manifest. The page groups
each source into one Syncing row, then shows Discover, Enrich, optional Review,
and Build Index. Collection, synthesis, and duplicate resolution are Discover;
research and identity matching are Enrich. SQLite decides whether Review is
needed and when it is complete. Retain the status pane; open the existing
review in the default browser when needed, then use `review-status --wait` to
continue when the real queue clears. Realization prepares indexing; the normal Modal
`index-people` command mirrors cloud phases into the same page and advances to
validation after download. The search validator alone marks Ready on success.
Do not replace the status tab or claim the chain finished at the imports handoff.
Read the free estimate and obtain the required permission before any paid
processing or upload. Never emulate completion as a real result; label a
requested demonstration as simulated.

Failures do not reset completed work. The page polls again after a connection
failure; restart its server only if health fails or server code changed while
idle. Enrichment retries a crashed step once after five seconds, then records
failure. Inspect that failure and rerun the exact command using saved artifacts.
If Modal disconnected after dispatch, inspect the existing run and use its
`download --wait` command rather than dispatching another paid job blindly.

## Finish with verified readiness

Installation, hosted search, imported sources, and a built local index are
separate results. Report only the verified ones in a short line:

> Powerpacks is ready. Signed in as <email>; <network> has <N> people.

A local-only installation needs no account. A missing or empty hosted network
isn't proof of usable search. Source imports aren't proof of a built index.
Don't call all contacts searchable merely because a network list reports them.

If the original request included a search, read the installed `search/SKILL.md`
and do it now, preserving the requested network. Read newly installed skills
directly in this session; no restart or new user command is needed.

Existing hosted networks can work immediately. First downloads and human login
take longer; source syncing and index building can take much longer. Show actual
progress and keep the page open, without promising a completion time.
