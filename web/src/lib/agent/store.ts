// The Agent page's open chat: one Codex thread, kept at module level so it survives switching
// pages. `reduce` folds Codex events into the transcript; the actions below are the only writers.

import { useEffect, useSyncExternalStore } from "react"

import {
  answerApproval,
  interruptTurn,
  onAgentEvent,
  onApproval,
  onCodexExit,
  openThread,
  startThread,
  startTurn,
} from "@/lib/api/codex"
import { errorText } from "@/lib/api/http"
import type { AgentEvent, Approval, ApprovalChoice, Entry } from "@/types/agent"

export interface AgentState {
  threadId: string | null
  turnId: string | null
  running: boolean
  /** True while a past chat's history loads. */
  loading: boolean
  entries: Entry[]
  approvals: Approval[]
  /** Commands run without asking; remembered on this Mac. */
  fullAccess: boolean
}

export const EMPTY_AGENT: AgentState = {
  threadId: null,
  turnId: null,
  running: false,
  loading: false,
  entries: [],
  approvals: [],
  fullAccess: false,
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
    loading: false,
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

const FULL_ACCESS_KEY = "powerpacks.agent.fullAccess"

function rememberedFullAccess(): boolean {
  try {
    return localStorage.getItem(FULL_ACCESS_KEY) === "1"
  } catch {
    return false
  }
}

let state: AgentState = { ...EMPTY_AGENT, fullAccess: rememberedFullAccess() }
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
  onApproval((approval) => {
    // A turn started before Full access was switched on still asks; approve it for the chat.
    if (state.fullAccess) void answerApproval(approval, "session")
    else set({ ...state, approvals: [...state.approvals, approval] })
  })
  onCodexExit(() => {
    if (state.threadId !== null)
      set(
        failed({ ...state, threadId: null, approvals: [] }, "Codex stopped. Send again to start a new chat."),
      )
  })
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The open chat; listening to Codex starts with the first reader. */
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
    await startTurn(threadId, text, state.fullAccess)
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

/** Turning it on also approves whatever is already waiting; it applies from the next message. */
export async function setFullAccess(on: boolean): Promise<void> {
  const waiting = on ? state.approvals : []
  set({ ...state, fullAccess: on })
  try {
    localStorage.setItem(FULL_ACCESS_KEY, on ? "1" : "")
  } catch {
    // Storage can be unavailable; the switch still holds for this session.
  }
  for (const approval of waiting) await answer(approval, "session")
}

/** Clears the screen; the next message starts a new thread. A running turn carries on in Codex. */
export function newChat(): void {
  set({ ...EMPTY_AGENT, fullAccess: state.fullAccess })
}

/** Opens a past chat with its history; later messages continue it. */
export async function openChat(threadId: string): Promise<void> {
  if (threadId === state.threadId) return
  wire()
  set({ ...EMPTY_AGENT, fullAccess: state.fullAccess, threadId, loading: true })
  try {
    const entries = await openThread(threadId)
    if (state.threadId === threadId) set({ ...state, entries, loading: false })
  } catch (error: unknown) {
    if (state.threadId === threadId) set(failed(state, errorText(error)))
  }
}
