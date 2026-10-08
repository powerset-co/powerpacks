// The desktop app's sign-in pane (desktop/src-tauri/src/signin.rs): a provider's own sign-in
// page docked inside the window, under the strip SignInBar renders. One pane at a time.

import { useEffect, useSyncExternalStore } from "react"

import { invoke, isDesktop, listen } from "@/lib/desktop"

const FINISHED_EVENT = "signin://finished"

export interface SignIn {
  /** Who the user is signing in to, for the strip. */
  title: string
  url: string
  /** The callback URL the sign-in ends on; reaching it closes the pane. */
  finish: string
}

let current: SignIn | null = null
const listeners = new Set<() => void>()

function set(next: SignIn | null): void {
  current = next
  for (const listener of listeners) listener()
}

let wired = false
function wire(): void {
  if (wired || !isDesktop()) return
  wired = true
  listen(FINISHED_EVENT, () => void closeSignIn())
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

export async function openSignIn(signIn: SignIn): Promise<void> {
  wire()
  await invoke("signin_open", { url: signIn.url, finish: signIn.finish })
  set(signIn)
}

export async function closeSignIn(): Promise<void> {
  if (current === null) return
  set(null)
  await invoke("signin_close")
}
