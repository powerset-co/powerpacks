// packs/powerset/primitives/install/status.py: the installer's own manifest.
export type InstallStep = "runtime" | "dependencies" | "skills" | "tools" | "ready"
export type InstallState = "running" | "waiting" | "failed" | "completed"

export interface InstallStatus {
  primitive: "powerpacks_install"
  status: InstallState
  step: InstallStep
  message: string
  log_path: string
  retry_command: string
}
