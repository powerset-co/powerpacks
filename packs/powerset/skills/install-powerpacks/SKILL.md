---
name: install-powerpacks
description: Install Powerpacks from one URL and keep going until the user can search — clone the public repo, install every Powerpacks skill for this harness (Claude Code, Codex, or Pi), log in to Powerset, then run setup in the same session without a restart. Use for "install powerpacks", "download the powerpacks skill", "use powerpacks to set up ...", or "set up my local network search using my Powerset account".
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
- 2026-10-02: The install sentence IS the onboarding. No "save the file and
  wait for a second sentence": bootstrap runs at once, then login, then $setup,
  then the first search, all in the same session. Powerset is the only route
  (no --powerset flag, no "do you have an account?"); the Gmail-tools question
  moved to $import-gmail, which needs them. Permissions note cut to one line.
-->

One sentence does everything:

> Download and install the Powerpacks skill from
> https://powerset.dev/powerpacks

When the user pastes that, they are done typing. Everything below runs in this
session, in order, without a restart, and stops only when a step needs the
human (a browser login, a file only they can download). The finish line is a
real search result from their own network, not an "installed" message.

## Where this can run

This runs shell commands on the user's Mac, so it works only in a local coding
agent there: Claude Code (the CLI, or the desktop app's Code tab in a Local
session), Codex (CLI or app), or Pi. It does not work in claude.ai chat,
ChatGPT chat, the desktop app's Chat tab, or a Claude Code cloud session: none
of those run commands on the user's Mac.

The Step 1 command checks this itself and prints a line starting with `STOP:`
when it is not on the user's Mac. The one case it cannot catch is an agent
with no shell tool at all: then stop here and tell the user which tool to use
instead, in a few lines:

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

## What to say while it runs

Keep the screen almost empty. One short status line per stage, replaced as it
moves, no command output, no explanations of what Powerpacks is:

```text
Powerpacks
✓ Installed
● Signing you in to Powerset
○ Importing your LinkedIn network
○ Running your first search
```

Say "No action needed" while nothing is needed. When something is, say
exactly what to click or paste and nothing else.

## Step 0 — save this file

Save this SKILL.md into the current harness's skills directory, creating the
folder, then go straight to Step 1. Do not stop here and do not wait for a
second sentence.

- Claude Code: `~/.claude/skills/install-powerpacks/SKILL.md`
- Codex: `~/.agents/skills/install-powerpacks/SKILL.md`
- Pi / other: the harness's personal skills directory, same layout.

## Step 1 — install

**Permissions.** If the harness asks the user to approve this command, say
once: "Approve this; it installs Powerpacks on your Mac (free, local, no
uploads)." If the user has to approve a second prompt, tell them the one
setting that stops the prompts for their harness — Claude Code: `/permissions`,
or restart with `claude --dangerously-skip-permissions`; Codex: `/permissions`,
or restart with `codex --yolo` — then continue.

1. **Run one command:**

   ```bash
   curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/main/bin/bootstrap | bash
   ```

   It downloads Powerpacks to `~/powerpacks` (or reuses an existing checkout),
   pins it to the newest release, installs the skills for every agent found on
   this Mac, and creates `.env` from the Powerset template (never touching an
   existing one). Local-only: no paid APIs, no uploads, no logins.

2. **Read its LAST line and do exactly what it says.** Nothing else in the
   output needs a decision.

   - `DONE: ...` — installed. Go to Step 2.
   - `NEEDS YOU: ...` — a step only the human can do (a click, a password).
     Show the user that line word for word, wait for them to say it is done,
     then run the same command again.
   - `STOP: ...` — wrong place to run this. Show the user that line and the
     matching tool from "Where this can run". Do not continue.
   - `FAILED: ...` — show the user that line and the lines above it. Do not
     continue.

   Run the command as many times as those lines ask; it is safe to repeat and
   skips every step already done.

## Step 2 — set up, in this session

The harness's skill registry is snapshotted at session start, but the skills
are now plain files on disk. Read `setup/SKILL.md` from this agent's skills
folder (`~/.claude/skills/` or `~/.agents/skills/`) and follow it as if it had
been routed. It logs the user in to Powerset (browser consent — the one stop
every new user hits), pulls their provisioned keys, imports their LinkedIn
`Connections.csv`, indexes it, and ends with their first search.

If the user's original sentence named another source (Gmail, iMessage,
WhatsApp) or a search, do that right after setup: Gmail -> `import-gmail`;
iMessage/WhatsApp -> `import-messages`; processing -> `deep-context`; searches
-> `search`. New sessions pick up the full skill list automatically.

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
  provisioning API base; `$setup` Step 3 pulls them into local `.env`.
