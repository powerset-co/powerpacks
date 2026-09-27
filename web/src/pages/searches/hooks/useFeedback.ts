import { useCallback, useEffect, useRef, useState } from "react"

import { postFeedback } from "@/lib/api/feedback"
import { flushFeedback, readQueue, writeQueue, type FeedbackOutcome } from "@/lib/searches/feedback"
import type { FeedbackRecord } from "@/types/searches"

export interface Feedback {
  /** Records not yet submitted, oldest first; `pending.length` is the count to show. */
  pending: readonly FeedbackRecord[]
  /** Queues `record`, sends the queue in order, and says whether `record` went out. */
  submit: (record: FeedbackRecord) => Promise<FeedbackOutcome>
  /** Sends the queue again. */
  retry: () => Promise<void>
}

/**
 * The feedback queue for the Searches page, mounted once. Every record is queued first
 * (localStorage, shared with results.js) and sent in order; what the server does not submit stays
 * queued and is sent again on mount, when the browser comes back online, and on `retry`.
 */
export function useFeedback(): Feedback {
  const [pending, setPending] = useState<readonly FeedbackRecord[]>(readQueue)
  const queue = useRef(pending)
  const sending = useRef<Promise<void>>(Promise.resolve())

  const commit = useCallback((next: readonly FeedbackRecord[]) => {
    queue.current = next
    writeQueue(next)
    setPending(next)
  }, [])

  // One send at a time; a record queued while one runs is kept behind what that send leaves.
  const retry = useCallback(() => {
    sending.current = sending.current.then(async () => {
      const sent = queue.current
      if (!sent.length) return
      const left = await flushFeedback(sent, postFeedback)
      commit([...left, ...queue.current.slice(sent.length)])
    })
    return sending.current
  }, [commit])

  const submit = useCallback(
    async (record: FeedbackRecord): Promise<FeedbackOutcome> => {
      commit([...queue.current, record])
      await retry()
      return queue.current.includes(record) ? "queued" : "sent"
    },
    [commit, retry],
  )

  useEffect(() => {
    const online = () => void retry()
    online()
    window.addEventListener("online", online)
    return () => window.removeEventListener("online", online)
  }, [retry])

  return { pending, submit, retry }
}
