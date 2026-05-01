# waha_session

WAHA session lifecycle and QR-code auth for WhatsApp. Stdlib-only.

The primitive talks to a running WAHA container (typically started by
`waha_runtime up`) over HTTP. It does not start Docker and it does not extract
contacts.

The QR code is fetched as a PNG **directly from WAHA** — no `qrcode` Python
dependency is involved. That image is the source of truth users scan.

## Subcommands

```bash
# Wait for the WAHA HTTP server to come up.
python packs/messages/primitives/waha_session/waha_session.py health

# Show current session state (exits 0 only if status == WORKING).
python packs/messages/primitives/waha_session/waha_session.py status

# Create or reuse the session, emit qr.png + qr.txt under .powerpacks/messages/whatsapp,
# open the PNG in the system image viewer, and poll until the user finishes the scan.
python packs/messages/primitives/waha_session/waha_session.py start --open --wait

# Re-poll an in-progress scan without recreating the session.
python packs/messages/primitives/waha_session/waha_session.py wait

# Stop and delete the session (does not stop the container).
python packs/messages/primitives/waha_session/waha_session.py stop
```

## Artifacts

`start` writes two files under `--qr-dir` (default `.powerpacks/messages/whatsapp/`):

- `qr.png` — image of the WhatsApp linking QR; scan it with WhatsApp >
  Settings > Linked Devices > Link a Device.
- `qr.txt` — the raw QR payload as text, for fallback rendering or debugging.

Both are refreshed every 15 seconds while waiting, in case WhatsApp rotates
the QR.

## Environment overrides

| Variable | Default |
| --- | --- |
| `POWERPACKS_WAHA_BASE_URL` | `http://127.0.0.1:3000` |
| `POWERPACKS_WAHA_API_KEY` | `powerpacks-local` |
| `POWERPACKS_WAHA_SESSION` | `default` |
| `POWERPACKS_WAHA_QR_DIR` | `.powerpacks/messages/whatsapp` |
