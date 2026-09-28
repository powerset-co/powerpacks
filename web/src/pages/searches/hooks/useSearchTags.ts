import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useMemo, useRef } from "react"

import type { ToastMessage } from "@/components/shared"
import { fetchTags, writeTags } from "@/lib/api/searches"
import { browserTags, NO_TAGS, removeTag, toggleTag, untagPeople } from "@/lib/searches/tags"
import type { Tagged } from "@/types/searches"
import { errorText } from "@/lib/api/http"

export function searchTagsKey(runId: string) {
  return ["searches", "tags", runId] as const
}

/**
 * A search's tags: every edit shows at once and is saved whole, one save at a time, so a
 * later edit never lands before an earlier one (results.js writeTagged). A failed save
 * puts back the last saved tags when no newer edit followed it, and says so on the page's toast.
 */
export function useSearchTags(runId: string, onToast: (toast: ToastMessage) => void) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: searchTagsKey(runId),
    queryFn: async () => {
      const { tagged } = await fetchTags(runId)
      if (tagged) return tagged
      // results.js moved tags kept only in this browser to the server the first time it had none.
      const kept = browserTags(runId)
      if (kept.tags.length) await writeTags(runId, kept)
      return kept
    },
  })
  const saves = useRef<Promise<void>>(Promise.resolve())
  // The tags the server last accepted, the rollback target (the pane remounts per run).
  const saved = useRef<Tagged | null>(null)

  const tagged = query.data ?? NO_TAGS

  const save = useCallback(
    (edit: (current: Tagged) => Tagged): void => {
      const key = searchTagsKey(runId)
      const current = queryClient.getQueryData<Tagged>(key) ?? NO_TAGS
      saved.current ??= current
      const next = edit(current)
      if (next === current) return
      // Structural sharing may store a copy: the stored value is what a newer edit replaces.
      const shown = queryClient.setQueryData<Tagged>(key, next)
      saves.current = saves.current.then(async () => {
        try {
          await writeTags(runId, next)
          saved.current = next
        } catch (error) {
          if (queryClient.getQueryData<Tagged>(key) === shown) queryClient.setQueryData(key, saved.current)
          onToast({ message: `Tags not saved: ${errorText(error)}`, error: true })
        }
      })
    },
    [queryClient, runId, onToast],
  )

  const loading = query.isPending
  // One identity until the tags or the loading state change: every row's controls read it.
  return useMemo(
    () => ({
      tagged,
      loading,
      toggle: (personId: string, tag: string) => save((t) => toggleTag(t, personId, tag)),
      remove: (tag: string) => save((t) => removeTag(t, tag)),
      untag: (personIds: readonly string[]) => save((t) => untagPeople(t, personIds)),
      clear: () => save(() => NO_TAGS),
    }),
    [tagged, loading, save],
  )
}

export type SearchTags = ReturnType<typeof useSearchTags>
