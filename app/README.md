# Powerpacks Console

Local Vite app for browsing Powerpacks artifacts from `.powerpacks/`.

```bash
cd .. # powerpacks repo root
scripts/run-powerpacks-console.sh start
```

From a Codex session outside the repo, use the installed bundle:

```bash
"$HOME/.codex/powerpacks/scripts/run-powerpacks-console.sh" start
```

Open the URL printed by the script, usually `http://localhost:5177/`.

The console is read-only and currently supports network search/rerank artifacts.
