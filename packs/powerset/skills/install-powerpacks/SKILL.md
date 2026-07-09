---
name: install-powerpacks
description: Bootstrap Powerpacks into this agent from one URL — clone the public repo, install every Powerpacks skill for this harness (Claude Code, Codex, or Pi), then continue the user's ask (e.g. "set up my local network search") in the same session without a restart. Use for "install powerpacks", "download the powerpacks skill", "use powerpacks to set up ...".
license: MIT
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.0.0
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
-->

One sentence installs everything:

> Download and install the Powerpacks skill from
> https://powerset.dev/powerpacks

Then the user just says what they want:

> Use powerpacks to set up my local network search.

## Step 0 — install this file (the "download and install" sentence)

Save this SKILL.md into the current harness's skills directory, creating the folder:

- Claude Code: `~/.claude/skills/install-powerpacks/SKILL.md`
- Codex: `~/.codex/skills/install-powerpacks/SKILL.md`
- Pi / other: the harness's personal skills directory, same layout.

That is the whole "install the skill" step. Do NOT clone anything yet — the full
install runs the first time the user asks to actually use Powerpacks.

## Step 1 — when the user asks to USE powerpacks

Trigger phrases (any harness):

- "Use powerpacks to set up my local network search"
- "Set up powerpacks" / "install powerpacks fully"
- "Import my LinkedIn/Gmail/iMessage network with powerpacks"
- "Search my network for ..." (when Powerpacks skills are not installed yet)

Do the following, in order:

1. **Resolve or clone the repo.** Use the first that exists: `$POWERPACKS_REPO_ROOT`,
   `~/powerpacks`, `~/workspace/powerpacks`. Otherwise clone the public repo:

   ```bash
   git clone https://github.com/powerset-co/powerpacks.git ~/powerpacks
   ```

2. **Run the installer for THIS harness** from the repo root (it pulls latest main
   when safe, sets up the Python env via uv, installs/refreshes every Powerpacks
   skill, and cleans stale ones):

   ```bash
   ./install.sh claude-code   # Claude Code -> ~/.claude/skills
   ./install.sh codex         # Codex       -> ~/.codex/skills
   adapters/pi/install.sh     # Pi
   ```

   Local-only: git + uv/Python setup, no paid APIs, no uploads. Downstream skills
   gate their own spend and logins.

3. **Continue in THIS session — no restart.** The harness's skill registry is
   snapshotted at session start, but you do not need it: the skills are now plain
   files on disk. Read the one that matches the user's ask directly (e.g.
   `~/.claude/skills/setup/SKILL.md`) and follow it as if it had been routed.
   New sessions pick up the full skill list automatically.

4. **Route the ask:**
   - "set up my local network search" (LinkedIn export -> merge -> search index)
     -> follow the installed `setup` skill (`$setup`).
   - broader source linking/onboarding -> `onboard`; Gmail -> `import-gmail`;
     iMessage/WhatsApp -> `import-messages`; then searches -> `search`.

## Notes

- The repo is public; no credentials are needed to install. Powerset login,
  Google OAuth, Full Disk Access, and any spend are asked for by the specific
  skills that need them, never during install.
- To refresh later: `$update-powerpacks` (installed with everything else).
