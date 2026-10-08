import { body } from "./http"

const BATCH_DELAY_MS = 100
const MAX_BATCH_SIZE = 100
// Signed paths last 30 days; refresh well before they expire.
const CACHE_MS = 24 * 60 * 60 * 1000
const FAILURE_CACHE_MS = 30_000
const GATEWAY = "https://proxy.powerset.dev"

type Resolve = (src: string | undefined) => void
const cache = new Map<string, { promise: Promise<string | undefined>; expires: number }>()
const pending = new Map<string, Resolve>()
let scheduled = false

/** All avatars share this queue, including requests still waiting for a response. */
export function thumbnail(url: string): Promise<string | undefined> {
  const parsed = new URL(url)
  const key = `${parsed.origin}${parsed.pathname}`
  const existing = cache.get(key)
  if (existing && existing.expires > Date.now()) return existing.promise

  const promise = new Promise<string | undefined>((resolve) => pending.set(key, resolve))
  cache.set(key, { promise, expires: Date.now() + CACHE_MS })
  schedule()
  return promise
}

function schedule() {
  if (scheduled || pending.size === 0) return
  scheduled = true
  setTimeout(() => void flush(), BATCH_DELAY_MS)
}

async function flush() {
  const batch = [...pending.entries()].slice(0, MAX_BATCH_SIZE)
  for (const [url] of batch) pending.delete(url)
  try {
    const response = await fetch("/api/profile-image/sign/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: batch.map(([url]) => url) }),
    })
    if (!response.ok) throw new Error("Couldn't load thumbnails")
    const { paths } = await body<{ paths: (string | null)[] }>(response)
    batch.forEach(([, resolve], index) => resolve(paths[index] ? GATEWAY + paths[index] : undefined))
  } catch {
    for (const [url, resolve] of batch) {
      const entry = cache.get(url)
      if (entry) entry.expires = Date.now() + FAILURE_CACHE_MS
      resolve(undefined)
    }
  } finally {
    scheduled = false
    schedule()
  }
}
