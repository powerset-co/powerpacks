# Powerpacks automations

Created: 2026-08-14

Change log:
- 2026-08-14: Codex App automation templates (PR #365).
- 2026-09-28: one template per harness; installed and removed from the local
  UI's Scheduled tasks page (`packs/ingestion/primitives/refresh/tasks.py`).

Each directory is one scheduled task. Codex install opens a chat with a request
for its native automation tool to create a recurring task attached to that chat.
"Start each run in new chat" stays off, and successful runs keep the chat open.
Claude uses a prompt template:

| File | Harness | Installed to |
| --- | --- | --- |
| `claude-task.md` | Claude Desktop | `~/.claude/scheduled-tasks/<id>/SKILL.md` + an entry in Desktop's `claude-code-sessions/<account>/<org>/scheduled-tasks.json` (Desktop reads it at launch, so install restarts Desktop) |

`${prompt}` is filled in at install time. From the repo root:

```bash
uv run --project . python packs/ingestion/primitives/refresh/tasks.py install --runner codex
uv run --project . python packs/ingestion/primitives/refresh/tasks.py uninstall --runner claude
```
