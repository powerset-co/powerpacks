# Advisor and tmux worker

Read this before delegating a workflow. The calling skill supplies the command,
checkout, session name, artifacts, permissions and completion criteria. Its
workflow-specific requirements still apply. Quick lookups can run directly.

## One advisor, one worker

The original chat is the advisor and the user's one voice. Own visible tasks,
scope/reviews, steering, recovery decisions and technical feedback. Keep tasks
current from saved artifacts and the worker's output; cached/skipped work counts,
but a live process or an open page is not completion.

Dispatch one worker with the skill path, canonical checkout, exact request,
selected accounts/network, existing approvals/limits, artifacts and session name.
Tell it to read this file too. The worker owns command execution, targeted repairs
and evidence for the advisor. It does not spawn another worker, create a second
checklist, address the user, or send duplicate feedback. Do not turn an advisor
precaution into a claimed user instruction, extra trial run or approval gate.

Use the calling skill's existing launcher when provided; otherwise use a native
sub-agent operating commands in tmux. Without sub-agents, do that work here.
Do not install another agent harness silently. tmux does not grant OS permissions.

## Run and watch

Inspect an existing session's pane, directory and live command before reusing it.
Observe ongoing work; never send a duplicate command into a busy pane. Resume the
same workflow/artifacts after interruption. Leave an unrelated session alone and
choose a different name. The worker alone starts commands; the advisor steers it.

```bash
tmux new-session -d -s '<session>' -c '<checkout>'   # only when absent
tmux capture-pane -p -t '<session>' -S -200
tmux send-keys -t '<session>' -l '<command from the calling skill>'
tmux send-keys -t '<session>' Enter                 # only when idle
```

Read progress about every 30 seconds with bounded waits, and relay user input
immediately. Preserve requested review pauses. A login/permission wait does not
end supervision: observe it and resume automatically. MCP/web calls can use the
worker's tools; tmux holds CLI work. If the advisor ends its turn, tmux cannot
wake it; promise follow-ups only with a supported, authorized wakeup mechanism.

## Repair and finish

Read the actual error and saved state, make the smallest necessary repair, then
rerun the documented continuation using completed outputs and paid caches. Use
the relevant setup/doctor skill for unclear setup failures. Preserve source
patches before an update can replace them. No unrelated refactors, resets,
relaxed search constraints, replacement pipelines or changed permissions.
Respect the calling skill's consent, spend and review rules; never ask twice.
After three attempts at the same failure, stop that command and explain the
remaining cause and specific action needed. Involve the user for decisions or
things only they can do, not commands the worker can run.

Verify the requested outcome from real artifacts before marking it complete.
Then exit the worker and close only its owned idle tmux session. Leave requested
viewers available. On user stop, stop starting work, interrupt the owned command,
check and cancel its specific detached job, and verify the stop. Preserve saved
work, report jobs that could not be stopped, and leave unrelated sessions alone.

## Technical feedback

The advisor sends one sanitized report per distinct failure, misleading progress
state or repair, including successful repairs. The worker returns evidence only.
Use the existing sender, with the calling skill's category (`install`, `search`
or `deep-context`), without another confirmation:

```bash
uv run --project . python packs/powerset/primitives/send_feedback/send_feedback.py \
  --category '<category>' --comment '<failure, repair, verified outcome>' \
  --metadata '<JSON: sanitized command/error, status/counts, code pointers, diff, version>'
```

Remove credentials, account/person/network identifiers, queries/JDs, message or
dossier prose and private paths from commands, errors and patches. Use placeholders
and aggregate counts; do not attach raw logs, manifests or databases. Contentful
user feedback follows its own skill's authorization rules. Check for `submitted`
before claiming delivery. If reporting fails, keep the sanitized report locally
for a later retry; do not interrupt the workflow for feedback login, loop on the
endpoint, or report the reporting failure back to itself.
