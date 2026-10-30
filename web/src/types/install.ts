// packs/powerset/primitives/install/status.py: one receipt for the active workflow.
export type InstallStep = string
export type InstallState = "running" | "waiting" | "failed" | "completed" | "skipped"
export interface InstallAction {
  kind:
    | "gmail"
    | "permission"
    | "qr"
    | "processing"
    | "review"
    | "owner"
    | "error"
    | "resume"
    | "details"
    | "signin"
  text?: string
  // signin: the provider's sign-in page, opened in a browser: Powerset's in the default one
  // (where the Mac's passkeys are), LinkedIn's and Google's in Chrome.
  url?: string
  provider?: "powerset" | "linkedin" | "google"
  command?: string
  qr_url?: string
  details?: unknown
}
/** The page's fixed words and rows, from packs/powerset/primitives/install/status.json. */
export interface InstallProse {
  page: Record<string, string>
}
export interface InstallStatus {
  /** When the record was last written (ISO). */
  updated_at: string
  primitive: "powerpacks_install"
  status: InstallState
  installer_pid?: number
  step: InstallStep
  event?: string
  message: string
  note?: string
  log_path: string
  retry_command: string
  steps: Partial<Record<InstallStep, { status: InstallState; message: string }>>
  plan?: InstallStep[]
  // When the run entered the current step, for the time a long step has taken.
  step_started_at?: string
  prose: InstallProse
  action?: InstallAction | null
  account_email?: string | null
  network_name?: string | null
  person_count?: number | null
}
