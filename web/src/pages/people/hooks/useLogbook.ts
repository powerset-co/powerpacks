import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { buildLogbook, readLogbook, type LogbookStatus } from "@/lib/api/logbook"
import { logbookDoneToast, logbookStartedToast } from "@/lib/people/copy"

const LOGBOOK_KEY = ["people-logbook"]
/** Every read of the saved archive (pages/people/logbook): a finished build refreshes them. */
export const SAVED_LOGBOOK_KEY = ["logbook"] as const
const POLL_MS = 1000

/** The toast a finished build leaves: what it saved and each channel it could not read. */
function finished(status: LogbookStatus): ToastMessage | null {
  if (status.status === "failed") {
    return { message: `Couldn't build the logbook. ${status.error ?? ""}`.trim(), error: true }
  }
  return status.status === "completed" && status.result ? { message: logbookDoneToast(status.result) } : null
}

/**
 * Builds the raw local logbook for a set of people: one build at a time, its status polled
 * every second while it runs and never otherwise. Opening the page only reads the status.
 * A finished build refreshes the saved logbooks (and so the rows' Logbook column), then opens what
 * it saved (`onBuilt`); selection, rows and tags are left as they are.
 */
export function useLogbook(onToast: (toast: ToastMessage) => void, onBuilt: (entries: string[]) => void) {
  const client = useQueryClient()
  const [starting, setStarting] = useState(false)

  const settle = useCallback(
    (status: LogbookStatus) => {
      const done = finished(status)
      if (done) onToast(done)
      if (status.status !== "completed") return
      void client.invalidateQueries({ queryKey: SAVED_LOGBOOK_KEY })
      if (status.result?.entries.length) onBuilt(status.result.entries)
    },
    [client, onToast, onBuilt],
  )

  const query = useQuery({
    queryKey: LOGBOOK_KEY,
    queryFn: async () => {
      const before = client.getQueryData<LogbookStatus>(LOGBOOK_KEY)
      const now = await readLogbook()
      if (before?.status === "building") settle(now)
      return now
    },
    refetchInterval: (latest) => (latest.state.data?.status === "building" ? POLL_MS : false),
    retry: false,
    staleTime: Infinity,
  })
  const building = starting || query.data?.status === "building"

  const build = useCallback(
    async (people: readonly string[]) => {
      if (building || !people.length) return
      setStarting(true)
      try {
        await client.cancelQueries({ queryKey: LOGBOOK_KEY })
        const status = await buildLogbook(people)
        client.setQueryData(LOGBOOK_KEY, status)
        if (status.status === "building") onToast({ message: logbookStartedToast(people.length) })
        else settle(status)
      } catch (error) {
        onToast({ message: `Couldn't build the logbook. ${errorText(error)}`, error: true })
      } finally {
        setStarting(false)
      }
    },
    [building, client, onToast, settle],
  )

  return { building, build }
}
