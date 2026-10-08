// The Codex agent through the desktop app (desktop/src-tauri/src/codex.rs). Requests go to
// `codex app-server`; its notifications and requests arrive as desktop events.

import {
  decodeHistory,
  decodeNotification,
  decodeRequest,
  decodeStatus,
  decodeThreadSummary,
} from "@/lib/agent/decode"
import { invoke, listen } from "@/lib/desktop"
import { isRecord } from "@/lib/utils"
import type {
  AgentEvent,
  Approval,
  ApprovalChoice,
  CodexStatus,
  Entry,
  RequestId,
  ThreadSummary,
} from "@/types/agent"

const NOTIFICATION_EVENT = "codex://notification"
const REQUEST_EVENT = "codex://request"
const EXIT_EVENT = "codex://exit"

function call(method: string, params: Record<string, unknown>): Promise<unknown> {
  return invoke("codex_call", { method, params })
}

export async function fetchCodexStatus(): Promise<CodexStatus> {
  return decodeStatus(await invoke("codex_status"))
}

/** Opens ChatGPT sign-in in the system browser; returns the login id to cancel it by. */
export async function startCodexLogin(): Promise<string> {
  const login = await invoke("codex_login")
  return isRecord(login) && typeof login.loginId === "string" ? login.loginId : ""
}

export async function cancelCodexLogin(loginId: string): Promise<void> {
  await call("account/login/cancel", { loginId })
}

export async function logoutCodex(): Promise<void> {
  await call("account/logout", {})
}

/** A new thread in the Powerpacks checkout; returns its id. */
export async function startThread(): Promise<string> {
  const started = await invoke("codex_start_thread")
  const thread = isRecord(started) && isRecord(started.thread) ? started.thread : {}
  if (typeof thread.id !== "string") throw new Error("Codex did not start a conversation.")
  return thread.id
}

/** This app's past chats, newest first. */
export async function fetchThreads(): Promise<ThreadSummary[]> {
  const listed = await invoke("codex_threads")
  const data: unknown[] = isRecord(listed) && Array.isArray(listed.data) ? listed.data : []
  return data.flatMap((raw) => {
    const summary = decodeThreadSummary(raw)
    return summary ? [summary] : []
  })
}

/** Resumes a past chat; returns its transcript so far. */
export async function openThread(threadId: string): Promise<Entry[]> {
  const opened = await invoke("codex_open_thread", { threadId })
  return decodeHistory(isRecord(opened) ? opened.thread : null)
}

export async function archiveThread(threadId: string): Promise<void> {
  await call("thread/archive", { threadId })
}

export async function startTurn(threadId: string, text: string): Promise<void> {
  await call("turn/start", { threadId, input: [{ type: "text", text, text_elements: [] }] })
}

export async function interruptTurn(threadId: string, turnId: string): Promise<void> {
  await call("turn/interrupt", { threadId, turnId })
}

/** The JSON-RPC result each approval kind expects for the user's choice. */
function approvalResult(approval: Approval, choice: ApprovalChoice): Record<string, unknown> {
  if (approval.kind === "permissions") {
    return choice === "decline"
      ? { permissions: {}, scope: "turn" }
      : { permissions: approval.grant, scope: choice === "session" ? "session" : "turn" }
  }
  const decision = { once: "accept", session: "acceptForSession", decline: "decline" }[choice]
  return { decision }
}

export async function answerApproval(approval: Approval, choice: ApprovalChoice): Promise<void> {
  await invoke("codex_respond", { id: approval.requestId, result: approvalResult(approval, choice) })
}

/** Answers a request this page cannot show, so Codex does not wait on it forever. */
export async function declineRequest(id: RequestId, result: Record<string, unknown>): Promise<void> {
  await invoke("codex_respond", { id, result })
}

export function onAgentEvent(handler: (event: AgentEvent) => void): () => void {
  return listen(NOTIFICATION_EVENT, (payload) => {
    const event = decodeNotification(payload)
    if (event) handler(event)
  })
}

/** Approvals for the page; user-input questions the page cannot show are answered empty. */
export function onApproval(handler: (approval: Approval) => void): () => void {
  return listen(REQUEST_EVENT, (payload) => {
    const approval = decodeRequest(payload)
    if (approval) {
      handler(approval)
      return
    }
    if (isRecord(payload) && payload.method === "item/tool/requestUserInput") {
      const id = payload.id
      if (typeof id === "string" || typeof id === "number") void declineRequest(id, { answers: {} })
    }
  })
}

export function onCodexExit(handler: () => void): () => void {
  return listen(EXIT_EVENT, handler)
}
