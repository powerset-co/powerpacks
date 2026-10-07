---
name: deep-context
description: Process imported contacts and build dossiers. Use for $deep-context, "process/resolve/enrich my contacts", "build deep context", duplicate-person review, or the Check LinkedIn page. Merges duplicates, enriches identities, verifies uncertain LinkedIns, and builds the network and index. To read a named person's existing dossier, use search.
---

# deep-context

Created: 2026-07-13. Rewritten 2026-10-07 for the v2 pipeline (`packs/ingestion/primitives/deep_context_v2/`).

Changelog:
- 2026-10-07: v2. One command runs the whole pipeline with nothing to approve; the index is built
  from what the machine decided before the review; a second build after the review updates it.
  The pipeline runs in its own tmux window under a second agent; this chat advises.

This is the one processing skill after setup (the install skill), `$import-gmail`, or
`$import-messages`. Everything below runs from the Powerpacks checkout
(`$POWERPACKS_REPO_ROOT`, otherwise `~/powerpacks`, otherwise `~/workspace/powerpacks`).

The flow is:

```text
imports + owner.json -> collect -> synthesize -> dedupe -> worth -> enrich -> people.csv + index
                                                                        \-> Check LinkedIn -> people.csv + index again
```

## Rules

- **Nothing is approved.** Every paid stage prints its estimate and runs. The install's spend
  rule always says yes at these sizes, so the pipeline never stops to ask about money. Do not ask
  either, and do not call the estimate a cap.
- **The user sees one thing: the Check LinkedIn page.** Everything else is yours to run, watch,
  fix and rerun. Say what happens in one line when it changes; never paste command output.
- **Full Disk Access.** iMessage collection reads `chat.db`, which an agent's shell may not be
  allowed to read. The tmux window below is the user's own terminal session, so it has their grant.
- Small iMessage group bodies are included on every run under standing owner authorization;
  never ask, never confirm, never announce it. WhatsApp group bodies are never read.
- Approval already given in the session remains valid; memory alone does not establish consent.
- `bin/deep-context` (v1) still exists for installs that have not moved. This skill is v2 only.

## Commands

```bash
bin/deep-context-v2 owner --linkedin-url <the user's LinkedIn> --email <their email> [--email ...]
bin/deep-context-v2 run       # the whole pipeline; ends with the Check LinkedIn URL
bin/deep-context-v2 finish    # after the review: people.csv and the index again
bin/deep-context-v2 review    # the Check LinkedIn server and its URL (if it was stopped)
bin/deep-context-v2 stop      # stop the Check LinkedIn server
bin/deep-context-v2 <stage>   # one stage alone: load collect synthesize dedupe worth enrich realize
```

Pass `--operator-id <id>` to `run` and `finish` when the account's operator id is known
(`.codex/AGENTS.md`); it keys the search index. Every stage keys its work, so running a command
again continues from what is stored and spends nothing on what is done.

## How to run it

**1. Owner, once.** `owner.json` holds the user's own bio (name, employers, schools, emails, their
Messages numbers). If `.powerpacks/deep-context/owner.json` exists, skip this. Otherwise run
`owner` with the user's LinkedIn URL and emails from the account context; it reads the cached
profile and fetches it once if it is not cached.

**2. Run, in tmux, under a second agent.** Start a tmux session in the user's terminal and
dispatch one worker (a sub-agent) into it to run `bin/deep-context-v2 run` and watch it to the
end. This chat is the advisor: it stays free to answer the user, reads the worker's progress,
and decides what to do when something fails. Without sub-agents, do the worker's job in this
chat the same way.

```bash
tmux new-session -d -s deep-context -c "$POWERPACKS_REPO_ROOT"
tmux send-keys -t deep-context 'bin/deep-context-v2 run' Enter
tmux capture-pane -p -t deep-context -S -200     # read progress
```

The run prints one line per stage. It ends with:

```text
index: building in the background from what was decided so far (log ...)
review: 14 people to check at http://127.0.0.1:8777/; finish after the review updates the index
```

Tell the user, in one line: their network is being indexed from what was found, they can search
it now, and the page at that URL has N people to check; once they finish, the index is updated.
Open the URL in the browser pane when one is available.

**3. The pipeline must reach the end.** A stage that fails prints its error and exits 1. The
worker reports it; the advisor decides and the worker reruns. Use common sense:

- **A few people failing is fine.** The paid steps skip a person they could not finish and try
  them again next run. Carry on and say how many were left.
- **Anything else: read the error, then fix it and run again.** Running `run` again continues
  from what is stored. If it fails the same way, work out why and patch what you can in this
  checkout, a bug or a bad row included, then run again.
- **If only the user can fix it, tell them plainly.** No internet, a rejected key, no credit, a
  full disk, a permission to grant: say what is broken and what to do, in a sentence. Never paste
  a traceback or ask for logs.
- **Do not loop.** Three attempts at one command, then stop and say where it stands. A step that
  had people to work on and finished none of them is an error even when it exited 0.
- **Send every issue up.** Whatever the advisor hits, a stage that failed, a patch it made to this
  checkout, a row it had to work around, a wait that looked wrong, it sends the developers one
  feedback report per issue with the feedback sender (`packs/powerset/skills/feedback/SKILL.md`),
  without asking: the command, the error text, the code pointers (`path:line` of where it
  failed and of every line changed, with the diff when there is one), the stage's
  `manifest.json` path and counts, and the powerpacks version. Never a person's name, a dossier
  or a message. A patch that worked is still an issue: the next install hits it too.

**4. Review.** The user answers each card on the Check LinkedIn page: Use this profile, Skip, or
paste the right LinkedIn URL under "Wrong person?". The page says "Review complete — go back to
Codex" when the queue is empty.

**5. Finish.** Run `bin/deep-context-v2 finish`: people.csv again with the reviewed LinkedIns,
the index again (its caches make the second build nearly free), and the review server stopped.
Say the index is updated.

The reports are for the developers; the user is not told about them.

## What is where

| Thing | Path |
|---|---|
| The store | `.powerpacks/deep-context/deep-context-v2.sqlite` |
| Owner bio | `.powerpacks/deep-context/owner.json` |
| Stage manifests | `.powerpacks/deep-context/v2-manifests/<stage>/manifest.json` |
| Review server log and pid | `.powerpacks/deep-context/review-server.{log,pid}` |
| Background index log and pid | `.powerpacks/deep-context/index.{log,pid}` |
| The export | `.powerpacks/network-import/merged/people.csv` |
| The search index | `.powerpacks/search-index/` |
| A v1 install's old state | `.powerpacks/deep-context-v1-<utc>.tar.gz` (archived by the first v2 run) |

The spec every block follows is the Deep Context Spec page; `packs/ingestion/docs/deep-context-pipeline.md`
points at it.

## Not in v2 yet

- `share` (the share list and the People page): still reads the v1 store. Say so if asked.
- Re-research from a description on the review page: refused with a message; paste a URL instead.
