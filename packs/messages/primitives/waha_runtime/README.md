# waha_runtime

Docker + WAHA container lifecycle for the WhatsApp pack. Stdlib-only.

This primitive does **not** call the WAHA HTTP API. It only:

- checks whether Docker is installed and the daemon is reachable
- pulls and runs the WAHA NOWEB image as `powerpacks-waha`
- mounts `~/.powerpacks/waha-sessions` so QR-scanned credentials persist
- stops / removes the container
- reports container status

Session/QR/contact extraction live in the sibling primitives
`waha_session` and `extract_whatsapp_contacts`.

## Usage

```bash
# 1. Find out whether Docker is installed and the daemon is healthy.
python packs/messages/primitives/waha_runtime/waha_runtime.py check

# 2. Pull (if needed) and start the WAHA container.
python packs/messages/primitives/waha_runtime/waha_runtime.py up

# 3. Inspect status (exits 0 only if the container is running).
python packs/messages/primitives/waha_runtime/waha_runtime.py status

# 4. Stop and remove the container. Use --purge-session to also delete
#    the persisted WhatsApp credentials so the next `up` requires a fresh QR.
python packs/messages/primitives/waha_runtime/waha_runtime.py down
python packs/messages/primitives/waha_runtime/waha_runtime.py down --purge-session
```

`check` exits non-zero when Docker is missing or the daemon is not running and
emits `alternatives` describing how to install Docker Desktop, Colima, or the
Linux Docker engine. The skill should surface these to the user and ask for
explicit consent before installing anything.

## Environment overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `POWERPACKS_WAHA_CONTAINER` | `powerpacks-waha` | Docker container name |
| `POWERPACKS_WAHA_PORT` | `3000` | Host port WAHA is bound to |
| `POWERPACKS_WAHA_API_KEY` | `powerpacks-local` | Value forced via `WAHA_API_KEY` |
| `POWERPACKS_WAHA_IMAGE` | `devlikeapro/waha:noweb-2026.3.4` | WAHA image tag |
| `POWERPACKS_WAHA_SESSIONS_DIR` | `~/.powerpacks/waha-sessions` | Persistent session dir |

The same values can be passed as flags (`--container-name`, `--port`, etc.).
