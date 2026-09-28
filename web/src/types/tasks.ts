// packs/ingestion/primitives/refresh/tasks.py Task, Install and Run.

export type Runner = "codex" | "claude"

export interface TaskRun {
  id: string
  runner: Runner
  started_at: string
  status: "ok" | "failed" | "unknown"
  summary: string
  // codex://threads/<id> for a Codex run; none for the others.
  open_url: string | null
  resume_command: string | null
}

export interface Task {
  id: string
  name: string
  schedule: string
  command: string
  installs: Runner[]
  runs: TaskRun[]
}
