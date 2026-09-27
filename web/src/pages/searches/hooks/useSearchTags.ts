import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useRef, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { fetchTags, writeTags } from "@/lib/api/searches"
import { NO_TAGS, removeTag, toggleTag, untagPeople } from "@/lib/searches/tags"
import type { Tagged } from "@/types/searches"

export function searchTagsKey(runId: string) {
  return ["searches", "tags", runId] as const
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * A search's tags: every edit shows at once and is saved whole, one save at a time, so a
 * later edit never lands before an earlier one (results.js writeTagged). A failed save
 * puts back the last saved tags when no newer edit followed it.
 */
export function useSearchTags(runId: string) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: searchTagsKey(runId),
    queryFn: async () => (await fetchTags(runId)).tagged ?? NO_TAGS,
  })
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const saves = useRef<Promise<unknown>>(Promise.resolve())
  // Per run: the tags the server last accepted, the rollback target.
  const saved = useRef(new Map<string, Tagged>())

  const tagged = query.data ?? NO_TAGS

  const save = useCallback(
    (edit: (current: Tagged) => Tagged): void => {
      const key = searchTagsKey(runId)
      const current = queryClient.getQueryData<Tagged>(key) ?? NO_TAGS
      if (!saved.current.has(runId)) saved.current.set(runId, current)
      const next = edit(current)
      if (next === current) return
      // Structural sharing may store a copy: the stored value is what a newer edit replaces.
      const shown = queryClient.setQueryData<Tagged>(key, next)
      saves.current = saves.current.then(async () => {
        try {
          await writeTags(runId, next)
          saved.current.set(runId, next)
        } catch (error) {
          if (queryClient.getQueryData<Tagged>(key) === shown)
            queryClient.setQueryData(key, saved.current.get(runId))
          setToast({ message: `Tags not saved: ${errorText(error)}`, error: true })
        }
      })
    },
    [queryClient, runId],
  )

  return {
    tagged,
    loading: query.isPending,
    toggle: useCallback((personId: string, tag: string) => save((t) => toggleTag(t, personId, tag)), [save]),
    remove: useCallback((tag: string) => save((t) => removeTag(t, tag)), [save]),
    untag: useCallback((personIds: readonly string[]) => save((t) => untagPeople(t, personIds)), [save]),
    clear: useCallback(() => save(() => NO_TAGS), [save]),
    toast,
    dismissToast: useCallback(() => setToast(null), []),
  }
}

export type SearchTags = ReturnType<typeof useSearchTags>
