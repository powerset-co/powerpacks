import { invoke } from "@/lib/desktop"
import { body, failure } from "@/lib/api/http"
import type { InstallStatus } from "@/types/install"

export async function fetchInstall(signal: AbortSignal): Promise<InstallStatus> {
  const response = await fetch("/api/install", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read installation progress")
  return body<InstallStatus>(response)
}

export async function installAction(action: string, values: object = {}): Promise<void> {
  const response = await fetch(`/api/install/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  })
  if (!response.ok) throw await failure(response, "Couldn't continue setup")
}

/** What the user answered to get setup past a wait (desktop/src-tauri/src/onboard.rs Answer). */
export interface SetupAnswer {
  gmailEmail?: string
  linkedinUrl?: string
}

/** Resumes setup in the desktop app, which runs it instead of an agent. */
export async function continueSetup(answer: SetupAnswer = {}): Promise<void> {
  await invoke("onboard_continue", { answer })
}

/** Skip a source setup stopped at (WhatsApp at its QR) for the rest of this setup; the server
 *  stops the waiting run and saves the skip, and setup resumes with `continueSetup`. */
export function skipSource(source: "whatsapp"): Promise<void> {
  return installAction("skip", { source })
}

/** Bring the app forward after a sign-in that had to happen in the browser. */
export function focusApp(): Promise<void> {
  return invoke("app_focus").then(() => undefined)
}

/** Open a page in the system browser. */
export function openExternal(url: string): Promise<void> {
  return invoke("open_external", { url }).then(() => undefined)
}

/** Whether macOS lets the app read Messages (Full Disk Access), checked afresh each call. */
export async function messagesReadable(): Promise<boolean> {
  return (await invoke("permission_messages")) === true
}

/** The command-line install on this machine whose data the app can import, as a path to show. */
export async function importSource(): Promise<string | null> {
  const path = await invoke("setup_import_source")
  return typeof path === "string" ? path : null
}

/** Import that install's data; true when setup is already done with it (the app can open). */
export async function importData(): Promise<boolean> {
  return (await invoke("setup_import")) === true
}
