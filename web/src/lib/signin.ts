// The desktop app's sign-in modal (SignInModal): a provider's own sign-in page in a native
// view placed over the app (desktop/src-tauri/src/signin.rs). One sign-in at a time. The modal
// opens the view once it has measured its body, and moves it as the window resizes.

import { useEffect, useSyncExternalStore } from "react"

import { invoke, isDesktop, listen } from "@/lib/desktop"
import { isRecord } from "@/lib/utils"

const FINISHED_EVENT = "signin://finished"

/** Where the Powerset login's own callback server listens (packs/powerset/primitives/auth/auth.py):
 *  a Powerset sign-in finishes there. */
export const POWERSET_CALLBACK = "http://localhost:9876/callback"

export interface SignIn {
  /** Who the user is signing in to, for the header. */
  title: string
  url: string
  /** The URL the sign-in ends on: the modal closes there, or reads LinkedIn first. */
  finish: string
  /** What happens at `finish`: close, or read the LinkedIn list in the view before closing. */
  then?: "close" | "linkedin"
  /** After a LinkedIn read: resume setup. */
  onDone?: () => void
}

/** A LinkedIn read in progress, shown in the modal header. */
export interface Reading {
  read: number
  total: number
}

const LINKEDIN_PROGRESS = "linkedin://progress"
let reading: Reading | null = null

export interface Bounds {
  x: number
  y: number
  width: number
  height: number
}

let current: SignIn | null = null
const listeners = new Set<() => void>()
// Who waits on the open sign-in's outcome (signInFinished).
const waiters = new Set<(reached: boolean) => void>()

function set(next: SignIn | null): void {
  current = next
  for (const listener of listeners) listener()
}

function settle(reached: boolean): void {
  for (const waiter of waiters) waiter(reached)
  waiters.clear()
}

let wired = false
function wire(): void {
  if (wired || !isDesktop()) return
  wired = true
  listen(FINISHED_EVENT, () => void finished())
  listen(LINKEDIN_PROGRESS, (payload) => {
    if (isRecord(payload) && typeof payload.read === "number" && typeof payload.total === "number") {
      reading = { read: payload.read, total: payload.total }
      for (const listener of listeners) listener()
    }
  })
}

/** The sign-in reached its callback: close, or read LinkedIn's list first. */
async function finished(): Promise<void> {
  const signIn = current
  if (signIn === null) return
  settle(true)
  if (signIn.then !== "linkedin" || reading !== null) {
    await closeSignIn()
    return
  }
  reading = { read: 0, total: 0 }
  for (const listener of listeners) listener()
  try {
    await invoke("linkedin_read")
    await closeSignIn()
    signIn.onDone?.()
  } catch (error: unknown) {
    reading = null
    await closeSignIn()
    throw error instanceof Error ? error : new Error(String(error))
  }
}

export function useReading(): Reading | null {
  return useSyncExternalStore(subscribe, () => reading)
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The open sign-in, or null. */
export function useSignIn(): SignIn | null {
  useEffect(wire, [])
  return useSyncExternalStore(subscribe, () => current)
}

/** Opens the modal; its body, once laid out, shows the page. */
export function openSignIn(signIn: SignIn): void {
  wire()
  set(signIn)
}

/** Resolves when the open sign-in ends: true as it reaches its callback, false when it is
 *  closed first. False at once with no sign-in open. */
export function signInFinished(): Promise<boolean> {
  if (current === null) return Promise.resolve(false)
  return new Promise((resolve) => waiters.add(resolve))
}

/** The modal's body is laid out: show the page there. */
export function showSignIn(signIn: SignIn, bounds: Bounds): Promise<void> {
  return invoke("signin_open", { url: signIn.url, finish: signIn.finish, bounds }).then(() => undefined)
}

export function placeSignIn(bounds: Bounds): Promise<void> {
  return invoke("signin_place", { bounds }).then(() => undefined)
}

export async function closeSignIn(): Promise<void> {
  if (current === null) return
  reading = null
  set(null)
  settle(false)
  await invoke("signin_close")
}

/** The provider's host, for the modal's footer. */
export function signInHost(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return ""
  }
}
