// packs/ingestion/primitives/accounts/accounts.py Account, one per connected account.

export type AccountSource = "gmail" | "imessage" | "whatsapp" | "linkedin"
export type AccountHealth = "ok" | "warning" | "error" | "off"

export interface Account {
  source: AccountSource
  name: string
  health: AccountHealth
  note: string
  messages: number
  contacts: number
  latest_message_at: string | null
  last_sync_at: string | null
}

export interface AccountsPayload {
  accounts: Account[]
  jobs: JobsPayload
  // Whether the daily refresh task is installed in Codex or Claude.
  scheduled: boolean
}

// api.py Job: a Sync or Reconnect the server is running, keyed by card ("gmail",
// "imessage", "whatsapp", or a Gmail address while it reconnects).
export interface Job {
  state: "running" | "done" | "failed"
  step: string
}

export type JobsPayload = Record<string, Job>
