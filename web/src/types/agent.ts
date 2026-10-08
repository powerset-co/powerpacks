// The Codex agent as the Agent page and the Accounts card see it: parsed from the
// `codex app-server` protocol by lib/agent/decode.ts.

export type CodexAccount =
  { kind: "chatgpt"; email: string | null; plan: string } | { kind: "apiKey" } | { kind: "other" }

export type CodexStatus = { installed: false } | { installed: true; account: CodexAccount | null }

export type ItemStatus = "running" | "done" | "failed" | "declined"

/** One row of the transcript, in arrival order. */
export type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; text: string }
  | { kind: "reasoning"; id: string; text: string }
  | {
      kind: "command"
      id: string
      command: string
      status: ItemStatus
      exitCode: number | null
      output: string
      durationMs: number | null
    }
  | { kind: "files"; id: string; paths: string[]; status: ItemStatus }
  | { kind: "tool"; id: string; label: string; status: ItemStatus }
  | { kind: "error"; id: string; text: string }

export type RequestId = string | number

/** Something Codex is waiting on the user for; answered through lib/agent/store `answer`. */
export type Approval =
  | { kind: "command"; requestId: RequestId; command: string; reason: string | null }
  | { kind: "files"; requestId: RequestId; reason: string | null }
  | {
      kind: "permissions"
      requestId: RequestId
      reason: string | null
      network: boolean
      paths: string[]
      // The requested profile, echoed back as the grant when the user allows it.
      grant: Record<string, unknown>
    }

export type ApprovalChoice = "once" | "session" | "decline"

/** A Codex notification for one thread, reduced to what the transcript needs. */
export type AgentEvent =
  | { type: "turnStarted"; threadId: string; turnId: string }
  | { type: "turnCompleted"; threadId: string; error: string | null }
  | { type: "item"; threadId: string; entry: Entry }
  | { type: "delta"; threadId: string; itemId: string; field: "text" | "output"; delta: string }
  | { type: "error"; threadId: string; message: string; willRetry: boolean }
  | { type: "loginCompleted"; success: boolean; error: string | null }
  | { type: "accountUpdated" }
