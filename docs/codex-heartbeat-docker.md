# Codex heartbeat worker in Docker

Powerpacks can run a long-lived Codex heartbeat worker under Docker:

```bash
scripts/run-codex-heartbeat-docker.sh start
```

The wrapper builds `adapters/codex/docker/Dockerfile`, mounts this checkout
read-only at `/workspace/powerpacks`, installs the Powerpacks Codex skills from
that mounted checkout into the container Codex home, then loops on `codex exec`
with a lightweight heartbeat prompt. Docker runs the container with
`--restart unless-stopped`.

## Sharing the regular shell Codex login

Default mode is **snapshot** mode, which avoids writing to your host Codex config:

- `${CODEX_HOME:-$HOME/.codex}` from the host mounts read-only at `/host-codex`.
- `/root/.codex` inside the container is a separate writable Docker volume.
- On container startup, `scripts/codex-heartbeat.sh` copies the host login/config
  snapshot into `/root/.codex`.

That means the worker can use the login created by `codex login` in your regular
shell, while Codex logs/session files written by the container stay in the Docker
volume instead of mutating your host `~/.codex`.

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

Override the prompt or interval:

```bash
CODEX_HEARTBEAT_PROMPT='Say one line with Powerpacks status only.' \
HEARTBEAT_INTERVAL_SECONDS=600 \
scripts/run-codex-heartbeat-docker.sh start
```

## Startup install behavior

By default the worker runs the Codex Powerpacks installer before the heartbeat so
a fresh container has the current skills. That installer runs `bin/setup-python`;
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
