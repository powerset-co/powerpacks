# Powerpacks automations

Created: 2026-08-14

Change log:
- 2026-08-14: Codex App automation templates (PR #365).
- 2026-09-28: one template per harness; installed and removed from the local
  UI's Scheduled tasks page (`packs/ingestion/primitives/refresh/tasks.py`).

Each directory is one scheduled task. The Tasks page offers a cadence and local time in each install modal.
Codex install starts `codex app-server`, persists a chat in the
Powerpacks checkout, and verifies its Full access permissions before writing a
heartbeat configuration targeting that chat. It opens the chat through its deep
link; the page shows pending until the Codex app imports the configuration.
Changing the schedule reuses the chat; successful runs keep it open.
Claude saves the selected cadence and local time with its prompt template:

| File | Harness | Installed to |
| --- | --- | --- |
| `claude-task.md` | Claude Desktop | `~/.claude/scheduled-tasks/<id>/SKILL.md` + an entry in Desktop's `claude-code-sessions/<account>/<org>/scheduled-tasks.json` (Desktop reads it at launch, so install restarts Desktop) |

`${prompt}` is filled in at install time. From the repo root:

```bash
uv run --project . python packs/ingestion/primitives/refresh/tasks.py install --runner codex
uv run --project . python packs/ingestion/primitives/refresh/tasks.py uninstall --runner claude
```
