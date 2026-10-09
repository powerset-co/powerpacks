---
name: install-powerpacks
description: Set up Powerpacks from one pasted URL through a resumable account, import, processing, and search-index workflow with live progress and automatic cost checks. Use for "Download and install the Powerpacks skill from https://powerset.dev/powerpacks", "install Powerpacks", or first-time Powerpacks setup; installing it means running the setup now, not only saving the file.
license: MIT
allowed-tools: Bash(curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/stable/bin/bootstrap *)
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.7.1
  summary: Install and build your searchable network from one sentence
  download-url: https://powerset.dev/powerpacks
  tags:
    - powerpacks
    - install
    - network-search
---

# Set up Powerpacks

<!--
Changelog:
- 2026-10-07: nothing asks for spend: every paid step runs after its estimate (in the
  install log); `--approve-spend` is gone. A rerun builds the index again unless
  people.csv is unchanged.
- 2026-10-07: processing runs deep-context v2; the review is the page at `/` and
  `bin/deep-context-v2 finish` rebuilds search after it.
- 2026-10-05: Codex uses a persistent tmux worker; the advisor relays chat choices
  and reads its output while the coordinator remains the owner of progress.
- 2026-10-05: Page lines name no reasons; a stopped or deferred step's reason is in its
  action's details (the ready step's `left_to_fix`).
- 2026-10-05: A worker runs setup to the end; the main chat stays free. A QR scan,
  login or permission no longer ends the agent's turn. The review offer reads the
  review queue's own count.
- 2026-10-05: One up-front message, asked synchronously: approve sending contact
  data to Parallel and OpenAI (else LinkedIn only), Gmail defaults to the Powerset
  login, full access is a note. The upload question is gone.
- 2026-10-05: A stopped Gmail Google Cloud setup is the agent's to fix, not the user's.
- 2026-10-05: The LinkedIn review no longer holds setup: search is built first, then
  the agent offers the review; Finish shows `$search` starters for what is enabled.
- 2026-10-05: Finish reports a LinkedIn read that stalled and the data export setup requested instead.
- 2026-10-05: Finish lists what setup deferred (research, profile lookups) and the fix.
- 2026-10-05: The bootstrap comes from the `stable` branch, which every release
  moves to its tag, so the launcher always matches the release it installs.
- 2026-10-05: Restored from 3.13.0 what the onboarding rewrite dropped: where
  this can run (and what to tell chat or cloud users), where to save the skill
  per harness, the full-access ask, and allowed-tools for the bootstrap command.
-->

Installing this skill means running it. Save this file to the harness's skills
folder (Claude Code: `~/.claude/skills/install-powerpacks/SKILL.md`; Codex:
`~/.agents/skills/install-powerpacks/SKILL.md`; Pi: its personal skills folder),
then start the setup below in the same turn. Do not stop after saving it.

The pasted installation instruction starts this whole workflow:

Install → Connect Powerset → Sync LinkedIn → Sync Gmail → Sync iMessage →
Sync WhatsApp → Discover → Enrich → Build Index → Verify → Review (optional,
whenever the user has time).

Own the result. Run commands yourself; involve the user only for browser logins,
QR scans, OS permissions, and the one approval below (the data-sharing consent;
paid steps never ask). Reviewing LinkedIn matches is
offered after search is ready, never in the way of it.
Own repairs through a verified rerun; use the recovery and feedback instructions below.
Open with: "I’ll set this up here. Feel free to ask questions or tell me what you
want as it runs."

## Where this can run

Setup runs shell commands on the user's Mac, so it works only in a local coding
agent there: Claude Code (the CLI, or the desktop app's Code tab in a Local
session), Codex (CLI or app), or Pi. It does not work in ChatGPT chat, claude.ai
chat, the desktop app's Chat tab, or a cloud session (Claude Code on the web,
Cloud in the desktop app, Codex cloud tasks): nothing would land on the Mac.

Bootstrap checks this and prints `STOP:` when it is not on the user's Mac. If the
agent has no shell at all, do not run anything; tell the user which tool to use:

- ChatGPT chat: "This needs a coding agent on your Mac. Please use Codex instead:
  open Terminal, run `curl -fsSL https://chatgpt.com/codex/install.sh | sh`, then
  `cd ~`, run `codex`, and paste the same sentence." (Or the Codex app, in a
  local folder.)
- claude.ai chat or a cloud session: "This needs a coding agent on your Mac.
  Please use Claude Code there: the desktop app's Code tab in a Local session on
  your home folder, or open Terminal, run `curl -fsSL https://claude.ai/install.sh | bash`,
  then `cd ~` and run `claude`. Then paste the same sentence."

Never claim a cloud session changed the user's computer.

## Start and supervise

Before running anything, send the user this one message, then stop and wait for
their reply. Ask it as a plain chat message and end your turn there: do not use a
question pop-up or any asynchronous prompt (those close when you finish talking),
and do not run any command until the user has answered.

> Before I start:
> - **One approval:** to research and enrich your contacts I'll send their names,
>   emails, phone numbers and a short summary of how you know them to Parallel and
>   OpenAI. OpenAI calls are configured with no logging for privacy. Reply
>   **approve** to go ahead; otherwise I'll only process your LinkedIn connections.
> - **Gmail:** I'll add the Google account you sign in to Powerset with. To add
>   others, list them in your reply.
> - **Fewer prompts:** setup runs many local commands; turn on full access so I
>   don't stop to ask each time (steps below). No reply needed for this one.

Put the steps for their harness under the last line:
- Claude Code CLI: restart with `claude --dangerously-skip-permissions` (a running
  session cannot switch into it), or allow the commands in `/permissions`.
- Claude Code desktop app: Settings > Claude Code > "Allow bypass permissions
  mode", then pick Bypass permissions in the mode selector by the send button.
- Codex CLI: restart with `codex --yolo`, or pick a profile in `/permissions`.
- Codex app: the permissions control under the composer > Full access.

The agent cannot read or change the permission mode; only the user can. If they
restart, they paste the same sentence again. If a command is later denied, or the
user has had to approve more than one prompt, repeat the full-access steps.

From the one reply: on **approve**, have the worker run the command below; otherwise add
`--source linkedin`. Pass every extra Gmail address they listed as `--gmail-email`
(the Powerset login is added on its own). Everything after that is logins the user
does back to back near the start (Powerset, LinkedIn, Google, each Gmail approval,
Full Disk Access, WhatsApp QR); then setup runs on its own.

```bash
curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/stable/bin/bootstrap | bash -s -- --powerset
```

Bootstrap installs the runtime and starts the installed coordinator `bin/onboard`.
That coordinator connects Powerset and verifies hosted search first, then owns
imports, processing, review, indexing, and verification. Once hosted search is
verified, tell the user they can search while local setup continues. Do not make
local import or processing completion a prerequisite for cloud search, and do not
assemble a second sequence of skill commands yourself.
Explicit install-only or no-login requests omit `--powerset` and stop earlier.

No project directory or manual clone is needed. Use the printed `powerpacks: repo:`
checkout for subsequent commands and read its `AGENTS.md`. Skills folders are not
workflow data roots. Fresh installs follow the published release; a PR test uses
that checkout's bootstrap and verifies its commit, not the public launcher.

### Advisor and background worker

Read [tmux-worker.md](tmux-worker.md) before starting; include it in the worker's
brief. If you downloaded only this SKILL.md, first save
`https://raw.githubusercontent.com/powerset-co/powerpacks/stable/packs/shared/skills/tmux-worker.md`
as `tmux-worker.md` beside it. A PR test uses that checkout's copy. It owns supervision, recovery, stopping and feedback (`category: install`).
Only the worker starts/resumes the coordinator; the advisor relays chat choices.

Before starting, the advisor creates this chat's visible checklist with the host's
native task/plan tool (Codex's plan tool or Claude's task tools):

1. Install Powerpacks
2. Connect Powerset
3. Sync LinkedIn
4. Sync Gmail
5. Sync iMessage
6. Sync WhatsApp
7. Discover contacts
8. Enrich contacts
9. Build search index
10. Verify search

Keep it current yourself from the worker's output and
`.powerpacks/install/manifest.json`, even when the browser page is open. The
worker reports stage results to the advisor; it never creates or updates a
second task list. Group each source's preparation, login and import under its
sync task. Follow the coordinator's actual order; logins can happen before sync.
For install-only requests, show only the requested setup tasks.

Mark verified cached/no-op work complete. Label user-skipped sources as skipped
(use the host's skipped state when available, otherwise complete with “Skipped”
in the task label). Leave failed or waiting tasks unfinished and name the wait,
such as “Sync Gmail — waiting for sign-in”. Add a review task only when review is
needed. Starting a background index build does not complete it; complete Verify
search only when index validation succeeds. Reuse this checklist after a retry.
If the host has no task tool, keep a compact checklist in chat instead.

On Codex with a local CLI and tmux, use `bin/onboard-worker`. It is standalone and
needs only Python 3, tmux and Codex, so a fresh install can download it before a
checkout exists:

```bash
curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/stable/bin/onboard-worker -o /tmp/powerpacks-onboard-worker.py
```

For a PR test, use that checkout's helper and bootstrap instead. Do not download
stable over a PR test. If tmux or a suitable Codex CLI is unavailable, use the
host's native background agent; do not install a second agent harness silently.
Other hosts use one native worker operating the bootstrap/coordinator in a tmux
session named `powerpacks-onboarding`, as Deep Context does. Before sending any
command, inspect its pane and the live coordinator; reuse active work instead of
starting another copy. Without a background agent, supervise here.

Write a private task file containing the saved skill's absolute path, the exact
bootstrap command above (including requested options), account/history/source
choices, existing consent and budget, and the requested outcome. The worker must
read that skill, execute the command and supervise it, not repeat the advisor's
up-front question or launch another worker. Keep the coordinator responsible for
processing too: do not hand off to standalone Deep Context stages that leave the
install page frozen. If a required restriction cannot be expressed by the
coordinator, tell the advisor before changing the execution path; never present
its old completed manifest as current progress. On an interrupted install, include
the known checkout and tell it to inspect the live process and saved continuation.
Never put tokens, passwords or message content in the task or steering messages.

Run the helper from the advisor's current shell. Supply the user's actual sandbox
and approval policy explicitly; don't infer full access from the install request.
For an already authorized Full access session, for example:

```bash
python3 /tmp/powerpacks-onboard-worker.py start --prompt-file /tmp/powerpacks-worker-task.txt --sandbox danger-full-access --approval never
python3 /tmp/powerpacks-onboard-worker.py read
python3 /tmp/powerpacks-onboard-worker.py send 'The user says: skip Gmail for now. Keep the other choices and continue.'
```

`start` reuses a live worker; after an exit it starts a replacement with the supplied
task. If a reused worker has finished its previous task, send the new authorized
request; `start` deliberately does not submit it again. Use the same helper/socket
throughout. The child uses `--no-daemon` to avoid
an unrelated shared Codex server; the launcher passes the originating Mac app for
permission guidance. This grants no Full Disk Access. The tmux process can outlive
the advisor's tool call; `running` means that process is alive, not setup complete.
Inspect startup output before steering: a CLI login or trust prompt needs handling
first; process creation alone does not mean the Codex composer is ready.

**Communication:** `send` steers the worker's active turn; `read` returns its recent
terminal output. This is the shared channel—no new queue or pipeline state file.
Read the existing manifest as well as the worker output: it owns the exact action,
progress and retry command. Open each printed `STATUS PAGE:` URL beside chat;
let the login commands manage their Chrome windows; open review separately. A `NEEDS USER:` or `BLOCKED:` reply
needs advisor attention; relay the user's answer through `send`. Verify changes
from the manifest before claiming a source was skipped or search is ready.

Keep observing login, QR and permission completion; no "done" reply is needed.
Keep the status/review server available when the worker finishes.

## Keep the status page beside chat

Open the exact printed `STATUS PAGE:` URL immediately. Use the host's browser-pane
tool when available; reuse the tab on retries. On Codex, use `open_in_codex` when
available. Other hosts may use their supported pane or system browser.

Keep this tab on `/install`. Login commands reuse Powerpacks-managed Chrome
sessions and close their windows when finished. Open review separately; never
navigate the status tab to login or callback URLs. The page reads saved progress and the real
Modal status files; it does not own the pipeline process. Closing or restarting
its server must not stop the coordinator.

Use the page for progress, with short chat only for input or needed action. The
animation signals activity, not invented percentages. Preserve a disconnected
page and reconnect to the same server; never turn a connection loss into a source
failure. Check `/healthz`, `/api/install`, and `.powerpacks/install/server.log`.

## Choices and waiting

Rerun the same install instruction to resume. Keep the existing checkout, accounts,
projects, imports and processing caches. Check access again, but do not force a
login or recreate a working project. Saved source/account/history choices survive
the bootstrap; explicit new choices override them. `--refresh` requests a resync
for that invocation only. Unchanged prepared contacts keep their current index;
missing outputs or changed inputs are repaired or processed by the normal flow.

Default to LinkedIn, Gmail's past year, iMessage, and WhatsApp. Inform the user
once and keep going. Apply already-given account/history choices. Gmail defaults to
the Powerset login's address. Only without a Powerset account does the coordinator
ask which Gmail accounts to add: ask the user in chat (a plain message, then wait
for the reply), append one `--gmail-email` per address to the saved
`retry_command`, first address first, and run it. Every address gets
its own browser approval; the coordinator allows them all beforehand. Powerset, LinkedIn and Google login flows reuse
headless Chrome (or Brave); a window opens only when a login is needed, closes
once the user is signed in, and the sessions are kept for later runs. LinkedIn
reads up to ~3,000 connections per run, then processing starts; the rest sync on
later runs. Never ask for LinkedIn's emailed export, the user's LinkedIn URL, or
their email: the coordinator reads them from those sessions.

Existing `--source`, `--gmail-email`, `--sync-after`, `--wacli-store`, and `--refresh`
options pass through bootstrap. Saved source/account/history/store choices survive
resume. Use `--refresh` only for a requested resync. Never choose an arbitrary Gmail
account from several or switch to a shared Powerset network merely because it works.
Unprovisioned or unindexed hosted search does not block local source setup.

When the Gmail step says "Gmail setup stopped in Google Cloud", read its
`action.details` (the page Google showed, `stuck_at`, the project) and fix it
here; never hand the user Google Cloud steps or open the console for them in a
browser tab. If it needs a project the user owns, rerun the saved
`retry_command`; setup creates their own.

Read the manifest's `action` when waiting. The coordinator owns the sequence;
the agent handles an action that needs setup repair or approval.
Do not hand its commands to the user. For Messages permissions, the existing
`/api/install/permissions` action opens Full Disk Access and highlights the detected
app. Tell the user which app was identified; only the user grants OS access.

"Skip Gmail for now" also applies to iMessage, WhatsApp, and LinkedIn. Stop only
that pipeline's owned work, wait for it to exit, append `--skip-source gmail`
(or the requested source) to the saved `retry_command`, and run it yourself.
Keep all other choices. To resume that source, remove its skip option. Existing
imports, stores, and accounts survive; never log out or clear data for a retry.

## Approval and repair

Paid steps (synthesis, duplicate processing, enrichment, indexing) run without
asking; each estimate is written to the install log first. Modal checks its
shared cache before spending.

An existing index is reused and verified locally when people.csv is unchanged;
otherwise the rerun builds it again.

Read command output, saved progress, and `.powerpacks/install/install.log`:

| Result | Action |
| --- | --- |
| `DONE:` | Verify the saved result; only index validation proves local search readiness. |
| `NEEDS YOU:` | Handle the saved action or follow the existing live wait; retain ownership. |
| `FAILED:` | Inspect the exact failure, repair within scope, rerun the saved coordinator. |
| `STOP:` | Explain the environment required; do not continue on another computer. |

Use the manifest's canonical `retry_command` to resume, following the shared
[repair and feedback instructions](tmux-worker.md#repair-and-finish). Never delete
`.powerpacks`, replace configuration or reset reviews to recover. If the index
build failed, rerun that continuation against Modal's cache.

## Finish

After index validation succeeds, say: **Indexed <N> people.** Use the
verified index's people count, including on reruns that reuse an existing index.
Then add: Try `$search software engineers in New York in my network`.
Do not append cloud network counts, source recaps, or optional review prompts.
Keep those details on the status page or explain them when asked.

If the requested workflow could not finish, briefly state what remains blocked
and any action only the user can take. For install-only requests, say Powerpacks
is installed; do not claim an index was built. If the user also asked for a
search, run the installed search skill once its backend is ready.
