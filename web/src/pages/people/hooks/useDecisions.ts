import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useRef, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { writeWorth } from "@/lib/api/people"
import { plural } from "@/lib/copy"
import type { WorthAction } from "@/lib/people/facets"
import { EMPTY } from "@/lib/sets"
import type { Person, WorthResult } from "@/types/people"

import { UPLOAD_KEY } from "../upload/useUpload"
import { PEOPLE_QUERY_KEY } from "./usePeopleQuery"

const DONE: Record<WorthAction, (count: number) => string> = {
  yes: (count) => `Marked ${plural(count, "person")} worth it.`,
  no: (count) => `Marked ${plural(count, "person")} not worth it.`,
}

function patchRows(rows: Person[] | undefined, results: readonly WorthResult[]): Person[] | undefined {
  if (!rows) return rows
  const byId = new Map(results.map((result) => [result.parent_id, result]))
  return rows.map((row) => {
    const patch = byId.get(row.parent_id)
    if (!patch) return row
    return {
      ...row,
      worth: patch.worth,
      worth_source: patch.worth_source,
      reason: patch.reason,
      tags: patch.tags,
    }
  })
}

/**
 * The human's worth call for a set of people in one write. Rows are marked pending while it
 * runs and patched from the server's answer. There is no undo: a call cannot go back to the
 * machine's, so the other button is the correction.
 */
export function useDecisions(byId: ReadonlyMap<string, Person>, onWritten: (ids: readonly string[]) => void) {
  const queryClient = useQueryClient()
  const [pending, setPending] = useState<ReadonlySet<string>>(EMPTY)
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const saving = useRef(false)

  /** Writes the call for `ids`; true when the server took it, false when skipped or failed. */
  const apply = useCallback(
    async (action: WorthAction, ids: readonly string[]): Promise<boolean> => {
      const rows = ids.map((id) => byId.get(id)).filter((row): row is Person => row !== undefined)
      if (saving.current || !rows.length || rows.some((row) => row.in_progress)) return false
      const targets = rows.map((row) => row.parent_id)
      saving.current = true
      setPending(new Set(targets))
      try {
        const results = await writeWorth(targets, action)
        queryClient.setQueryData<Person[]>(PEOPLE_QUERY_KEY, (current) => patchRows(current, results))
        // The upload list follows worth: the upload status says so and drops a stale check.
        void queryClient.invalidateQueries({ queryKey: UPLOAD_KEY })
        onWritten(targets)
        setToast({ message: DONE[action](results.length) })
        return true
      } catch (error) {
        setToast({ message: `Couldn't save changes. ${errorText(error)}`, error: true })
        return false
      } finally {
        saving.current = false
        setPending(EMPTY)
      }
    },
    [byId, queryClient, onWritten],
  )

  const dismissToast = useCallback(() => setToast(null), [])

  return { pending, saving: pending.size > 0, apply, toast, showToast: setToast, dismissToast }
}
