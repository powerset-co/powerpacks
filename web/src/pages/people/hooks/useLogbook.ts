import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { LOGBOOK_DOWNLOAD_URL, buildLogbook, readLogbook, type LogbookStatus } from "@/lib/api/logbook"
import { LOGBOOK, logbookDoneToast, logbookStartedToast } from "@/lib/people/copy"

const LOGBOOK_KEY = ["people-logbook"]
const POLL_MS = 1000

/** The toast a finished build leaves: what it saved and where, each channel it could not read,
 *  and the download when it wrote files. */
function finished(status: LogbookStatus): ToastMessage | null {
  if (status.status === "failed") {
    return { message: `Couldn't build the logbook. ${status.error ?? ""}`.trim(), error: true }
  }
  const result = status.result
  if (status.status !== "completed" || !result) return null
  return {
    message: logbookDoneToast(result),
    action: result.files
      ? { label: LOGBOOK.download, onClick: () => window.location.assign(LOGBOOK_DOWNLOAD_URL) }
      : undefined,
  }
}

/**
 * Builds the raw local logbook for a set of people: one build at a time, its status polled
 * every second while it runs and never otherwise. Opening the page only reads the status.
 * Selection, rows and tags are left as they are.
 */
export function useLogbook(onToast: (toast: ToastMessage) => void) {
  const client = useQueryClient()
  const [starting, setStarting] = useState(false)
  const query = useQuery({
    queryKey: LOGBOOK_KEY,
    queryFn: async () => {
      const before = client.getQueryData<LogbookStatus>(LOGBOOK_KEY)
      const now = await readLogbook()
      const done = before?.status === "building" ? finished(now) : null
      if (done) onToast(done)
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
        onToast(finished(status) ?? { message: logbookStartedToast(people.length) })
      } catch (error) {
        onToast({ message: `Couldn't build the logbook. ${errorText(error)}`, error: true })
      } finally {
        setStarting(false)
      }
    },
    [building, client, onToast],
  )

  return { building, build, result: query.data?.status === "completed" ? query.data.result : null }
}
