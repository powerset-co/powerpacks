# Messages Harness

The messages harness treats each local extraction step as a small primitive
with structured diagnostics.

## Why Bare Primitives First

iMessage is just local SQLite reads. Powerpacks should run that directly with a
stdlib-only Python primitive so Homebrew Python, virtualenvs, and package
installers are not in the critical path.

WhatsApp still has real runtime surface area: Docker, WAHA, QR auth, and local
session state. Keep `contact-exporter` available there until we port those
pieces into similarly small primitives.

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
5. Optional WhatsApp/review/upload can use `powerset_contacts_harness` until
   those are ported.

## Skill Boundary

Use a single user-facing skill for the pack:

- `import-messages`: plan and execute local message-contact import runs

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
