"""wacli binary client package (openclaw/wacli WhatsApp metadata sync).

One module per concern, low layer first:
`paths` (fixed artifact locations) · `runtime` (errors, stderr status, progress,
the pinned `run_command`) · `util` (pure phone/jid/name + depth value helpers) ·
`payloads` (frozen dataclasses that parse wacli's JSON once) · `binary` (pinned
binary install/verify + `wacli --json`) · `store_db` (read-only SQLite over
`wacli.db`) · `qr` (login-QR render/redaction) · `pairing` (device identity +
the full-sync pairing marker) · `auth` (link the account) · `sync` (one metadata
sync pass) · `backfill` (the `history backfill-batch` boundary) · `depth` (the
history-depth stage) with `depth_results` (its artifacts).

Changelog:
- 2026-07-30 (created): the `whatsapp_wacli.py` monolith split into this
  package; the old path stays as the thin CLI entry.

The CLI over all of it is `../whatsapp_wacli.py`. No re-exports: consumers
import the module that defines what they need.
"""
