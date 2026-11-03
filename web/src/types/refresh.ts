// packs/ingestion/primitives/refresh/daily.py Run, SourceRun and Problem; api.py refresh_payload.

export type RefreshSource = "linkedin" | "gmail" | "imessage" | "whatsapp"

export interface SourceRun {
  source: RefreshSource
  status: "synced" | "skipped" | "failed"
  accounts: string[]
}

/** Something only the person can fix, worded as what to do, with where to do it. */
export interface Problem {
  source: RefreshSource | "refresh" | "powerset"
  title: string
  detail: string
  action: string
  href: string
}

export interface RefreshRun {
  id: number
  trigger: "daily" | "manual"
  status: "running" | "ok" | "needs_you" | "failed"
  started_at: string
  finished_at: string
  pid: number
  missed_days: number
  sources: SourceRun[]
  people_before: number | null
  people_after: number | null
  index_rebuilt: boolean
  problems: Problem[]
  message: string
  error: string
  log: string
}

export interface RefreshSettings {
  enabled: boolean
  // HH:MM, 24-hour, this Mac's time.
  time: string
  label: string
}

export interface RefreshState {
  settings: RefreshSettings
  next_run_at: string | null
  running: RefreshRun | null
  runs: RefreshRun[]
}
