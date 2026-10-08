// The chat sidebar's date groups, newest first, as chat apps show them.

import type { ThreadSummary } from "@/types/agent"

const DAY_MS = 86_400_000
// Each group holds chats newer than its age in days; the last holds the rest.
const GROUPS = [
  { label: "Today", days: 0 },
  { label: "Yesterday", days: 1 },
  { label: "Previous 7 days", days: 7 },
  { label: "Previous 30 days", days: 30 },
  { label: "Older", days: Number.POSITIVE_INFINITY },
] as const

export interface ThreadGroup {
  label: string
  threads: ThreadSummary[]
}

export function groupThreads(threads: ThreadSummary[], now: Date): ThreadGroup[] {
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const groups: ThreadGroup[] = GROUPS.map(({ label }) => ({ label, threads: [] }))
  for (const thread of [...threads].sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime())) {
    const age = midnight - thread.updatedAt.getTime()
    const index = GROUPS.findIndex(({ days }) => age <= days * DAY_MS)
    groups[index === -1 ? GROUPS.length - 1 : index]?.threads.push(thread)
  }
  return groups.filter(({ threads: list }) => list.length > 0)
}
