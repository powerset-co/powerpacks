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
import { readStored, writeStored } from "@/lib/storage"
import type { AgentEvent, Approval, ApprovalChoice, Entry } from "@/types/agent"
import type { ChatScope } from "@/types/searches"

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
  /** A saved search opened from the sidebar; the chat's first message is about it. */
  searchRun: string | null
  /** The model and effort the next turn runs with; null is Codex's default. Remembered on this Mac. */
  model: string | null
  effort: string | null
  /** Where this chat searches, pinned at its first message: a set or this Mac's local index. Null
   *  until picked (a new chat takes the last pick), or for a chat from before scopes. */
  scope: ChatScope | null
}

export const EMPTY_AGENT: AgentState = {
  threadId: null,
  turnId: null,
  running: false,
  loading: false,
  entries: [],
  approvals: [],
  fullAccess: false,
  searchRun: null,
  model: null,
  effort: null,
  scope: null,
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
const MODEL_KEY = "powerpacks.agent.model"
const EFFORT_KEY = "powerpacks.agent.effort"
// The open chat, so leaving the page or relaunching the app reopens it.
const OPEN_CHAT_KEY = "powerpacks.agent.openChat"
// The last chat's scope, so a new chat starts where the last one searched.
const SCOPE_KEY = "powerpacks.agent.scope"

function recalledScope(): ChatScope | null {
  try {
    const raw: unknown = JSON.parse(recalled(SCOPE_KEY) || "null")
    if (raw && typeof raw === "object" && "set_id" in raw && typeof raw.set_id === "string")
      return { set_id: raw.set_id }
    if (raw && typeof raw === "object" && "local_db" in raw && typeof raw.local_db === "string")
      return { local_db: raw.local_db }
  } catch {
    // An unreadable pick is no pick.
  }
  return null
}

function remember(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Storage can be unavailable; the value still holds for this session.
  }
}

function recalled(key: string): string {
  try {
    return localStorage.getItem(key) ?? ""
  } catch {
    return ""
  }
}

let state: AgentState = {
  ...EMPTY_AGENT,
  fullAccess: recalled(FULL_ACCESS_KEY) === "1",
  scope: recalledScope(),
  model: recalled(MODEL_KEY) || null,
  effort: recalled(EFFORT_KEY) || null,
}
const listeners = new Set<() => void>()

function set(next: AgentState): void {
  state = next
  for (const listener of listeners) listener()
}

let wired = false
function wire(): void {
  if (wired) return
  wired = true
  const reopen = recalled(OPEN_CHAT_KEY)
  if (reopen && state.threadId === null) void openChat(reopen)
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

export async function send(message: string): Promise<void> {
  wire()
  // A chat opened from a saved search names its folder, so Codex reads it and the chat keeps it.
  const text =
    state.threadId === null && state.searchRun !== null
      ? `About the search saved in .powerpacks/deep-search/${state.searchRun}: ${message}`
      : message
  set({ ...state, running: true, entries: [...state.entries, { kind: "user", id: localId("user"), text }] })
  try {
    const threadId = state.threadId ?? (await startThread(state.scope))
    set({ ...state, threadId })
    remember(OPEN_CHAT_KEY, threadId)
    if (state.searchRun) writeStored("local", `search-thread:${state.searchRun}`, threadId)
    await startTurn(threadId, text, state.fullAccess, { model: state.model, effort: state.effort })
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
/** The model for the next turns; the effort resets to that model's own default. */
export function setModel(model: string | null): void {
  set({ ...state, model, effort: null })
  remember(MODEL_KEY, model ?? "")
  remember(EFFORT_KEY, "")
}

export function setEffort(effort: string | null): void {
  set({ ...state, effort })
  remember(EFFORT_KEY, effort ?? "")
}

export async function setFullAccess(on: boolean): Promise<void> {
  const waiting = on ? state.approvals : []
  set({ ...state, fullAccess: on })
  remember(FULL_ACCESS_KEY, on ? "1" : "")
  for (const approval of waiting) await answer(approval, "session")
}

/** Where the next chat searches; a chat that has started (or is sending its first message) keeps its scope. */
export function setScope(scope: ChatScope): void {
  if (state.threadId !== null || state.running) return
  set({ ...state, scope })
  remember(SCOPE_KEY, JSON.stringify(scope))
}

/** Clears the screen; the next message starts a new thread. A running turn carries on in Codex. */
export function newChat(): void {
  set({ ...EMPTY_AGENT, fullAccess: state.fullAccess, scope: recalledScope() })
  remember(OPEN_CHAT_KEY, "")
}

/** Shows a saved search beside a new chat about it. */
export function openSearch(runId: string): void {
  set({ ...EMPTY_AGENT, fullAccess: state.fullAccess, searchRun: runId, scope: recalledScope() })
  remember(OPEN_CHAT_KEY, "")
  const saved = readStored("local", `search-thread:${runId}`, (value) =>
    typeof value === "string" ? value : null,
  )
  if (saved) void openChat(saved, runId)
}

/** Opens a past chat with its history; later messages continue it. */
export async function openChat(threadId: string, searchRun: string | null = null): Promise<void> {
  if (threadId === state.threadId) return
  wire()
  set({ ...EMPTY_AGENT, fullAccess: state.fullAccess, threadId, searchRun, loading: true })
  remember(OPEN_CHAT_KEY, threadId)
  try {
    const { entries, scope } = await openThread(threadId)
    if (state.threadId === threadId) set({ ...state, entries, scope, loading: false })
  } catch (error: unknown) {
    if (state.threadId === threadId) set(failed(state, errorText(error)))
  }
}
