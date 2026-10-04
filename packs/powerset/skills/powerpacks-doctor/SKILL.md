---
name: powerpacks-doctor
description: Diagnose Powerpacks installation, Python, login, credentials, and agent connection problems. Use for a requested health check or when a Powerpacks workflow fails with an unclear setup error. Repair within the user's requested scope; not for routine preflight or product development.
---

# Powerpacks Doctor

Use the installed checkout identified by this skill's context. For an uninstalled
source skill, use `POWERPACKS_REPO_ROOT` when supplied, otherwise `~/powerpacks`.
Run commands there. For data stored under the wrong installation, use
`fix-powerpacks` instead.

Start with the failing command's error. If the cause and repair are already
clear, make that repair; don't run a broad check merely to confirm it.
Otherwise run the read-only report:

```bash
bin/doctor run --profile search-core --env-file .env
```

Read `checks`: each entry provides `status`, `message`, `fix_kind`, and, when
available, `fix_command`. An audit-only request ends with the findings.
During requested setup or repair, execute relevant free, reversible fixes
directly and retry the failing command. Do not give the user a command you can
run or ask again for an already-authorized repair.

- Python or dependency problems: run `bin/setup-python`.
- Login or credential refresh: use `powerset`; sign-in opens in the system's
  default browser while any in-app progress pane stays on its status page.
- Other local fixes: run the relevant reported command from this shell. Do not
  run blanket `doctor fix` or hide browser/TTY interaction in a nested command.
- Human action or account provisioning: explain the one action needed. An agent
  cannot grant the account service access; report that limitation accurately.

Ask before new spend, destructive changes, or expanding access. Preserve local
data and paid artifacts; don't start imports, enrichment, or uploads as repair.
Never print credential values. Stop if the same failure returns after a
targeted repair and explain the remaining action.

Verify the original command works before reporting recovery. Keep the result
short and distinguish installation, sign-in, and working search.
