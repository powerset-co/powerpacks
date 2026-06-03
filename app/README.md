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
