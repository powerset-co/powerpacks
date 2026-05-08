# Messages Harness

The messages harness treats each local extraction step as a small primitive
with structured diagnostics.

## Why Bare Primitives First

iMessage is just local SQLite reads. Powerpacks runs that directly with a
stdlib-only Python primitive so Homebrew Python, virtualenvs, and package
installers are not in the critical path.

WhatsApp has real runtime surface area: Docker, WAHA, QR auth, and local
session state. Each of those is a separately-runnable stdlib-only primitive
(`waha_runtime`, `waha_session`, `extract_whatsapp_contacts`) so the harness can
compose them and an agent can recover from any single step failing without
rerunning the whole flow.

## Harness Contract

Every primitive should:

- write a manifest
- write empty output artifacts if it fails before producing rows
- include diagnostics specific enough for an agent to patch the primitive
- avoid message content entirely
- keep going at the harness level when one channel fails

## Pack Flow

1. `extract_imessage_contacts check`
2. User approves local extraction.
3. `extract_imessage_contacts extract --output-csv .powerpacks/messages/imessage.contacts.csv`
4. If it fails, inspect the manifest diagnostics and patch the primitive or
   rerun with explicit paths.
5. Optional WhatsApp extraction follows the same pattern: `waha_runtime check`
   → `waha_runtime up` (after consent) → `waha_session start --open --wait`
   (after consent + QR scan) → `extract_whatsapp_contacts extract` (after
   consent) → `normalize_message_contacts normalize`. WhatsApp extraction is
   exhaustive by default: do not pass `--skip-message-counts` in normal runs,
   allow up to an hour for large histories, and monitor the primitive's stderr
   heartbeat or `.progress.jsonl` artifact instead of assuming it is hung.

## Skill Boundary

Use one user-facing skill:

- `import-contacts`: guided iMessage + WhatsApp import, merge, review, and
  upload-gated sync. Use primitives directly for narrow debugging.

Reasoning-only steps stay in the skill/task docs. Executable steps are the
primitive scripts. This keeps the pack portable across Codex, NanoClaw, Claude
Code, or a human shell.

## Primitive Boundary

Keep primitives small and restartable:

- channel extraction
- output normalization
- merge/review decisions
- upload wrapper

Do not combine QR handling, Docker lifecycle, macOS permission prompts,
Contacts parsing, matching, and upload into one primitive. The harness should
compose them and record where the run failed.
