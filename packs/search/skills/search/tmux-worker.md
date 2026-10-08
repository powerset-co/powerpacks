# Advisor and tmux worker

Use for ordinary/deep people searches and multi-step company resolution. A quick
read-only person or company lookup can run directly. Keep the existing commands,
search artifacts, review requirements and spend/upload permissions.

## Own the outcome

This chat is the advisor and the user's one voice. Keep visible tasks for the
requested work: scope/intake, prepare/review, retrieve/rank, present results.
For deep search, keep retrieve/rank open across ponds until the existing stopping
rule is met. Cached steps count as complete; starting a command does not.

Dispatch one native sub-agent as the worker. Give it the canonical checkout,
request/JD, chosen network/backend, corrections, run directory, approved scope,
relevant skill references and tmux session name. Its brief: run and watch commands
in that session, report status/errors and artifact paths to the advisor, wait at
requested reviews, and follow the advisor's recovery decisions. No task tool,
user messages or feedback sends; the advisor owns those.

The worker alone sends commands or changes search artifacts. The advisor reads
the pane/artifacts, reviews queries and payloads, and sends steering to the worker.
Do not run a second prepare, pond, retry or resolver while the worker is running
one. If sub-agents are unavailable, do the worker's job in this chat using tmux.

## One session for the search

Use `powerpacks-search` in the canonical checkout. If it exists, inspect its pane
and current directory before reusing it. Reuse an interrupted session for this
search; if its command is still running, watch it rather than sending another.
Leave an unrelated session alone and choose a different name for this search.
Use the chosen session for every command, pond and recovery attempt.

```bash
tmux has-session -t powerpacks-search
tmux list-panes -t powerpacks-search -F '#{pane_current_path} #{pane_current_command}'
tmux capture-pane -p -t powerpacks-search -S -200
# If absent:
tmux new-session -d -s powerpacks-search -c '<canonical checkout>'
# The worker sends the actual skill command once the pane is idle:
tmux send-keys -t powerpacks-search -l '<command from the skill>'
tmux send-keys -t powerpacks-search Enter
```

Read the pane and current artifacts about every 30 seconds while work is running;
do not poll in a tight loop. The advisor checks actual statuses/counts and the
returned pending payload or bounded summary, keeping tasks and useful user updates
current. Preserve the user's scope and corrections when steering; explicit
step-by-step requests still pause. MCP/web reads can run in the worker's tools;
tmux holds the CLI commands.

If the user stops the search, tell the worker to stop starting commands/ponds and
interrupt only this search's running command in its owned pane. Preserve result
artifacts, checkpoints and paid caches. Verify it stopped before cleanup; never
interrupt another session or call an interrupted search complete.

At completion verify the returned result artifacts and requested viewer. Keep a
detached viewer available. Kill only the tmux session this workflow created or
already owns, after its pane is idle and no search command remains:

```bash
tmux kill-session -t powerpacks-search
```

## Recover and finish

Read the actual error and saved state before retrying. Use documented recovery
first: a transient read gets one retry; setup/auth problems use the matching
Powerset/doctor instructions. Resume the same run and reuse completed outputs and
paid caches. For deep search, follow the harness's saved status/next action rather
than initializing another search. Company resolution still starts fresh for a
new request; recovery within the current request preserves completed steps.

The advisor decides the smallest repair needed to complete the requested search;
the worker applies it and reruns the affected command. No unrelated refactor,
replacement retrieval or relaxed constraint. Respect existing spend/review gates;
cached work does not authorize another paid call. At most three attempts at one
failing command, then stop that command and state the remaining error and any
specific user action needed. Zero matches is a valid result; a failed retrieval
or ranking is not. Report each technical issue once through
[feedback.md](feedback.md), including an issue repaired successfully.
