---
name: install-powerpacks
description: Set up Powerpacks from one pasted URL through a resumable account, import, processing, and search-index workflow with live progress and automatic cost checks. Use for "Download and install the Powerpacks skill from https://powerset.dev/powerpacks", "install Powerpacks", or first-time Powerpacks setup.
license: MIT
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.4.2
  summary: Install and build your searchable network from one sentence
  download-url: https://powerset.dev/powerpacks
  tags:
    - powerpacks
    - install
    - network-search
---

# Set up Powerpacks

The pasted installation instruction starts this whole workflow:

Install → Connect Powerset → Sync LinkedIn → Sync Gmail → Sync iMessage →
Sync WhatsApp → Discover → Enrich → Review when needed → Build Index → Verify.

Own the result. Run commands yourself; involve the user only for browser logins,
QR scans, OS permissions, required review, and upload authorization.
Open with: "I’ll set this up here. Feel free to ask questions or tell me what you
want as it runs."

## Start and supervise

This needs a shell on the user's Mac. Without one, explain where to paste the
instruction; do not claim a cloud installation changed their computer.

Before running anything, ask the two things setup needs, in one message: "Which
Gmail accounts should I add? The first one owns the Gmail setup." and "Building
your search index uploads your contacts to your Powerset workspace. OK?" Pass
each address as `--gmail-email`, first address first, and `--approve-upload` on a
yes. Everything after that is logins the user
does back to back near the start (Powerset, LinkedIn, Google, each Gmail
approval, Full Disk Access, WhatsApp QR); then setup runs on its own.

```bash
curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/main/bin/bootstrap | bash -s -- --powerset --approve-upload --gmail-email first@example.com --gmail-email second@example.com
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
once and keep going. Apply already-given account/history choices. Gmail's identity
is independent of Powerset's identity. If the coordinator still asks which Gmail
accounts to add (none were passed and msgvault has none), ask the user in chat
(offer the action's `suggested` address), append one `--gmail-email` per address
to the saved `retry_command`, first address first, and run it. Every address gets
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

Upload consent is separate from spend: use `--approve-upload` when the user
authorizes uploading contacts to the configured workspace. Reuse that authorization
while repairing/resuming the same requested upload; do not ask again. Existing downloaded indexes are reused
and verified locally without another upload.

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

Report installation, account access, hosted search, imported sources, and verified
local search separately. A network's people count does not prove search readiness.
Keep internal IDs and provider details in troubleshooting. If the user also asked
for a search, run the installed search skill once its requested backend is ready.
