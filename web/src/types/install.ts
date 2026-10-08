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
    | "approval"
    | "owner"
    | "error"
    | "resume"
    | "recovery"
    | "details"
  text?: string
  command?: string
  // approval: the spend step and its estimate (packs/powerset/primitives/install/pipeline.py)
  step?: string
  estimate?: unknown
  qr_url?: string
  details?: unknown
}
/** The page's rows and fixed words, from packs/powerset/primitives/install/status_prose.py. */
export interface InstallProse {
  rows: { label: string; steps: InstallStep[]; done_label: string; needs: InstallStep }[]
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
  prose: InstallProse
  action?: InstallAction | null
  index_progress?: { status: string; message: string; progress?: number; payload: unknown } | null
  account_email?: string | null
  network_name?: string | null
  person_count?: number | null
}
