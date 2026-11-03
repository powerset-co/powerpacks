import { useQueryClient, type QueryClient } from "@tanstack/react-query"
import { useCallback, useRef, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { deletePeople, writeWorth } from "@/lib/api/people"
import { plural } from "@/lib/copy"
import type { WorthAction } from "@/lib/people/facets"
import { EMPTY } from "@/lib/sets"
import type { Person, WorthResult } from "@/types/people"

import { UPLOAD_KEY } from "../upload/useUpload"
import { patchPeople, refetchPeople } from "./usePeopleList"

const DONE: Record<WorthAction, (count: number) => string> = {
  yes: (count) => `Kept ${plural(count, "person")}.`,
  no: (count) => `Skipped ${plural(count, "person")}.`,
}

/** The rows a worth write answered, patched in place on every loaded slice. */
export function patchWorth(client: QueryClient, results: readonly WorthResult[]): void {
  const byId = new Map(results.map((result) => [result.parent_id, result]))
  patchPeople(client, (row) => {
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
 * The human's worth call for a set of people in one write, and the delete behind a confirm. Rows are marked pending while it
 * runs, patched from the server's answer, then the list is read again (a filtered tab, the order
 * and the counts follow the write). There is no undo: a call cannot go back to the machine's, so
 * the other button is the correction. `byId` holds the loaded rows; a selection can name more.
 */
export function useDecisions(
  byId: ReadonlyMap<string, Person>,
  onWritten: (ids: readonly string[]) => void,
  onDeleted: (ids: readonly string[]) => void,
) {
  const queryClient = useQueryClient()
  const [pending, setPending] = useState<ReadonlySet<string>>(EMPTY)
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const saving = useRef(false)
  // The people a Delete press named; the bar asks, Enter deletes them, Escape forgets them.
  const [armed, setArmed] = useState<readonly string[]>([])

  /** Writes the call for `ids`; true when the server took it, false when skipped or failed. */
  const apply = useCallback(
    async (action: WorthAction, ids: readonly string[]): Promise<boolean> => {
      const targets = [...ids]
      if (saving.current || !targets.length || targets.some((id) => byId.get(id)?.in_progress)) return false
      saving.current = true
      setPending(new Set(targets))
      try {
        const results = await writeWorth(targets, action)
        patchWorth(queryClient, results)
        void refetchPeople(queryClient)
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

  /** Deletes `ids` from the store for good and drops their rows. */
  const remove = useCallback(
    async (ids: readonly string[]) => {
      const targets = [...ids]
      if (saving.current || !targets.length) return
      saving.current = true
      setPending(new Set(targets))
      try {
        const deleted = await deletePeople(targets)
        await refetchPeople(queryClient)
        void queryClient.invalidateQueries({ queryKey: UPLOAD_KEY })
        onDeleted(targets)
        setToast({ message: `Deleted ${plural(deleted, "person")} from this computer.` })
      } catch (error) {
        setToast({ message: `Couldn't delete. ${errorText(error)}`, error: true })
      } finally {
        saving.current = false
        setPending(EMPTY)
      }
    },
    [queryClient, onDeleted],
  )

  /** Deletes what the last Delete press named, if the bar is still asking. */
  const confirmDelete = useCallback(() => {
    setArmed([])
    if (armed.length) void remove(armed)
  }, [armed, remove])

  /** The question before a delete: the action bar asks it, Enter or its Delete answers. */
  const askDelete = useCallback((ids: readonly string[]) => {
    if (saving.current || !ids.length) return
    setArmed([...ids])
  }, [])

  const cancelDelete = useCallback(() => setArmed([]), [])

  // What the bar asks while a delete is armed: "Delete Ada Park?", "Delete 3 people?".
  const question = armed.length
    ? `Delete ${armed.length === 1 ? (byId.get(armed[0] ?? "")?.name ?? "") : plural(armed.length, "person")}?`
    : null

  const dismissToast = useCallback(() => setToast(null), [])

  return {
    pending,
    saving: pending.size > 0,
    apply,
    askDelete,
    confirmDelete,
    cancelDelete,
    armed: () => armed.length > 0,
    question,
    toast,
    showToast: setToast,
    dismissToast,
  }
}
