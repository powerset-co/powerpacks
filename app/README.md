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
npm run dev -- --host 0.0.0.0 --port 5177
```

Start the console in the background from the repo root:

```bash
scripts/run-powerpacks-console.sh start
```

From a Codex session outside the repo, use the installed bundle:

```bash
"$HOME/.codex/powerpacks/scripts/run-powerpacks-console.sh" start
```

Open the URL printed by the script, usually `http://localhost:5177/`.

The console is read-only and currently supports network search/rerank artifacts.

## Run as a daemon (macOS launchd)

Install a per-clone LaunchAgent so the console starts at login and restarts if
it dies (label `co.powerset.powerpacks-console.<repo-dir-name>`, so multiple
clones can each run their own daemon on different ports):

```bash
PORT=5178 bash scripts/run-powerpacks-console.sh daemon-install
```

`HOST`/`PORT` are baked into the plist at install time (defaults `0.0.0.0` /
`5177`); re-run `daemon-install` to change them. Manage it with:

```bash
bash scripts/run-powerpacks-console.sh daemon-status     # launchctl state, pid, port, log tail
bash scripts/run-powerpacks-console.sh daemon-uninstall  # bootout + remove the plist
```

The daemon runs `scripts/run-powerpacks-console.sh run` (foreground vite with
`--strictPort`) and logs to `.powerpacks/servers/powerpacks-console.launchd.log`.
The plist lives in `~/Library/LaunchAgents/`. The `start`/`stop` background mode
above still works, but don't mix it with the daemon on the same port.
