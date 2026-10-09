// The desktop app's update check (desktop/src-tauri/src/update.rs): the latest Powerpacks release
// against the running app. Only the app can ask; the browser build has nothing to update.

import { invoke } from "@/lib/desktop"
import { readStored, writeStored } from "@/lib/storage"
import { isRecord } from "@/lib/utils"

export interface Update {
  current: string
  latest: string
  /** The release page, for the system browser. */
  url: string
  available: boolean
}

export const UPDATE_INTERVAL_MS = 12 * 60 * 60 * 1000
const CHECKED_KEY = "update.checked"
const DISMISSED_KEY = "update.dismissed"

export interface Checked {
  at: number
  update: Update
}

function isUpdate(raw: unknown): raw is Update {
  return (
    isRecord(raw) &&
    typeof raw.current === "string" &&
    typeof raw.latest === "string" &&
    typeof raw.url === "string" &&
    typeof raw.available === "boolean"
  )
}

export async function checkUpdate(): Promise<Update> {
  const update = await invoke("update_check")
  if (!isUpdate(update)) throw new Error("The update check answered oddly.")
  writeStored("local", CHECKED_KEY, { at: Date.now(), update })
  return update
}

/** The last check on this machine, so a relaunch within the interval asks nothing. */
export function lastCheck(): Checked | null {
  return readStored("local", CHECKED_KEY, (raw) =>
    isRecord(raw) && typeof raw.at === "number" && isUpdate(raw.update)
      ? { at: raw.at, update: raw.update }
      : null,
  )
}

/** The version the user dismissed; the pane stays away until a newer one. */
export function dismissedVersion(): string | null {
  return readStored("local", DISMISSED_KEY, (raw) => (typeof raw === "string" ? raw : null))
}

export function dismissVersion(version: string): void {
  writeStored("local", DISMISSED_KEY, version)
}
