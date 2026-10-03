---
name: install-powerpacks
description: Bootstrap Powerpacks into this agent from one URL — clone the public repo, install every Powerpacks skill for this harness (Claude Code, Codex, or Pi), initialize the hosted Powerset config when requested, then continue the user's ask in the same session without a restart. Use for "install powerpacks", "download the powerpacks skill", "use powerpacks to set up ...", or "set up my local network search using my Powerset account".
license: MIT
allowed-tools: Bash(curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/main/bin/bootstrap *)
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.1.0
  summary: One-sentence bootstrap for the full Powerpacks skill suite
  download-url: https://powerset.dev/powerpacks
  tags:
    - powerpacks
    - install
    - bootstrap
    - network-search
---

# Powerpacks Installer

<!--
Created: 2026-07-08
Changelog:
- 2026-10-02: The download sentence runs the full install and opens live progress
  in the browser pane; the same local server serves setup and review afterwards.
- 2026-07-08: Initial ShareOne-style one-URL bootstrap skill.
- 2026-07-10: Define the Powerset-environment route and provisioning API URL.
- 2026-07-12: Hosted-config init is conditional on the user choosing Powerset;
  otherwise $setup Step 1 asks explicitly (own keys are the alternative).
- 2026-07-24: A fresh clone is pinned to the newest published release via
  bin/powerpacks-channel instead of being left on the default branch.
- 2026-08-24: Pull the provisioned Parallel key alongside Modal and OpenAI.
- 2026-08-31: Pull the provisioned Powerset API key for local profile hydration.
- 2026-09-28: New Step 1.3 installs the machine tools Gmail import needs
  (msgvault, gcloud, node, Chrome) so install owns environment setup. Dropped
  the dead `onboard` route.
- 2026-09-30: "Where this can run" check stops claude.ai chat, ChatGPT chat,
  and cloud sessions and names the tool to use instead; Permissions note
  before Step 1 with per-harness bypass steps and a stop-on-denial rule;
  allowed-tools pre-approves Step 1's local commands in Claude Code.
- 2026-09-30: Step 1 is one command, `bin/bootstrap`, and one rule: do what its
  last line says. The clone, release pin, installer, tool check and .env moved
  into the script so any model can run it and the user only has to approve.
- 2026-09-30: Codex skills install to `~/.agents/skills`; `~/.codex/skills` is
  Codex's deprecated location and the installer cleans our skills out of it.
-->

One sentence installs everything:

> Download and install the Powerpacks skill from
> https://powerset.dev/powerpacks

The install opens a live progress page beside the conversation when the host
supports a browser pane. After installation, a Powerset user can say:

> Use Powerpacks to set up my local network search using my Powerset account.

This wording is explicit but not required. With the shorter prompt (no
Powerset mentioned), `$setup` asks whether to log in with a Powerset account
before anything runs; answering yes gives the same Powerset-backed setup.

## Where this can run

This skill runs shell commands on the user's Mac, so it works only in a local
coding agent there: Claude Code (the CLI, or the desktop app's Code tab in a
Local session), Codex (CLI or app), or Pi. It does not work in claude.ai chat,
ChatGPT chat, the desktop app's Chat tab, or a Claude Code cloud session
(Claude Code on the web, or Cloud in the desktop app): none of those run
commands on the user's Mac, so nothing lands where Powerpacks looks for it.

The Step 1 command checks this itself and prints a line starting with
`STOP:` when it is not on the user's Mac. The one case it cannot catch is an
agent with no shell tool at all: then stop here, do not run Step 0 or Step 1,
and tell the user which tool to use instead, in a few lines:

- In ChatGPT chat: "This needs a coding agent on your Mac. Please use Codex
  instead: open Terminal, run
  `curl -fsSL https://chatgpt.com/codex/install.sh | sh`
  (https://learn.chatgpt.com/docs/codex/cli), then `cd ~`, run `codex`, and
  paste the same sentence."
- In claude.ai chat or a cloud session: "This needs a coding agent on your
  Mac. Please use the Claude Code app or CLI on your Mac instead: install the
  app from https://code.claude.com/docs/en/desktop-quickstart and open a Local
  session on your home folder, or open Terminal, run
  `curl -fsSL https://claude.ai/install.sh | bash`
  (https://code.claude.com/docs/en/setup), then `cd ~` and run `claude`. Then
  paste the same sentence."

## Step 0 — save this skill and start the install

Save this SKILL.md into the current harness's skills directory, creating the folder:

- Claude Code: `~/.claude/skills/install-powerpacks/SKILL.md`
- Codex: `~/.agents/skills/install-powerpacks/SKILL.md`
- Pi / other: the harness's personal skills directory, same layout.

Continue directly to Step 1 in this session. The download-and-install sentence
requests the full local install, including the live progress page. It does not
request account logins, contact imports, paid work, or uploads.

## Step 1 — install Powerpacks and open progress

Trigger phrases (any harness):

- "Download and install the Powerpacks skill from https://powerset.dev/powerpacks"
- "Use powerpacks to set up my local network search"
- "Use powerpacks to set up my local network search using my Powerset account"
- "Set up powerpacks" / "install powerpacks fully"
- "Import my LinkedIn/Gmail/iMessage network with powerpacks"
- "Search my network for ..." (when Powerpacks skills are not installed yet)

Use the session's existing permissions. If a command is denied, explain which
command was denied and stop; do not bypass the denial.

Do the following, in order:

1. **Run one command asynchronously.** Let the shell tool yield early so you
   can open the page while installation continues (Codex: short
   `yield_time_ms`; Claude Code: background Bash). Add `--powerset` only when the ask named Powerset
   ("using my Powerset account"):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/main/bin/bootstrap | bash
   ```

   For a Powerset ask, append `-s -- --powerset` to `bash`.

   It downloads Powerpacks to `~/powerpacks` (or reuses an existing checkout),
   pins it to the newest release, installs the skills for every agent found on
   this Mac, and, only when the user chose Powerset, runs
   `cp packs/powerset/templates/env.powerset.example .env` (never touching an
   existing `.env`). Local-only: no paid APIs, no uploads, no logins.

2. **Open the page while the command runs.** Watch the output for
   `STATUS PAGE: <url>`. Use that URL, not an assumed port. In Codex desktop,
   call `open_in_codex` with a browser target and `placement: "right"`;
   in another host use its browser/preview pane. In a CLI with no pane,
   open the URL in the default browser. Reuse the same tab on retries and for
   subsequent setup or review. The server stays up when the command finishes.

   The page reads `.powerpacks/install/manifest.json` written by the installer.
   Never write progress yourself, invent a percentage, or claim a step completed
   from an animation. A queued panel-open result does not prove it rendered.
   If the page fails, check its URL, `/healthz`, `/api/install`, and the saved
   `.powerpacks/install/server.log`. Restart the same server with the repo's
   pinned Python: `python -m packs.ingestion.primitives.deep_context.review.cli
   start --stage install --port <same port>` from the repo, then reopen the returned URL.
   For `Tab content couldn't render`, inspect the host app logs; distinguish a
   host panel failure from an unreachable server. Do not repeatedly open duplicates.

3. **Read its LAST line and act on it.** Continue watching the asynchronous
   command until it exits; opening the page is not installation completion.

   - `DONE: ...` — installed. Continue below. For an install-only ask, leave
     the installed page open and ask what the user wants to connect in chat.
   - `NEEDS YOU: ...` — a step only the human can do (a click, a password).
     Show the user that line word for word, wait for them to say it is done,
     then run the same command again.
   - `ASK: ...` — one yes/no question about installing the free Gmail-import
     tools. Ask the user that question. Yes: run the command again with
     `--tools` added. No: run it again with `--no-tools` added.
   - `STOP: ...` — wrong place to run this. Show the user that line and the
     matching tool from "Where this can run". Do not continue.
   - `FAILED: ...` — inspect the failure and the saved install/server logs,
     fix a free local setup problem, then rerun the same command. Do not move
     into imports or paid work. If the same failure repeats after a repair,
     report the exact remaining problem and the next action. Passwords, visible
     clicks and denied permissions still require the user.

   Run the command as many times as those lines ask; it is safe to repeat and
   skips every step already done.

4. **Continue in THIS session — no restart.** The harness's skill registry is
   snapshotted at session start, but you do not need it: the skills are now plain
   files on disk. Read the one that matches the user's ask directly (e.g.
   `~/.claude/skills/setup/SKILL.md`) and follow it as if it had been routed.
   New sessions pick up the full skill list automatically.

5. **Route the ask:**
   - "set up my local network search" with or without "using my Powerset
     account" -> follow `$setup` (LinkedIn export -> merge -> search index).
     Its Steps 1-3 authenticate the Powerset user and pull that user's
     provisioned Modal/OpenAI/Parallel/Powerset API keys before the LinkedIn import; when the prompt
     didn't name Powerset, its Step 1 first asks whether to log in with a
     Powerset account (the user's own keys are the alternative). Do not run a
     separate `$powerset setup`; that would duplicate the same login/key pull.
   - Gmail -> `import-gmail`; iMessage/WhatsApp -> `import-messages`;
     processing -> `deep-context`; then searches -> `search`.

   Keep progress and decisions beside the chat. Open `/accounts` on the same
   server for connections, and `/` for Deep Context review when its store is
   ready. Use the existing skills' commands and their real status; do not start
   a second server or ask the user to copy commands. Only native login or macOS
   permission actions need another window.

## Notes

- The repo is public; no credentials are needed to install. Powerset login,
   Google OAuth, Full Disk Access, and any spend are asked for by the specific
  skills that need them, never during install.
- To refresh later: `$update-powerpacks` (installed with everything else).
- Keep these URLs distinct:
  - Install skill: `https://powerset.dev/powerpacks`
  - Provisioning API base: `https://search-api-7wk4uhe77q-uw.a.run.app`
  - Auth0 audience identifier only: `https://api.powerset.dev`
- The provisioning calls are `/v2/integrations/modal/token`,
  `/v2/integrations/openai/key`, and `/v2/integrations/parallel/key` on the
  provisioning API base. "Using my Powerset account" means authenticate that
  user and pull those allowlisted values into local `.env`.
