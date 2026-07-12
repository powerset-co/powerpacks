---
name: install-powerpacks
description: Bootstrap Powerpacks into this agent from one URL — clone the public repo, install every Powerpacks skill for this harness (Claude Code, Codex, or Pi), initialize the hosted Powerset config when requested, then continue the user's ask in the same session without a restart. Use for "install powerpacks", "download the powerpacks skill", "use powerpacks to set up ...", or "set up my local network search using my Powerset account".
license: MIT
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.0.2
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
-->

One sentence installs everything:

> Download and install the Powerpacks skill from
> https://powerset.dev/powerpacks

Then a Powerset user says:

> Use Powerpacks to set up my local network search using my Powerset account.

This wording is explicit but not required. With the shorter prompt (no
Powerset mentioned), `$setup` asks whether to log in with a Powerset account
before anything runs; answering yes gives the same Powerset-backed setup.

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
- "Use powerpacks to set up my local network search using my Powerset account"
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

3. **Initialize the hosted config only when the user chose Powerset.** If the
   ask said "using my Powerset account" (or otherwise named Powerset), work in
   the canonical repo. If `.env` does not exist, copy the public hosted config
   and restrict its permissions:

   ```bash
   cp packs/powerset/templates/env.powerset.example .env
   chmod 600 .env
   ```

   If `.env` already exists, preserve its secrets and other settings. Ensure its
   public Powerset URL/Auth0 keys match
   `packs/powerset/templates/env.powerset.example`; do not replace the whole file.

   If the ask did NOT mention Powerset (plain "set up my local network
   search"), skip this step — `$setup` Step 1 asks the user whether they have
   a Powerset account to log in with and initializes `.env` on a yes (own
   Modal/OpenAI keys are the alternative).

4. **Continue in THIS session — no restart.** The harness's skill registry is
   snapshotted at session start, but you do not need it: the skills are now plain
   files on disk. Read the one that matches the user's ask directly (e.g.
   `~/.claude/skills/setup/SKILL.md`) and follow it as if it had been routed.
   New sessions pick up the full skill list automatically.

5. **Route the ask:**
   - "set up my local network search" with or without "using my Powerset
     account" -> follow `$setup` (LinkedIn export -> merge -> search index).
     Its Steps 1-3 authenticate the Powerset user and pull that user's
     provisioned Modal/OpenAI keys before the LinkedIn import; when the prompt
     didn't name Powerset, its Step 1 first asks whether to log in with a
     Powerset account (the user's own keys are the alternative). Do not run a
     separate `$powerset setup`; that would duplicate the same login/key pull.
   - broader source linking/onboarding -> `onboard`; Gmail -> `import-gmail`;
     iMessage/WhatsApp -> `import-messages`; then searches -> `search`.

## Notes

- The repo is public; no credentials are needed to install. Powerset login,
  Google OAuth, Full Disk Access, and any spend are asked for by the specific
  skills that need them, never during install.
- To refresh later: `$update-powerpacks` (installed with everything else).
- Keep these URLs distinct:
  - Install skill: `https://powerset.dev/powerpacks`
  - Provisioning API base: `https://search-api-7wk4uhe77q-uw.a.run.app`
  - Auth0 audience identifier only: `https://api.powerset.dev`
- The provisioning calls are
  `/v2/integrations/modal/token` and `/v2/integrations/openai/key` on the
  provisioning API base. "Using my Powerset account" means authenticate that
  user and pull those allowlisted values into local `.env`.
