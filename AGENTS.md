# Using Powerpacks

Powerpacks helps the user search their network, import contacts, and build
context about people. This file governs using the installed product. For an
explicit request to change Powerpacks code, read `docs/development.md`.

## Carry out the user's request

- Own the requested workflow through its verified outcome. Run commands you can
  run; don't hand the user a checklist or ask them to invoke another skill.
- Interpret follow-ups in the context of the ongoing task. Answer explanatory
  questions directly without making unsolicited code changes; don't abandon an
  already-requested workflow merely because a follow-up is phrased as a question.
- Handle routine setup, retries, and free, reversible repairs within the user's
  request. Ask only for a missing decision, an action requiring the user, spend
  beyond the workflow's automatic budget, destructive changes, or expanded access.
  Don't ask twice.
- Follow the relevant skill's scope. Installation includes its default local
  contact imports; inform the user and honor changes or skips in chat. Message
  processing uses Deep Context; onboarding follows the install skill's cost limit.
  Uploads need authorization.
- Preserve existing configuration, unrelated skills, local data, checkpoints,
  and paid artifacts. Never delete data or replace a named account with another
  account that happens to work.
- Read current output before describing behavior. Report installation, sign-in,
  and usable search separately. Never invent readiness, counts, or progress.
- Keep chat short and understandable. Use the browser page for live progress;
  explain only the result, a necessary choice, or the action requiring the user.

## Find the installation

A fresh install needs no selected project or manual clone. Bootstrap downloads
Powerpacks into `~/powerpacks`, reusing an existing supported installation when
available. Codex skills live in `~/.agents/skills`; Claude Code skills live in
`~/.claude/skills`; Pi skills live in `~/.pi/agent/skills`.

Installed skills identify the checkout they came from. Run their commands from
that checkout, regardless of the chat's directory. Configuration is its `.env`;
workflow data is its `.powerpacks/`. A copied skill bundle is not a data root.
Do not write Powerpacks state into an unrelated project or the skills folder.

If `.codex/AGENTS.md` exists in the checkout, read it for remembered account and
network context. It is a snapshot, not proof of current access. Refresh it with
`bin/agent-bootstrap` when missing/stale, after account or network changes, or
when the user asks. Never print secret values.

## Choose the skill

Load the matching `SKILL.md` and follow it. The skill owns its sequence,
permissions, recovery, and verification. Don't ask the user to choose a skill
when their intent is clear, or inspect product source on the happy path.
Paths below are relative to the installed checkout.

| User intent | Skill |
| --- | --- |
| Install or set up Powerpacks (the pasted URL, `$setup`, first run, LinkedIn import) | `packs/powerset/skills/install-powerpacks/SKILL.md` |
| Login, account, network selection, credentials, agent connection | `packs/powerset/skills/powerset/SKILL.md` |
| Health check or unclear installation/setup failure | `packs/powerset/skills/powerpacks-doctor/SKILL.md` |
| Update Powerpacks | `packs/powerset/skills/update-powerpacks/SKILL.md` |
| Repair installation or data paths | `packs/powerset/skills/fix-powerpacks/SKILL.md` |
| People search, job description, shortlist, named person or dossier lookup | `packs/search/skills/search/SKILL.md` |
| Company search | `packs/search/skills/search-company/SKILL.md` |
| Local relational or aggregate search | `packs/search/skills/search-sql/SKILL.md` |
| Browse personal or network contacts | `packs/contacts/skills/search-contacts/SKILL.md` |
| Import Gmail contacts | `packs/ingestion/skills/import-gmail/SKILL.md` |
| Gmail archive/OAuth setup | `packs/ingestion/skills/msgvault/SKILL.md` |
| Import iMessage or WhatsApp contacts | `packs/ingestion/skills/import-messages/SKILL.md` |
| Import Twitter/X contacts | `packs/ingestion/skills/import-twitter/SKILL.md` |
| Process contacts, build dossiers, duplicates, review, sharing | `packs/ingestion/skills/deep-context/SKILL.md` |
| Build the local search index | `packs/indexing/skills/build-local-search-index/SKILL.md` |
| Raw conversation archive | `packs/ingestion/skills/logbook/SKILL.md` |
| Reset derived pipeline state | `packs/ingestion/skills/clean-slate/SKILL.md` |
| Sales Navigator leads | `packs/sales-nav/skills/sales-nav-search/SKILL.md` |
| Apollo outbound | `packs/apollo/skills/build-outbound/SKILL.md` |
| Send requested product feedback | `packs/powerset/skills/feedback/SKILL.md` |
| Instructions to or from a teammate's agent ("check steer", "steer Jake's agent") | `packs/powerset/skills/steer/SKILL.md` |

## Setup and recovery

Don't run a broad health check before every task. Start with the requested
primitive; if it fails with an unclear setup problem, load `powerpacks-doctor`.
The doctor skill owns diagnosis and repair. Missing Python dependencies are a
setup problem, not a reason to change product code.

During installation, open the printed progress URL beside chat and keep that
pane on the status page. Sign-in opens in the system's default browser. The
user completes login or OS permission dialogs; the agent resumes the workflow.
If a targeted repair leaves the same failure, explain the remaining problem
and the specific action needed instead of retrying indefinitely.
