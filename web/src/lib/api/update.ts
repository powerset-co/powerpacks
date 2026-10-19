// The desktop app's updater (the Tauri updater plugin in desktop/src-tauri): checks the release
// feed against the running app, installs the new version in place, and restarts into it. Only the
// app can ask; the browser build has nothing to update.

import { invoke, listen } from "@/lib/desktop"
import { readStored, writeStored } from "@/lib/storage"
import { isRecord } from "@/lib/utils"

export interface Update {
  current: string
  latest: string
  available: boolean
}

/** Bytes of the new version downloaded so far; `total` is null when the server sent no size. */
export interface UpdateProgress {
  downloaded: number
  total: number | null
}

export const UPDATE_INTERVAL_MS = 12 * 60 * 60 * 1000
const DISMISSED_KEY = "update.dismissed"

function isUpdate(raw: unknown): raw is Update {
  return (
    isRecord(raw) &&
    typeof raw.current === "string" &&
    typeof raw.latest === "string" &&
    typeof raw.available === "boolean"
  )
}

function isProgress(raw: unknown): raw is UpdateProgress {
  return (
    isRecord(raw) &&
    typeof raw.downloaded === "number" &&
    (typeof raw.total === "number" || raw.total === null)
  )
}

export async function checkUpdate(): Promise<Update> {
  const update = await invoke("update_check")
  if (!isUpdate(update)) throw new Error("The update check answered oddly.")
  return update
}

/** Downloads, verifies and installs the new version; it runs once the app restarts. */
export async function installUpdate(): Promise<void> {
  await invoke("update_install")
}

/** Restarts into the installed version. */
export async function restartApp(): Promise<void> {
  await invoke("update_restart")
}

/** The download's progress while `installUpdate` runs; returns the unsubscribe. */
export function onUpdateProgress(handler: (progress: UpdateProgress) => void): () => void {
  return listen("update:progress", (payload) => {
    if (isProgress(payload)) handler(payload)
  })
}

/** The version the user dismissed; the pane stays away until a newer one. */
export function dismissedVersion(): string | null {
  return readStored("local", DISMISSED_KEY, (raw) => (typeof raw === "string" ? raw : null))
}

export function dismissVersion(version: string): void {
  writeStored("local", DISMISSED_KEY, version)
}
