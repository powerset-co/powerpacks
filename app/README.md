# Powerpacks Console

Local Vite app for browsing Powerpacks artifacts from `.powerpacks/`.

Install or repair the local app from the repo root:

```bash
./install.sh app
```

For a clean reinstall:

```bash
./install.sh app --clean
```

Direct npm usage is supported:

```bash
cd app
npm install
npm run dev -- --port 5177
# for LAN/container exposure, pass an explicit host:
npm run dev:lan -- --port 5177
```

Start the console in the background from the repo root:

```bash
scripts/run-powerpacks-console.sh start
```

For a Docker-managed persistent stack, use Docker Compose:

```bash
scripts/run-powerpacks-compose.sh start
# include the Codex loop scheduler too:
scripts/run-powerpacks-compose.sh start --with-loops
```

The compose stack uses `restart: unless-stopped`, so services restart when the
Docker daemon restarts. On Docker Desktop, make sure Docker Desktop itself starts
on login if you want the stack back after a machine reboot. The loops profile
uses an existing host Codex OAuth home as a read-only auth snapshot when present;
if it is missing, Compose can still start for disabled/not-due no-op checks, but
due Codex runs need host auth before they can succeed.

For the console service, Compose bind-mounts the Powerpacks checkout read-write
so Vite can write its normal temporary config/cache files and hot reload directly
from the host files. `app/node_modules` stays isolated in a named Docker volume.

On macOS, install the console as a persistent launchd LaunchAgent:

```bash
scripts/install-powerpacks-console-launchd.sh install
# or install the console + Codex loop scheduler together:
scripts/install-powerpacks-stack-launchd.sh install --with-loops
```

This keeps the existing Vite console running as the local Powerpacks control
plane across user logins/restarts. launchd agents use `RunAtLoad` + `KeepAlive`,
so they start after login and are relaunched if they exit. It is the recommended
UI/control plane for Codex loop tasks; the scheduler workers still do the actual
due checks and Codex runs.
The Vite dev server keeps hot reload for app source files, while ignoring runtime
state directories such as `.powerpacks/`, `.codex/`, `.venv/`, `node_modules/`,
and `dist/` so generated artifacts do not churn the dev server.

Keep this split when adding loop management or remote pub/sub features:

- Console/Vite app: local UI and narrow local API control plane.
- Scheduler workers: Docker/launchd processes that perform due checks and Codex
  execution.
- Future WSS/pub-sub client: should connect from the local console/control plane
  and enqueue/update allowlisted network-search query tasks through narrow APIs,
  not expose arbitrary command execution. Sanitize/validate remote payloads before
  they become local loop tasks.

To add easy local hostnames for the console:

```bash
scripts/install-powerpacks-console-hostname.sh print
scripts/install-powerpacks-console-hostname.sh install
```

After installing hostnames, open `http://powerpacks.test:5177` or
`http://powerpacks:5177`. `/etc/hosts` cannot encode ports, so a no-port URL like
`http://powerpacks` would require a separate port-80 reverse proxy or binding the
console to port 80; the helper intentionally does not install that privileged
proxy.

From a Codex session outside the repo, use the installed bundle:

```bash
"$HOME/.codex/powerpacks/scripts/run-powerpacks-console.sh" start
```

Open the URL printed by the script, usually `http://localhost:5177/`.

The console is read-only and currently supports network search/rerank artifacts.
