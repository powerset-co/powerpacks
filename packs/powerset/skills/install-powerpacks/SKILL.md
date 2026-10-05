---
name: install-powerpacks
description: Set up Powerpacks from one pasted URL through a resumable account, import, processing, and search-index workflow with live progress and automatic cost checks. Use for "Download and install the Powerpacks skill from https://powerset.dev/powerpacks", "install Powerpacks", or first-time Powerpacks setup; installing it means running the setup now, not only saving the file.
license: MIT
allowed-tools: Bash(curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/stable/bin/bootstrap *)
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.5.0
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
QR scans, OS permissions, and the one approval below. Reviewing LinkedIn matches is
offered after search is ready, never in the way of it.
Never edit files in the Powerpacks checkout: a changed checkout cannot update.
If something looks broken, tell the user what you saw and offer `$feedback`.
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

From the one reply: on **approve**, run the command below; otherwise add
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

Run asynchronously and retain responsibility until completion or a concrete
required user action. If the user requests a worker and supervisor, dispatch one
worker to run the coordinator, supervise it in the original chat, and relay chat
choices. Do not launch a competing installer or finish supervising while owned
work is still running. A browser action does not require a "done" message.

## Keep the status page beside chat

Open the exact printed `STATUS PAGE:` URL immediately. Use the host's browser-pane
tool when available; reuse the tab on retries. On Codex, use `open_in_codex` when
available. Other hosts may use their supported pane or system browser.

Keep this tab on `/install`. Login and review open in the system's default browser,
never by navigating the status tab. The page reads saved progress and the real
Modal status files; it does not own the pipeline process. Closing or restarting
its server must not stop the coordinator.

Use the page for progress, with short chat only for input or needed action. The
animation signals activity, not invented percentages. Preserve a disconnected
page and reconnect to the same server; never turn a connection loss into a source
failure. Check `/healthz`, `/api/install`, and `.powerpacks/install/server.log`.

## Choices and waiting

Default to LinkedIn, Gmail's past year, iMessage, and WhatsApp. Inform the user
once and keep going. Apply already-given account/history choices. Gmail defaults to
the Powerset login's address. Only without a Powerset account does the coordinator
ask which Gmail accounts to add: ask the user in chat (a plain message, then wait
for the reply), append one `--gmail-email` per address to the saved
`retry_command`, first address first, and run it. Every address gets
its own browser approval; the coordinator allows them all beforehand. LinkedIn and the Google Console run in
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

During onboarding, each paid step runs automatically when its estimated cost is
below $500. At $500 or more, show the estimate and ask the user before running.
This applies to synthesis, duplicate processing, enrichment, and indexing;
do not ask at their former lower thresholds. Estimates and cache checks still
run. Use the saved continuation for an authorized larger estimate, rather than
running a separate paid command. Modal checks its shared cache before spending.

Existing downloaded indexes are reused and verified locally without another upload.

Read command output, saved progress, and `.powerpacks/install/install.log`:

| Result | Action |
| --- | --- |
| `DONE:` | Verify the saved result; only index validation proves local search readiness. |
| `NEEDS YOU:` | Handle the saved action or follow the existing live wait; retain ownership. |
| `FAILED:` | Inspect the exact failure, repair within scope, rerun the saved coordinator. |
| `STOP:` | Explain the environment required; do not continue on another computer. |

Use the manifest's canonical `retry_command` to resume. Native artifacts and
SQLite, not UI labels, decide completed work. Never delete `.powerpacks`, replace
configuration, reset reviews, or bypass spend gates to recover. If a targeted
repair repeats the same failure, explain the remaining cause and needed action.
For an unclear setup problem, load the installed `powerpacks-doctor` skill.
If Modal disconnected after dispatch, inspect the existing run and recover its
result; do not blindly dispatch another paid job.

## Finish

The user does their part once, at the start; everything else runs on its own. When
the ready step completes, say it in this order, in plain words:

1. **Search is ready.** Then show only what this setup turned on:
   - Local network (local search validated): `$search find people who … in my network`
   - Powerset network (hosted search connected: the credentials step completed, not
     the "Hosted search isn't enabled" warning): `$search find people who would be a
     good fit for <job post URL> in my Powerset network`
2. **What is left to fix**, if the ready message lists any (research skipped for a
   missing key or no provider credit, a LinkedIn read that stalled): one line each with
   what fixes it, and that the next setup run picks it up. When LinkedIn stopped
   sending connections, say how many were read of how many, that it stopped to keep
   their account safe, and that setup asked LinkedIn for their data export (it can
   take a day); the next setup run imports it. Never ask the user to download or
   upload the export.
3. **Optional review.** When the ready step has a `review` action, say: "If you have
   time, <its text> Want me to open them?" Only on a yes, open the action's URL beside
   chat (the host's browser pane, else the default browser). When they are done, say
   you are rebuilding search with their decisions and run the saved `retry_command`.
   They can come back to it later by saying "review my pending contacts"
   (`bin/deep-context review linkedin`).

A network's people count does not prove search readiness; only index validation
does. Keep internal IDs and provider details in troubleshooting. If the user also
asked for a search, run the installed search skill once its backend is ready.
