// The Powerpacks desktop app's bridge (desktop/src-tauri). Inside the app the page gets
// window.__TAURI__ (withGlobalTauri); in a plain browser it is absent and desktop-only UI hides.

interface TauriGlobal {
  core: { invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown> }
  event: {
    listen: (event: string, handler: (event: { payload: unknown }) => void) => Promise<() => void>
  }
}

declare global {
  interface Window {
    __TAURI__?: TauriGlobal
  }
}

export type DesktopPlatform = "mac" | "windows" | "linux"

/** True inside the desktop app. */
export function isDesktop(): boolean {
  return typeof window !== "undefined" && window.__TAURI__ !== undefined
}

/** The desktop app's OS, or null in a browser. */
export function desktopPlatform(): DesktopPlatform | null {
  if (!isDesktop()) return null
  const agent = navigator.userAgent
  if (agent.includes("Mac")) return "mac"
  if (agent.includes("Windows")) return "windows"
  return "linux"
}

function tauri(): TauriGlobal {
  const bridge = window.__TAURI__
  if (!bridge) throw new Error("This needs the Powerpacks desktop app.")
  return bridge
}

/** Calls a desktop command; the app's errors arrive as strings and become Errors. */
export async function invoke(command: string, args?: Record<string, unknown>): Promise<unknown> {
  try {
    return await tauri().core.invoke(command, args)
  } catch (error: unknown) {
    throw error instanceof Error ? error : new Error(String(error))
  }
}

/** Subscribes to a desktop event; returns the unsubscribe. */
export function listen(event: string, handler: (payload: unknown) => void): () => void {
  let stop: (() => void) | null = null
  let stopped = false
  void tauri()
    .event.listen(event, ({ payload }) => handler(payload))
    .then((unlisten) => {
      if (stopped) unlisten()
      else stop = unlisten
    })
  return () => {
    stopped = true
    stop?.()
  }
}
