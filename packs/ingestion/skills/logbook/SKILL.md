---
name: logbook
description: Download EVERYTHING for a set of people across Gmail, iMessage, and WhatsApp and format it into a faithful, verbatim raw markdown archive — one file per email thread / DM / group, ## YYYY headings inside, append-only incremental sync. The inverse of $deep-context (which synthesizes facts and discards text). Use for $logbook, "build a raw logbook", "archive all my messages with these people", "dump every email/text/whatsapp with X verbatim", "logbook from this CSV".
---

<!--
Created: 2026-06-25
Changelog:
- 2026-09-30: People page reads saved logbooks in-app (Logbook reader, Has logbook
  filter); the ZIP download is gone.
- 2026-09-30: WhatsApp reads include `@lid` chats and groups where the person is a
  silent participant; export stages each entry so a failed read keeps the old
  archive; unreadable chat.db reports `unreadable`; memory and sync-order notes
  corrected.
- 2026-09-30: People page "Build logbook" (selected people, in-process, no CSV);
  export keeps other entries and moves rebuilt files to `<slug>.bkup-<utc>/`;
  missing/unreadable channels are reported and left untouched.
- 2026-06-25: Initial skill. Raw verbatim archive (no LLM, no network, no spend) from a
  people CSV. Streaming readers (uncapped, group-aware) reuse deep_context.context_sources for the
  msgvault connection, attributedBody decoding, and immutable chat.db open. One entry per
  person OR group (slug = person/group name); one file per email thread / DM / group;
  ## YYYY headings inside; stable ids (gmail thread id + message id + historyId, wacli
  chat_jid + msg_id + rowid, chat.db chat guid + ROWID) in frontmatter + manifest. export =
  full rebuild; sync = append-only incremental keyed on a monotonic per-channel watermark.
-->

# logbook

Use this for `$logbook`, "build a raw logbook", "archive everything I've said
with these people", or "dump every email/text/WhatsApp with <person> verbatim".

It builds a **faithful, verbatim raw archive** of your conversations with a list
of people: every Gmail thread, every iMessage/WhatsApp DM, and group chats —
formatted into readable markdown. **No LLM, no network, no spend** — every read is
a local SQLite query, so it's fast and free. This is the *inverse* of
`$deep-context`: that skill synthesizes facts and throws the text away; this one
keeps the text.

## Defaults — this is NOT `$deep-context`. Do not borrow its defaults.

`$logbook` has its OWN behavior. Even though it shares low-level readers with
`$deep-context`, **none of deep-context's caps or opt-ins apply here**:

- **NO message cap.** Download EVERYTHING. deep-context caps pooled messages at
  1600 per person — `$logbook` does not. Never apply a 1600 (or any) message cap.
- **Groups are ON by default** (DMs *and* every group a person is in, both chat
  channels). deep-context makes group bodies opt-in; `$logbook` includes them by
  default. Use `--no-groups` only if the user explicitly asks to exclude groups.
- **Deepen ALWAYS** (step 3 below) — it is not optional and you do not ask to skip.
- **There is no `--force` flag.** If the user says "force"/"refresh", they mean
  rebuild from scratch: that's just `export`. It rebuilds only the CSV's entries,
  moves their prior files to `<slug>.bkup-<utc>/`, and keeps every other entry.
  Do not invent flags.
- **When a request is genuinely ambiguous, ASK** — do not silently pick a
  conservative default. "Everything" means everything; if you're unsure of scope,
  ask the user rather than quietly narrowing it.

Output lives under `.powerpacks/logbook/` (gitignored):

```
.powerpacks/logbook/
  <slug>/                      slug = person name OR group name
    gmail/<year>-<subject>-<hash>.md   one file per email thread
    imessage/dm.md
    whatsapp/dm.md
  <group-slug>/whatsapp/group.md       groups are their own top-level entry
  index.md                     catalog (entries, files, message counts, channels)
  manifest.json                counts + per-container stable-id watermarks (sync state)
```

Each file has YAML frontmatter (`entry, channel, kind, container_id, title,
created_at`), then `## YYYY` sections of `**<date> · <sender>:** <verbatim body>`.

## Privacy

This skill reads AND persists **verbatim message bodies**, including **group
chats** — a deliberate, second scoped exception to the metadata-only privacy
contract (`$deep-context` is the first, but it's DM-only and synthesized). Bodies
are written to `.powerpacks/logbook/` which is **gitignored**; nothing is sent
anywhere. **Groups are included by default** (this is "everything"); pass
`--no-groups` to limit to DMs/threads. Surface this once so the user knows
verbatim group bodies are being archived.

## Run it in YOUR terminal (Full Disk Access)

iMessage reads need `chat.db`, which requires **Full Disk Access**. The Claude
Code Bash tool runs under a helper that does NOT inherit your terminal's FDA, so
iMessage reads can come back empty there. For the iMessage channel, run
`bin/logbook ...` directly in a terminal that has Full Disk Access (e.g. Ghostty).
Gmail (msgvault) and WhatsApp (wacli) read fine from anywhere.

## Flow

1. **Ask for the CSV.** "Point me to your CSV of people to build a logbook for."
   Accepted shapes (auto-detected):
   - founder CSV: `Founder, Cell, Emails, WhatsApp Groups` (Cell = comma-separated
     phones; Emails = `;`-separated; the group cell names one group to archive).
   - the merged `people.csv` (canonical network-import schema).

2. **Check reachability** (read-only, free) — confirms we can actually reach these
   people per channel and how deep each store goes:
   ```bash
   bin/logbook check --csv "<path>"
   ```
   Report per-channel `status` + earliest/latest dates. If iMessage is
   `unreadable`, the usual cause is missing Full Disk Access (grant it or run in
   their terminal); a corrupt chat.db reads the same way.

3. **Deepen the local stores — ALWAYS. Not optional, do not ask, do not offer to
   skip.** "Everything" means deepest-available, so deepen is part of every run. It
   is FREE (no per-message cost). Just run it:
   ```bash
   bin/logbook deepen --csv "<path>" --run --rounds 3
   ```
   `deepen` is **scoped to the CSV people's chats** (their DMs + groups, by jid) — it
   does NOT backfill the user's entire WhatsApp. Do NOT print an estimate as a gate
   and do NOT ask "skip deepen?" — just deepen, then export. (WhatsApp may connect to
   the user's phone; mention that in passing, but still run it.) Per-channel depth:
   - **iMessage** — `chat.db` is already complete locally (beginning-of-time); deepen is a no-op.
   - **Gmail (msgvault)** — backfills the whole mailbox (`gmail.py discover --fresh`, OAuth, free).
   - **WhatsApp (wacli)** — shallow by default; deepen runs scoped
     `wacli history backfill --chat <jid> --requests <rounds>` (on-demand sync
     from the primary phone). Depth is bounded by what the phone still holds, so
     full WhatsApp history is not guaranteed.

4. **Export** (full build — all channels + all groups by default):
   ```bash
   bin/logbook export --csv "<path>"                     # everything (all channels + groups)
   bin/logbook export --csv "<path>" --channels gmail    # one channel
   bin/logbook export --csv "<path>" --no-groups         # DMs/threads only
   bin/logbook export --csv "<path>" --slug <one-slug>   # just one person/group
   ```
   Then point the user at `.powerpacks/logbook/index.md`.

5. **Sync** later (incremental, **append-only** — never overwrites). Reads the
   per-channel watermark from `manifest.json`, pulls only messages the stores got
   since, and appends them to the existing files. The watermark is store insertion
   order, so history deepened after the last sync is appended at the end, out of
   date order — run `export` after a deepen to get date order back:
   ```bash
   bin/logbook sync --csv "<path>"
   ```
   Re-running with nothing new is a no-op.

## From the People page

`bin/deep-context review people` → select people (or open one) → **Build logbook**.
It runs the same export for exactly those people (every child's email and phone
under each parent; no Worth or share filter), over every date the local stores
hold, groups included. It reads only what is already synced — no deepen, no
network — and says which channel was missing or unreadable. When it finishes the
page opens the Logbook reader on what it built; **Back to People** (or browser
Back) returns to the same list. Saved logbooks stay readable after a restart:
filter People by **Logbook: Has logbook** and use **View logbook** on a person or
selection. Nothing is uploaded with Share.

## Notes

- **Memory:** one output file open at a time. Gmail streams one message at a time;
  iMessage and WhatsApp load one conversation (a person's DMs or one group) at a
  time, so memory follows the largest single conversation.
- **Stable ids for dedupe/sync** live in each file's frontmatter (`container_id`)
  and the manifest watermark map: Gmail thread id + message id (+ `sources.sync_cursor`
  historyId), wacli `chat_jid` + `msg_id` + `rowid`, chat.db `chat.guid` + `ROWID`.
  Sync filters on those increasing ids. A failed append can leave unrecorded lines;
  run `export` to rebuild after a failure. IDs follow insertion, not message date.
- **Raw fidelity:** bodies are kept verbatim (quoted reply chains, signatures,
  `[cid:...]` image refs and all) — this is a raw archive, not a summary.
- Group entries are keyed by group name (their own top-level slug), so a shared
  group is written once, not duplicated under every member.
