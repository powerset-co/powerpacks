// A value kept in the browser: "session" survives a reload within the tab, "local" survives the
// browser. Storage can be blocked (private mode, quota), so a read falls back to null and a
// write is best effort.

export type Store = "session" | "local"

function storage(store: Store): Storage {
  return store === "session" ? sessionStorage : localStorage
}

/** The stored value for `key`, or null when none is stored, it is not JSON, or `parse` rejects it. */
export function readStored<T>(store: Store, key: string, parse: (raw: unknown) => T | null): T | null {
  try {
    return parse(JSON.parse(storage(store).getItem(key) ?? "null"))
  } catch {
    return null
  }
}

export function writeStored(store: Store, key: string, value: unknown): void {
  try {
    storage(store).setItem(key, JSON.stringify(value))
  } catch {
    // Storage blocked: the value just won't survive a reload.
  }
}
