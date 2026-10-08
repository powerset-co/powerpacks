// The one place `codex app-server` JSON becomes typed values (types/agent.ts). Unknown methods
// and item types decode to null: the protocol grows faster than this page needs to.

import { isRecord } from "@/lib/utils"
import type {
  AgentEvent,
  Approval,
  CodexAccount,
  CodexStatus,
  Entry,
  ItemStatus,
  RequestId,
} from "@/types/agent"

type Json = Record<string, unknown>

function text(record: Json, key: string): string | null {
  const value = record[key]
  return typeof value === "string" ? value : null
}

function number(record: Json, key: string): number | null {
  const value = record[key]
  return typeof value === "number" ? value : null
}

function record(raw: unknown, key: string): Json | null {
  if (!isRecord(raw)) return null
  const value = raw[key]
  return isRecord(value) ? value : null
}

function list(record: Json, key: string): unknown[] {
  const value = record[key]
  return Array.isArray(value) ? value : []
}

const STATUS: Readonly<Record<string, ItemStatus>> = {
  inProgress: "running",
  completed: "done",
  failed: "failed",
  declined: "declined",
}

function status(item: Json): ItemStatus {
  return STATUS[text(item, "status") ?? ""] ?? "running"
}

export function decodeAccount(raw: unknown): CodexAccount | null {
  if (!isRecord(raw)) return null
  switch (text(raw, "type") ?? "") {
    case "chatgpt":
      return { kind: "chatgpt", email: text(raw, "email"), plan: text(raw, "planType") ?? "unknown" }
    case "apiKey":
      return { kind: "apiKey" }
    default:
      return { kind: "other" }
  }
}

/** desktop/src-tauri codex.rs `status`: `{installed, account}`. */
export function decodeStatus(raw: unknown): CodexStatus {
  if (!isRecord(raw) || raw.installed !== true) return { installed: false }
  return { installed: true, account: decodeAccount(raw.account) }
}

/** A ThreadItem as a transcript entry; null for items the page does not show. */
export function decodeItem(raw: unknown): Entry | null {
  if (!isRecord(raw)) return null
  const id = text(raw, "id")
  if (id === null) return null
  switch (text(raw, "type") ?? "") {
    case "agentMessage":
      return { kind: "agent", id, text: text(raw, "text") ?? "" }
    case "reasoning":
      return {
        kind: "reasoning",
        id,
        text: list(raw, "summary")
          .filter((part) => typeof part === "string")
          .join("\n\n"),
      }
    case "commandExecution":
      return {
        kind: "command",
        id,
        command: text(raw, "command") ?? "",
        status: status(raw),
        exitCode: number(raw, "exitCode"),
        output: text(raw, "aggregatedOutput") ?? "",
        durationMs: number(raw, "durationMs"),
      }
    case "fileChange":
      return {
        kind: "files",
        id,
        status: status(raw),
        paths: list(raw, "changes").flatMap((change) =>
          isRecord(change) ? [text(change, "path") ?? ""] : [],
        ),
      }
    case "mcpToolCall":
      return {
        kind: "tool",
        id,
        label: `${text(raw, "server") ?? "tool"} · ${text(raw, "tool") ?? ""}`,
        status: status(raw),
      }
    case "webSearch":
      return { kind: "tool", id, label: `Web search · ${text(raw, "query") ?? ""}`, status: "done" }
    default:
      return null
  }
}

const DELTAS: Readonly<Record<string, "text" | "output">> = {
  "item/agentMessage/delta": "text",
  "item/reasoning/summaryTextDelta": "text",
  "item/commandExecution/outputDelta": "output",
}

/** A server notification (`codex://notification` payload) as an AgentEvent. */
export function decodeNotification(raw: unknown): AgentEvent | null {
  if (!isRecord(raw)) return null
  const method = text(raw, "method") ?? ""
  const params = isRecord(raw.params) ? raw.params : {}
  const threadId = text(params, "threadId") ?? ""
  if (method === "account/login/completed") {
    return { type: "loginCompleted", success: params.success === true, error: text(params, "error") }
  }
  if (method === "account/updated") return { type: "accountUpdated" }
  if (method === "turn/started") {
    const turnId = text(record(params, "turn") ?? {}, "id")
    return turnId === null ? null : { type: "turnStarted", threadId, turnId }
  }
  if (method === "turn/completed") {
    const turn = record(params, "turn") ?? {}
    return { type: "turnCompleted", threadId, error: text(record(turn, "error") ?? {}, "message") }
  }
  if (method === "item/started" || method === "item/completed") {
    const entry = decodeItem(params.item)
    return entry === null ? null : { type: "item", threadId, entry }
  }
  const field = DELTAS[method]
  if (field !== undefined) {
    const itemId = text(params, "itemId")
    const delta = text(params, "delta")
    return itemId === null || delta === null ? null : { type: "delta", threadId, itemId, field, delta }
  }
  if (method === "error") {
    const message = text(record(params, "error") ?? {}, "message") ?? "Codex hit an error."
    return { type: "error", threadId, message, willRetry: params.willRetry === true }
  }
  return null
}

function requestId(raw: Json): RequestId | null {
  const id = raw.id
  return typeof id === "string" || typeof id === "number" ? id : null
}

function paths(permissions: Json): string[] {
  const fileSystem = record(permissions, "fileSystem") ?? {}
  return [...list(fileSystem, "read"), ...list(fileSystem, "write")].filter(
    (path) => typeof path === "string",
  )
}

/** A server request (`codex://request` payload) that needs the user, as an Approval. */
export function decodeRequest(raw: unknown): Approval | null {
  if (!isRecord(raw)) return null
  const id = requestId(raw)
  const params = isRecord(raw.params) ? raw.params : {}
  if (id === null) return null
  const reason = text(params, "reason")
  switch (text(raw, "method") ?? "") {
    case "item/commandExecution/requestApproval":
      return { kind: "command", requestId: id, command: text(params, "command") ?? "", reason }
    case "item/fileChange/requestApproval":
      return { kind: "files", requestId: id, reason }
    case "item/permissions/requestApproval": {
      const permissions = record(params, "permissions") ?? {}
      const network = record(permissions, "network")
      const fileSystem = record(permissions, "fileSystem")
      const grant = { ...(network && { network }), ...(fileSystem && { fileSystem }) }
      return {
        kind: "permissions",
        requestId: id,
        reason,
        network: network?.enabled === true,
        paths: paths(permissions),
        grant,
      }
    }
    default:
      return null
  }
}
