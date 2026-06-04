# Codex heartbeat worker

Powerpacks can run a local Codex heartbeat as either:

- a Docker-managed worker, useful for Linux servers and reproducible containers;
- a macOS `launchd` LaunchAgent, useful when you want to reuse the Codex OAuth
  login from your normal shell and do not want Docker.

Both paths use the same config-gated runner. The important cost-control behavior
is that each wakeup first reads local JSON config/state and exits without calling
Codex until the configured schedule says processing is due.

## Heartbeat config

Create an editable local config:

```bash
scripts/codex-heartbeat-runner.py \
  --config .powerpacks/codex-heartbeat.json \
  --init-config
```

Edit `.powerpacks/codex-heartbeat.json`:

```json
{
  "enabled": true,
  "interval_seconds": 3600,
  "retry_interval_seconds": 900,
  "max_tasks_per_tick": 1,
  "run_on_start": true,
  "prompt": "Powerpacks scheduled heartbeat: inspect the local Powerpacks checkout and report one terse line with current status. Do not run spend-bearing searches, uploads, or external workflows unless the prompt explicitly asks for them.",
  "tasks": [
    {
      "id": "powerpacks-status",
      "interval_seconds": 3600,
      "session": {
        "mode": "new"
      },
      "prompt": "Powerpacks status loop: inspect the local checkout and report one terse line with health/status."
    },
    {
      "id": "example-followup-queue",
      "enabled": false,
      "interval_seconds": 900,
      "retry_interval_seconds": 300,
      "session": {
        "mode": "resume-id",
        "id": "paste-codex-session-id-here"
      },
      "prompt": "Example disabled loop task. Replace this with a concrete recurring Codex task prompt, then set enabled=true."
    }
  ]
}
```

The wrapper wakes every `HEARTBEAT_POLL_SECONDS` seconds, but this is only a local
due check across all configured tasks. Codex is invoked only for tasks where:

- `enabled` is `true`, and
- no previous successful run exists and `run_on_start` is `true`, or
- `interval_seconds` has elapsed since the last successful run.

Set either top-level `enabled` or a task-level `enabled` to `false` for a
completely free no-op loop. Use `tasks` when you want a Claude-loop-style list of
recurring Codex tasks. Each task gets independent state under the shared state
file, so one failing task's `retry_interval_seconds` backoff does not block other
due tasks. `max_tasks_per_tick` can cap how many due tasks are drained in one
wakeup.
If a due Codex run fails, `retry_interval_seconds` throttles retries so a broken
auth/session does not spend on every poll.

All tasks in one config share the same worker environment: one Powerpacks
checkout, one Codex login, one install/cache setup, and sequential `codex exec`
runs. If you need separate repositories, credentials, or runtime environments,
run separate Docker containers or launchd agents with different config/state
paths.

## Reusing login and Codex sessions

There are two different kinds of reuse:

1. **Login/auth reuse**: Docker snapshot mode copies the host Codex login into the
   persistent `powerpacks-codex-home` Docker volume, and launchd runs directly
   with your normal `~/.codex` OAuth login. The worker checks for existing auth
   before running `codex login --with-api-key`, so it should not log in again on
   every due job.
2. **Conversation/session reuse**: by default each task runs a fresh
   `codex exec`. For tasks that should keep a Codex conversation alive across
   due runs, set a task-level `session`:

```json
{
  "id": "followup-queue",
  "interval_seconds": 900,
  "session": {
    "mode": "resume-id",
    "id": "7f9f9a2e-1b3c-4c7a-9b0e-example-id"
  },
  "prompt": "Continue processing the next due item from the followup queue."
}
```

Supported session modes:

- `new` / `fresh` / `none`: run `codex exec <prompt>`; this is the default.
- `resume-id`: run `codex exec resume <id> <prompt>`; safest for many tasks
  because each task can target a specific Codex session.
- `resume-last`: run `codex exec resume --last <prompt>`; convenient for a
  single-task worker, but risky for many tasks in the same checkout because they
  may resume whichever Codex session was most recent.

The scheduler does not auto-discover/store new session ids yet; paste the session
id into config for tasks where you want conversation reuse.

You can inspect due status without calling Codex:

```bash
scripts/codex-heartbeat-runner.py \
  --config .powerpacks/codex-heartbeat.json \
  --dry-run
```

## macOS launchd worker

Install a LaunchAgent that uses your regular shell Codex OAuth login:

```bash
scripts/install-codex-heartbeat-launchd.sh install
```

The installer creates `.powerpacks/codex-heartbeat.json` if needed, writes a plist
under `~/Library/LaunchAgents/`, and logs to `~/Library/Logs/Powerpacks/`.

Useful commands:

```bash
scripts/install-codex-heartbeat-launchd.sh status
scripts/install-codex-heartbeat-launchd.sh logs
scripts/install-codex-heartbeat-launchd.sh restart
scripts/install-codex-heartbeat-launchd.sh uninstall
```

Because the launchd worker runs directly on your Mac, it does not copy or mount
Codex credentials. It uses the normal `~/.codex` OAuth login that `codex login`
created for your user.

## Docker worker

Powerpacks can run a long-lived Codex heartbeat worker under Docker:

```bash
scripts/run-codex-heartbeat-docker.sh start
```

The wrapper builds `adapters/codex/docker/Dockerfile`, mounts this checkout
read-only at `/workspace/powerpacks`, and runs the same local config/state due
check loop. Only when a run is due does it copy/auth/install and invoke Codex.
Docker runs the container with `--restart unless-stopped`.

## Sharing the regular shell Codex login

Default mode is **snapshot** mode, which avoids writing to your host Codex config:

- `${CODEX_HOME:-$HOME/.codex}` from the host mounts read-only at `/host-codex`.
- `/root/.codex` inside the container is a separate writable Docker volume.
- On container startup, `scripts/codex-heartbeat.sh` copies the host login/config
  snapshot into `/root/.codex`.

That means the worker can use the login created by `codex login` in your regular
shell, while Codex logs/session files written by the container stay in the Docker
volume instead of mutating your host `~/.codex`.

Docker snapshot mode can start even if no host `~/.codex/auth.json` and no
`OPENAI_API_KEY` are available. That keeps no-op polls free: disabled/not-due
checks only read local config/state and do not need auth. A due run still needs a
usable Codex login snapshot or `OPENAI_API_KEY`; otherwise the failure is recorded
in heartbeat state and retried only after `retry_interval_seconds`.

If you log in or refresh Codex credentials in your regular shell, restart the
worker so it copies the new snapshot:

```bash
scripts/run-codex-heartbeat-docker.sh restart
```

You can also skip host login sharing and pass an API key:

```bash
OPENAI_API_KEY=... scripts/run-codex-heartbeat-docker.sh start
```

API-key mode passes `OPENAI_API_KEY` as a Docker container environment variable,
which is visible to local users/processes that can run `docker inspect`. Prefer
snapshot login sharing for normal local use. When no `/root/.codex/auth.json`
exists, the heartbeat also runs `codex login --with-api-key` inside the
container so Codex versions that require a login file can use the key; that means
the key-derived container auth persists in the `powerpacks-codex-home` Docker
volume until you remove that volume.

### Direct mount mode

If you intentionally want the container to use your host Codex home directly:

```bash
POWERPACKS_CODEX_HOME_MODE=direct scripts/run-codex-heartbeat-docker.sh start
```

Direct mode mounts `${CODEX_HOME:-$HOME/.codex}` at `/root/.codex` read-write.
Only use it if you accept that the container may update or corrupt the same Codex
config/session files used by your regular shell.

## One-shot validation

Run one heartbeat and exit:

```bash
scripts/run-codex-heartbeat-docker.sh once
```

Override the prompt or local poll interval:

```bash
CODEX_HEARTBEAT_PROMPT='Say one line with Powerpacks status only.' \
HEARTBEAT_POLL_SECONDS=60 \
scripts/run-codex-heartbeat-docker.sh start
```

## Startup install behavior

When a run is due, the worker runs the Codex Powerpacks installer before invoking
Codex so a fresh container has the current skills. That installer runs `bin/setup-python`;
the wrapper persists its environment/cache in a Docker volume named
`powerpacks-codex-cache` by default. The heartbeat sets
`POWERPACKS_SKIP_AGENT_BOOTSTRAP=1` during install so the container does not try
to write repo-local `.codex/AGENTS.md` or `.powerpacks/memory/*` files into the
read-only checkout. If the container is already prepared and you want the
heartbeat to start without dependency sync/downloads, pass:

```bash
POWERPACKS_HEARTBEAT_SKIP_INSTALL=1 scripts/run-codex-heartbeat-docker.sh start
```

For a one-time install that skips Python dependency sync but still refreshes the
Codex skills, pass:

```bash
POWERPACKS_SKIP_UV_SYNC=1 scripts/run-codex-heartbeat-docker.sh once
```

Stop or inspect the worker:

```bash
scripts/run-codex-heartbeat-docker.sh status
scripts/run-codex-heartbeat-docker.sh logs
scripts/run-codex-heartbeat-docker.sh stop
```
