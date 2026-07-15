---
name: update-powerpacks
description: Force-update the canonical Powerpacks checkout to origin/main and reinstall skills while preserving .powerpacks. Use for $update-powerpacks.
---

# update-powerpacks

Run exactly one command for the current harness:

**Codex**

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/update-powerpacks/update-powerpacks" codex
```

**Claude Code**

```bash
"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/update-powerpacks/update-powerpacks" claude-code
```

**Pi**

```bash
"${PI_HOME:-$HOME/.pi/agent}/skills/update-powerpacks/update-powerpacks" pi
```

Do not inspect Git state first. Do not run any other Git command. Do not pop or
repair the stash. Do not run setup, doctor, imports, or state repair afterward.
Report the script's final key/value lines.
