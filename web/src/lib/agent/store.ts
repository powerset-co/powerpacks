// The Agent page's conversation: one Codex thread, kept at module level so it survives
// switching pages. `reduce` folds Codex events into the transcript; the actions below are the
// only writers.

import { useEffect, useSyncExternalStore } from "react"

import {
  answerApproval,
  interruptTurn,
  onAgentEvent,
  onApproval,
  onCodexExit,
  startThread,
  startTurn,
} from "@/lib/api/codex"
import { errorText } from "@/lib/api/http"
import type { AgentEvent, Approval, ApprovalChoice, Entry } from "@/types/agent"

export interface AgentState {
  threadId: string | null
  turnId: string | null
  running: boolean
  entries: Entry[]
  approvals: Approval[]
}

export const EMPTY_AGENT: AgentState = {
  threadId: null,
  turnId: null,
  running: false,
  entries: [],
  approvals: [],
}

let localIds = 0
function localId(prefix: string): string {
  localIds += 1
  return `${prefix}-${localIds}`
}

function upsert(entries: Entry[], entry: Entry): Entry[] {
  const index = entries.findIndex(({ id }) => id === entry.id)
  if (index === -1) return [...entries, entry]
  return entries.map((current, at) => (at === index ? entry : current))
}

function appendDelta(entry: Entry, field: "text" | "output", delta: string): Entry {
  if (field === "output" && entry.kind === "command") return { ...entry, output: entry.output + delta }
  if (field === "text" && (entry.kind === "agent" || entry.kind === "reasoning"))
    return { ...entry, text: entry.text + delta }
  return entry
}

function failed(state: AgentState, text: string): AgentState {
  return {
    ...state,
    running: false,
    turnId: null,
    entries: [...state.entries, { kind: "error", id: localId("error"), text }],
  }
}

/** The transcript after one Codex event; events for another thread change nothing. */
export function reduce(state: AgentState, event: AgentEvent): AgentState {
  if ("threadId" in event && state.threadId !== null && event.threadId !== state.threadId) return state
  switch (event.type) {
    case "turnStarted":
      return { ...state, running: true, turnId: event.turnId }
    case "turnCompleted": {
      const done = { ...state, running: false, turnId: null, approvals: [] }
      return event.error === null ? done : failed(done, event.error)
    }
    case "item":
      return { ...state, entries: upsert(state.entries, event.entry) }
    case "delta":
      return {
        ...state,
        entries: state.entries.map((entry) =>
          entry.id === event.itemId ? appendDelta(entry, event.field, event.delta) : entry,
        ),
      }
    case "error":
      return event.willRetry ? state : failed(state, event.message)
    case "loginCompleted":
    case "accountUpdated":
      return state
  }
}

let state = EMPTY_AGENT
const listeners = new Set<() => void>()

function set(next: AgentState): void {
  state = next
  for (const listener of listeners) listener()
}

let wired = false
function wire(): void {
  if (wired) return
  wired = true
  onAgentEvent((event) => set(reduce(state, event)))
  onApproval((approval) => set({ ...state, approvals: [...state.approvals, approval] }))
  onCodexExit(() => {
    if (state.threadId !== null)
      set(
        failed(
          { ...state, threadId: null, approvals: [] },
          "Codex stopped. Send again to start a new conversation.",
        ),
      )
  })
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The conversation; listening to Codex starts with the first reader. */
export function useAgent(): AgentState {
  useEffect(wire, [])
  return useSyncExternalStore(subscribe, () => state)
}

export async function send(text: string): Promise<void> {
  wire()
  set({ ...state, running: true, entries: [...state.entries, { kind: "user", id: localId("user"), text }] })
  try {
    const threadId = state.threadId ?? (await startThread())
    set({ ...state, threadId })
    await startTurn(threadId, text)
  } catch (error: unknown) {
    set(failed(state, errorText(error)))
  }
}

export async function stop(): Promise<void> {
  if (state.threadId === null || state.turnId === null) return
  await interruptTurn(state.threadId, state.turnId)
}

export async function answer(approval: Approval, choice: ApprovalChoice): Promise<void> {
  set({ ...state, approvals: state.approvals.filter(({ requestId }) => requestId !== approval.requestId) })
  try {
    await answerApproval(approval, choice)
  } catch (error: unknown) {
    set(failed(state, errorText(error)))
  }
}

/** Clears the transcript; the next message starts a new thread. */
export function newConversation(): void {
  if (!state.running) set(EMPTY_AGENT)
}
