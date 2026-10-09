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
  // signin: the provider's sign-in page. Powerset and LinkedIn show inside the desktop app;
  // Google only allows its own in the browser, so the app opens it there and waits.
  url?: string
  provider?: "powerset" | "linkedin" | "google"
  command?: string
  qr_url?: string
  details?: unknown
}
/** The page's rows and fixed words, from packs/powerset/primitives/install/status_prose.py. */
export interface InstallProse {
  rows: { label: string; steps: InstallStep[]; needs: InstallStep }[]
  page: Record<string, string>
}
export interface InstallStatus {
  primitive: "powerpacks_install"
  status: InstallState
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
  index_progress?: { status: string; message: string; progress?: number; payload: unknown } | null
  account_email?: string | null
  network_name?: string | null
  person_count?: number | null
}
