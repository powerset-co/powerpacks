// A value that survives a reload within the tab (sessionStorage). Storage can be blocked
// (private mode, quota), so a read falls back to null and a write is best effort.

/** The stored value for `key`, or null when none is stored, it is not JSON, or `parse` rejects it. */
export function readSession<T>(key: string, parse: (raw: unknown) => T | null): T | null {
  try {
    return parse(JSON.parse(sessionStorage.getItem(key) ?? "null"))
  } catch {
    return null
  }
}

export function writeSession(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Storage blocked: the value just won't survive a reload.
  }
}
