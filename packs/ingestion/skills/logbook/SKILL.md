---
name: logbook
description: Download EVERYTHING for a set of people across Gmail, iMessage, and WhatsApp and format it into a faithful, verbatim raw markdown archive — one file per email thread / DM / group, ## YYYY headings inside, append-only incremental sync. The inverse of $deep-context (which synthesizes facts and discards text). Use for $logbook, "build a raw logbook", "archive all my messages with these people", "dump every email/text/whatsapp with X verbatim", "logbook from this CSV".
---

<!--
Created: 2026-06-25
Changelog:
- 2026-06-25: Initial skill. Raw verbatim archive (no LLM, no network, no spend) from a
  people CSV. Streaming readers (uncapped, group-aware) reuse deep_context.sources for the
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
  rebuild from scratch: that's just `export` (it overwrites). Do not invent flags.
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
   `unreadable_full_disk_access`, tell the user to grant FDA (or run in their
   terminal) before relying on iMessage.

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
   per-channel watermark from `manifest.json`, pulls only newer messages, and
   appends them to the existing files:
   ```bash
   bin/logbook sync --csv "<path>"
   ```
   Re-running with nothing new is a no-op.

## Notes

- **Memory:** one ordered cursor per (entry, channel), streamed row-by-row; one
  output file open at a time. Peak RSS stays in the tens of MB even for a contact
  with 100k+ messages — bounded by the work, not the corpus.
- **Stable ids for dedupe/sync** live in each file's frontmatter (`container_id`)
  and the manifest watermark map: Gmail thread id + message id (+ `sources.sync_cursor`
  historyId), wacli `chat_jid` + `msg_id` + monotonic `rowid`, chat.db `chat.guid`
  + monotonic `ROWID`. Sync filters on the monotonic id, so appends never duplicate.
- **Raw fidelity:** bodies are kept verbatim (quoted reply chains, signatures,
  `[cid:...]` image refs and all) — this is a raw archive, not a summary.
- Group entries are keyed by group name (their own top-level slug), so a shared
  group is written once, not duplicated under every member.
