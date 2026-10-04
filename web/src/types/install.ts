// packs/powerset/primitives/install/status.py: one receipt for the active workflow.
export type InstallStep = string
export type InstallState = "running" | "waiting" | "failed" | "completed" | "skipped"
export interface InstallAction {
  kind:
    | "sources"
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
  text?: string
  command?: string
  app_path?: string | null
  qr_url?: string
  details?: unknown
}
export interface InstallStatus {
  primitive: "powerpacks_install"
  status: InstallState
  step: InstallStep
  message: string
  log_path: string
  retry_command: string
  steps: Partial<Record<InstallStep, { status: InstallState; message: string }>>
  plan?: InstallStep[]
  labels?: Record<string, string>
  action?: InstallAction | null
  index_progress?: { status: string; message: string; progress?: number; payload: unknown } | null
  account_email?: string | null
  network_name?: string | null
  person_count?: number | null
}
