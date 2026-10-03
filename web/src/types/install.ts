// packs/powerset/primitives/install/status.py: the installer's own manifest.
export type InstallStep =
  | "runtime"
  | "dependencies"
  | "skills"
  | "tools"
  | "account"
  | "credentials"
  | "connection"
  | "network"
  | "ready"
export type InstallState = "running" | "waiting" | "failed" | "completed" | "skipped"

export interface InstallStatus {
  primitive: "powerpacks_install"
  status: InstallState
  step: InstallStep
  message: string
  log_path: string
  retry_command: string
  steps: Partial<Record<InstallStep, { status: InstallState; message: string }>>
  account_email?: string | null
  network_name?: string | null
  person_count?: number | null
}
