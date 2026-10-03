---
name: install-powerpacks
description: Set up Powerpacks from one pasted URL. Install locally, open live progress, connect the user's Powerset account, and verify their network in the same session. Use for "Download and install the Powerpacks skill from https://powerset.dev/powerpacks", "install Powerpacks", or first-time Powerpacks setup.
license: MIT
allowed-tools: Bash(curl -fsSL https://raw.githubusercontent.com/powerset-co/powerpacks/main/bin/bootstrap *)
metadata:
  slug: install-powerpacks
  display-name: Powerpacks Installer
  version: 1.2.0
  summary: Install, connect your account, and check your network from one sentence
  download-url: https://powerset.dev/powerpacks
  tags:
    - powerpacks
    - install
    - network-search
---

# Set up Powerpacks

<!-- Changelog: 2026-10-02 — one script owns installation, account connection,
network checks, and real progress; the agent handles human actions and recovery. -->

The user pastes:

> Download and install the Powerpacks skill from https://powerset.dev/powerpacks

Carry that request through to a working account and a verified network. Do not
stop after saving this file, ask them to type another skill, or hand them an
installation checklist. The script does the work; you watch its progress,
handle anything requiring the user, and recover from failures.

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
short shell `yield_time_ms`; in Claude Code, use background Bash.

This installs Powerpacks, preserves existing configuration, connects the
Powerset account, checks access to search, and checks the selected network.
Already-valid login is skipped. The normal pasted instruction authorizes this
setup, including local credential configuration. Browser sign-in still belongs
to the user. It does not authorize uploading contacts, paid research, or reading
mail and messages.

- Explicit "install only", "don't log in", or custom/local-only setup: omit
  `--powerset`. Follow that narrower request; don't report account readiness.
- Keep `--no-tools` for initial setup. Gmail tools are installed only if the
  user later asks to connect Gmail. Don't delay first use with optional imports.
- Reruns update the existing checkout within its selected release channel and
  preserve configuration and network data. Fresh installs follow the published
  release. Do not switch channels or edit application code to repair setup.

## Show the live page

Watch for `STATUS PAGE: <url>` and open that exact URL immediately, before
waiting for the command to finish. Reuse the tab on retries.

- If the host exposes a browser-pane tool, use it to open the page beside chat.
  Use `open_in_codex` only if that tool is actually available.
- Otherwise open the URL in the Mac's default browser with `open "<url>"`.
  Codex CLI can use this browser tab; it cannot render a custom animation in chat.
- A printed URL or queued open request is not proof of a rendered page. Check
  the returned browser state when available, or the page's HTTP response.

The animation and step labels come from the script's
`.powerpacks/install/manifest.json`. The page shows installing, waiting for
sign-in, skipped login, search connection, network check, and completion.
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
| `DONE:` | Verify the saved result and give the account/network summary below. Continue any search the user already requested. |
| `NEEDS YOU:` | Show the single action needed. During browser login the script waits and continues automatically. If the script has exited, resume it after the action. |
| `ASK:` | Answer from the user's existing request where possible. Optional Gmail tools are unnecessary for initial setup; rerun with `--no-tools`. |
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
- **Personal network has 0 people:** name the actual signed-in email and ask:
  "Your personal network for <email> is empty. Would you like to use another
  account or connect your contacts?" An account switch uses the installed
  `auth.py login --force-account` primitive, then reruns onboarding. Don't guess
  another identity or silently switch to a shared network.
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

## Finish with verified readiness

`DONE` for local installation alone does not mean a network is ready. Use the
script's successful account and network checks. For example, with real values:

> Powerpacks is ready. You're signed in as <email>.
> <network name> has <N> people, and search is working.
> Try: "Find backend engineers in my network."

If something needs the user, state that instead of "ready". An empty personal
network is not successful personal-network onboarding. Never invent a count
or call every contact searchable just because the network list reports it.

If the original request included a search, read the installed `search/SKILL.md`
and do it now, preserving the requested network. Read newly installed skills
directly in this session; no restart or new user command is needed.

Existing hosted networks can be usable immediately. A fresh install may need
downloads and a human login, so do not promise a one-minute completion time.
LinkedIn imports and local index builds are separate, longer work: use `setup`
only when the user requested a local LinkedIn network. Gmail and messages use
`import-gmail` and `import-messages`; processing uses `deep-context`. Explain
any actual file or permission needed in ordinary words. Don't run all imports
as a prerequisite to using an already-searchable network.
