// One source's sign-in before setup, as the Connect screen and the onboarding page run it: its
// steps in the connect record, what it needs on this Mac first, and where it stands.

import type { SignedIn } from "@/lib/api/accounts"
import { fetchPreflight, installPreflight, type ConnectSource, type Preflight } from "@/lib/api/install"
import type { InstallStatus, InstallStep } from "@/types/install"

// While a sign-in or an install is still running, ask again this often.
export const POLL_MS = 2_000

// Each source's steps in the Connect screen's run, the last its login (status_prose.py).
export const STEPS: Record<ConnectSource, readonly InstallStep[]> = {
  linkedin: ["linkedin_login"],
  gmail: ["gmail_tools", "gmail_login"],
  whatsapp: ["whatsapp_tools", "whatsapp_login"],
}

// What a source's sign-in needs on this Mac first (preflight.py): a browser for LinkedIn's and
// Google's sign-in pages, and Google's command-line tools for the Gmail app.
export type Tool = "chromium" | "gcloud"
export const NEEDS: Record<ConnectSource, readonly Tool[]> = {
  linkedin: ["chromium"],
  gmail: ["chromium", "gcloud"],
  whatsapp: [],
}
export const TOOL = { chromium: "browser", gcloud: "gcloud" } as const

/** Whether the connect record is a sign-in still under way: each write carries its run's pid until
 *  the run hands back (0) or fails, and a read reports a run whose process is gone as stopped. */
export function live(record: InstallStatus): boolean {
  return (record.installer_pid ?? 0) > 0 && record.status !== "failed"
}

export type Standing =
  | { state: "working"; qr?: string }
  | { state: "needed"; stopped: true; line: string; command?: string }
  | { state: "checking" }
  | { state: "ready" }
  | { state: "needed" }

/** Where one source stands: its sign-in under way (the connect record), else whether it is signed
 *  in (accounts.signed_in, the answer setup takes), else whether its last sign-in stopped. */
export function standing(
  record: InstallStatus | undefined,
  signed: SignedIn | undefined,
  source: ConnectSource,
): Standing {
  const own = record && STEPS[source].includes(record.step) ? record : undefined
  if (own && live(own)) return { state: "working", qr: own.action?.qr_url }
  // Stopped: failed, or waiting with nothing left running (a password, a closed window). It comes
  // before the sign-in answer: a Gmail mailbox added to a signed-in one can stop.
  if (own && (own.status === "failed" || own.status === "waiting"))
    return { state: "needed", stopped: true, line: own.message, command: own.action?.command }
  if (signed === undefined) return { state: "checking" }
  return signed[source] ? { state: "ready" } : { state: "needed" }
}

/** Install what a source's sign-in needs (a browser, Google's tools) and wait for it. */
export async function prepare(source: ConnectSource): Promise<void> {
  const needed = (checks: Preflight) => NEEDS[source].map((tool) => checks[TOOL[tool]])
  const checks = await fetchPreflight()
  for (const tool of NEEDS[source]) if (!checks[TOOL[tool]].ok) await installPreflight(tool)
  for (let now = checks; !needed(now).every((check) => check.ok); now = await fetchPreflight()) {
    const broken = needed(now).find((check) => check.install?.status === "failed")
    if (broken) throw new Error(broken.install?.message ?? "Could not install what this sign-in needs")
    await new Promise((resolve) => setTimeout(resolve, POLL_MS))
  }
}
