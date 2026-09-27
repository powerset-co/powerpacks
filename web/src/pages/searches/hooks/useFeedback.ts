import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { postFeedback, signIn as signInToPowerset } from "@/lib/api/feedback"
import {
  flushFeedback,
  readQueue,
  writeQueue,
  type FeedbackFailure,
  type FeedbackOutcome,
} from "@/lib/searches/feedback"
import type { FeedbackRecord } from "@/types/searches"

export interface Feedback {
  /** Records not yet submitted, every run's, oldest first. */
  pending: readonly FeedbackRecord[]
  /** Why the open run's queue stopped; while set, new records wait for `retry` or `signIn`. */
  failure: FeedbackFailure | null
  /** Queues `record`, sends the open run's queue unless it is stopped, says whether it went out. */
  submit: (record: FeedbackRecord) => Promise<FeedbackOutcome>
  /** Sends the open run's queue again. */
  retry: () => Promise<void>
  /** Signs in to Powerset through the server, then retries; throws when the sign-in failed. */
  signIn: () => Promise<void>
}

/**
 * The feedback queue for the Searches page, mounted once. Every record is queued first
 * (localStorage, shared with results.js) and the open run's are sent in order, as results.js
 * sent them. A record the server does not submit stops the queue, and it waits, visibly, for
 * Retry or a sign-in; opening a run and the browser coming back online retry it.
 */
export function useFeedback(runId: string | null): Feedback {
  const [pending, setPending] = useState<readonly FeedbackRecord[]>(readQueue)
  const [failure, setFailure] = useState<FeedbackFailure | null>(null)
  const queue = useRef(pending)
  const stopped = useRef<FeedbackFailure | null>(null)
  const sending = useRef<Promise<void>>(Promise.resolve())
  const retried = useRef<string | null>(null)

  const commit = useCallback((next: readonly FeedbackRecord[]) => {
    queue.current = next
    writeQueue(next)
    setPending(next)
  }, [])

  const stop = useCallback((next: FeedbackFailure | null) => {
    stopped.current = next
    setFailure(next)
  }, [])

  // One send at a time; a record queued while one runs is kept behind what that send leaves.
  const send = useCallback(() => {
    sending.current = sending.current.then(async () => {
      const sent = queue.current
      if (runId === null || stopped.current || !sent.some((record) => record.run_id === runId)) return
      const { left, failure: next } = await flushFeedback(sent, runId, postFeedback)
      commit([...left, ...queue.current.slice(sent.length)])
      stop(next)
    })
    return sending.current
  }, [runId, commit, stop])

  const retry = useCallback(() => {
    stop(null)
    return send()
  }, [stop, send])

  const submit = useCallback(
    async (record: FeedbackRecord): Promise<FeedbackOutcome> => {
      commit([...queue.current, record])
      await send()
      return queue.current.includes(record) ? "queued" : "sent"
    },
    [commit, send],
  )

  const signIn = useCallback(async () => {
    await signInToPowerset()
    await retry()
  }, [retry])

  // Each run opened gets one fresh try (StrictMode's second mount does not post again).
  useEffect(() => {
    if (retried.current === runId) return
    retried.current = runId
    void retry()
  }, [runId, retry])

  useEffect(() => {
    const online = () => void retry()
    window.addEventListener("online", online)
    return () => window.removeEventListener("online", online)
  }, [retry])

  return useMemo(
    () => ({ pending, failure, submit, retry, signIn }),
    [pending, failure, submit, retry, signIn],
  )
}
