---
name: codex-loop
description: Create and manage local Codex loop/heartbeat tasks backed by Powerpacks Docker or macOS launchd scheduling. Use for $codex-loop, recurring Codex jobs, heartbeat loops, scheduled prompts, and multi-task Codex automation.
---

# codex-loop

Use this skill when the user wants to create, inspect, update, disable, or install
local Codex loop tasks. A Codex loop task is a JSON config entry consumed by
`scripts/codex-heartbeat-runner.py`; Docker and macOS launchd wrappers wake up,
read local config/state, and only call Codex when a task is due.

This is the Powerpacks replacement for a Claude-style loop when the user wants
Codex to process many recurring tasks that Codex cannot schedule by itself.

## Safety and cost model

- No-op polls must stay free: disabled/not-due tasks only read local JSON
  config/state and must not invoke Codex, copy auth, run `codex login`, or run
  Powerpacks install.
- Prefer explicit intervals and retry backoff. Every task should have
  `interval_seconds` and `retry_interval_seconds`.
- For many tasks, prefer `max_tasks_per_tick` so one wakeup drains a bounded
  number of due tasks.
- Use `session.mode=resume-id` for conversation reuse across due runs. Avoid
  `resume-last` when multiple tasks share the same checkout unless the user
  explicitly accepts that it may resume the wrong latest Codex session.
- Do not store secrets in the loop config. Use Codex OAuth (`~/.codex`) or the
  Docker auth-volume behavior documented in Powerpacks.

## Canonical repo

Run commands from a canonical non-`.codex` Powerpacks checkout. Prefer, in order:

1. `$POWERPACKS_REPO_ROOT` if it points to a Powerpacks repo;
2. current working directory if it is a Powerpacks repo and not under `.codex`;
3. `~/powerpacks`;
4. `~/workspace/powerpacks`.

```bash
resolve_powerpacks_root() {
  for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" != *"/.codex/"* ]] || continue
    if [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
repo="$(resolve_powerpacks_root)" || {
  echo "No canonical non-.codex Powerpacks repo found. Install/copy Powerpacks to ~/powerpacks first." >&2
  exit 1
}
cd "$repo"
```

## Command routing

| User asks | Do this |
| --- | --- |
| `$codex-loop help` or asks what loops are supported | Show the help text below. |
| `$codex-loop init` | Create `.powerpacks/codex-heartbeat.json` with `scripts/codex-heartbeat-runner.py --config .powerpacks/codex-heartbeat.json --init-config`. |
| `$codex-loop list` | Read `.powerpacks/codex-heartbeat.json` and summarize task ids, enabled state, intervals, session mode, and prompt first line. |
| `$codex-loop add <id>` / asks to create a loop | Edit `.powerpacks/codex-heartbeat.json` and append or update one task. Confirm the task id, interval, retry interval, prompt, and session mode. |
| `$codex-loop disable <id>` | Set that task's `enabled` to `false`; do not delete history/state. |
| `$codex-loop enable <id>` | Set that task's `enabled` to `true`. |
| `$codex-loop due` | Run `scripts/codex-heartbeat-runner.py --config .powerpacks/codex-heartbeat.json --dry-run`. |
| `$codex-loop install docker` | Run or print `scripts/run-codex-heartbeat-docker.sh start`. |
| `$codex-loop install compose` | Run or print `scripts/run-powerpacks-compose.sh start --with-loops` for the Docker Compose console + scheduler stack. |
| `$codex-loop install launchd` | On macOS, run or print `scripts/install-codex-heartbeat-launchd.sh install`. |
| `$codex-loop install stack launchd` | On macOS, run or print `scripts/install-powerpacks-stack-launchd.sh install --with-loops` for the console + scheduler stack. |
| `$codex-loop console install` | On macOS, run or print `scripts/install-powerpacks-console-launchd.sh install` so the existing Vite console stays persistent. |
| `$codex-loop console hostname` | Explain that `/etc/hosts` can map `powerpacks.test`/`powerpacks` to localhost but cannot remove the port; run or print `scripts/install-powerpacks-console-hostname.sh print`. |

If the user asks in natural language, infer the command and proceed. Ask one
clarifying question only when the interval or prompt is genuinely missing.

## Help text

When asked for help, respond with:

```text
$codex-loop init                 create .powerpacks/codex-heartbeat.json
$codex-loop list                 show configured loop tasks
$codex-loop add <id>             add/update a loop task prompt and schedule
$codex-loop enable <id>          enable a loop task
$codex-loop disable <id>         disable a loop task without deleting it
$codex-loop due                  dry-run due checks without Codex spend
$codex-loop install docker       start the Docker worker
$codex-loop install compose      start Docker Compose console + loop worker
$codex-loop install launchd      install the macOS launchd worker
$codex-loop install stack launchd install macOS launchd console + loop worker
$codex-loop console install      keep the Powerpacks Console running with launchd
$codex-loop console hostname     install/print local console hostnames
```

## Config format

Create or update `.powerpacks/codex-heartbeat.json`:

```json
{
  "enabled": true,
  "interval_seconds": 3600,
  "retry_interval_seconds": 900,
  "max_tasks_per_tick": 1,
  "tasks": [
    {
      "id": "followup-queue",
      "enabled": true,
      "interval_seconds": 900,
      "retry_interval_seconds": 300,
      "session": {
        "mode": "resume-id",
        "id": "paste-codex-session-id-here"
      },
      "prompt": "Continue processing the next due item from the followup queue."
    }
  ]
}
```

Task fields:

- `id`: stable unique id. Use lowercase kebab-case.
- `enabled`: false means local no-op, no Codex call.
- `interval_seconds`: minimum seconds between successful runs.
- `retry_interval_seconds`: minimum seconds before retrying after failure.
- `session.mode`:
  - `new`: fresh `codex exec` each due run.
  - `resume-id`: `codex exec resume <id> <prompt>`; preferred for many tasks.
  - `resume-last`: `codex exec resume --last <prompt>`; only for single-task workers or explicit user acceptance.
- `prompt`: the exact prompt Codex should receive when the task is due.

## Adding a task

When adding a task:

1. Ensure config exists:
   ```bash
   scripts/codex-heartbeat-runner.py --config .powerpacks/codex-heartbeat.json --init-config
   ```
2. Read the JSON. Preserve existing tasks and top-level defaults.
3. Append or update the task with a stable `id`.
4. Prefer `enabled: false` if the user is still drafting the prompt.
5. Run a no-spend due check:
   ```bash
   scripts/codex-heartbeat-runner.py --config .powerpacks/codex-heartbeat.json --dry-run
   ```
6. Summarize exactly what changed and how to start the worker.

## Installing the worker

Docker:

```bash
scripts/run-codex-heartbeat-docker.sh start
```

Docker Compose stack (console by default, add loops with `--with-loops`):

```bash
scripts/run-powerpacks-compose.sh start
scripts/run-powerpacks-compose.sh start --with-loops
```

Compose uses `restart: unless-stopped`; it restarts services when Docker restarts.
On Docker Desktop, the user must enable Docker Desktop startup after login/reboot.
The loops profile uses the host Codex OAuth home as a read-only auth snapshot when
it exists. If it is missing, Compose uses an empty snapshot so disabled/not-due
no-op checks can still start, but due Codex runs need host auth before they can
succeed.

macOS launchd:

```bash
scripts/install-codex-heartbeat-launchd.sh install
scripts/install-powerpacks-stack-launchd.sh install --with-loops
```

launchd uses `RunAtLoad` + `KeepAlive`, so agents start after user login and are
restarted if they exit.

Use launchd when the user wants to reuse their local Codex OAuth login directly.
Use Docker when they want a containerized worker with persistent Docker volumes.

## Persistent console control plane

Powerpacks already has a local Vite console. Prefer reusing it as the UI/control
plane rather than creating a second web daemon:

```bash
scripts/install-powerpacks-console-launchd.sh install
```

For easier local names:

```bash
scripts/install-powerpacks-console-hostname.sh print
scripts/install-powerpacks-console-hostname.sh install
```

Be explicit with users: `/etc/hosts` maps names to IP addresses only. It cannot
store ports. After hostname install, use `http://powerpacks.test:5177` or
`http://powerpacks:5177`. A bare `http://powerpacks` URL requires a separate
port-80 reverse proxy or privileged port binding; do not install that implicitly.
