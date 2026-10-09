// The desktop app's sign-in modal (SignInModal): a provider's own sign-in page in a native
// view placed over the app (desktop/src-tauri/src/signin.rs). One sign-in at a time. The modal
// opens the view once it has measured its body, and moves it as the window resizes. Only the
// Powerset login runs here; LinkedIn and Google sign in through Chrome (Playwright).

import { useEffect, useSyncExternalStore } from "react"

import { invoke, isDesktop, listen } from "@/lib/desktop"

const FINISHED_EVENT = "signin://finished"

/** Where the Powerset login's own callback server listens (packs/powerset/primitives/auth/auth.py):
 *  a Powerset sign-in finishes there. */
export const POWERSET_CALLBACK = ["http://localhost:9876/callback"]

export interface SignIn {
  /** Who the user is signing in to, for the header. */
  title: string
  url: string
  /** The URL prefixes the sign-in ends on: the modal closes there. */
  finish: string[]
}

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
}

/** The sign-in reached its callback: close. */
async function finished(): Promise<void> {
  if (current === null) return
  settle(true)
  await closeSignIn()
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
