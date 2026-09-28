// Feedback on a search or one of its candidates: the record POST /searches/feedback accepts
// (results_web/server.py _save_feedback), and the queue of records not yet sent. The queue lives
// in localStorage under the key results.js uses, stored as the same form values, so either page
// replays what the other queued.

import type {
  Candidate,
  FeedbackRecord,
  FeedbackReply,
  HumanJudgment,
  Ratings,
  SearchRunPayload,
} from "@/types/searches"

import { readStored, writeStored } from "@/lib/storage"
import { isRecord } from "@/lib/utils"

import type { ScoreOf } from "./filters"
import type { ResultRow } from "./ranking"

export const FEEDBACK_STORAGE_KEY = "powerpacks:pending-feedback:v1"

export const SCORE_SCALE = 5

// human_ratings.convert_rating: a judgment without a scale was saved on the old ten-point rubric.
const LEGACY_SCALE = 10

export type FeedbackOutcome = "sent" | "queued"

export const OUTCOME_MESSAGE: Record<FeedbackOutcome, string> = {
  sent: "Sent.",
  queued: "Saved on this device.",
}

/** Why the queue stopped: Powerset needs a sign-in, or anything else (network, server). */
export type FeedbackFailure = "needs_auth" | "failed"

// The sender's status when Powerset has no usable sign-in (results_web/server.py _save_feedback).
const NEEDS_AUTH = "needs_auth"

export function buildScoreFeedback(
  runId: string,
  candidate: Pick<Candidate, "person_id">,
  score: number,
  note: string,
): FeedbackRecord {
  return {
    run_id: runId,
    person_id: candidate.person_id,
    comment: note.trim(),
    human_judgment: { score, scale: SCORE_SCALE },
  }
}

export function buildSearchFeedback(runId: string, comment: string): FeedbackRecord {
  return { run_id: runId, person_id: "", comment: comment.trim(), human_judgment: null }
}

/** A note about one candidate with no score: the drawer's flag. */
export function buildPersonFeedback(
  runId: string,
  candidate: Pick<Candidate, "person_id">,
  comment: string,
): FeedbackRecord {
  return { run_id: runId, person_id: candidate.person_id, comment: comment.trim(), human_judgment: null }
}

/** The form fields the server reads; `human_judgment` is left out for search feedback, as results.js does. */
export function formValues(record: FeedbackRecord): Record<string, string> {
  const values = { run_id: record.run_id, person_id: record.person_id, comment: record.comment }
  return record.human_judgment ? { ...values, human_judgment: JSON.stringify(record.human_judgment) } : values
}

/** A judgment's score on the five-point rubric; a ten-point score goes through `ratings.legacy`. */
export function fiveScore(judgment: HumanJudgment, legacy: Ratings["legacy"]): number | null {
  if (judgment.scale === SCORE_SCALE) return judgment.score
  return legacy[String(judgment.score)] ?? null
}

export interface QueuedScore {
  score: number
  note: string
}

/** The scores still queued for one run by person, the latest winning: what the rows show until sent. */
export function queuedScores(
  pending: readonly FeedbackRecord[],
  runId: string,
  legacy: Ratings["legacy"],
): Map<string, QueuedScore> {
  const scores = new Map<string, QueuedScore>()
  for (const record of pending) {
    if (record.run_id !== runId || !record.human_judgment) continue
    const score = fiveScore(record.human_judgment, legacy)
    if (score !== null) scores.set(record.person_id, { score, note: record.comment })
  }
  return scores
}

/** The person's own score: one still queued (the newest), else the one on file. */
export function yourScore(
  row: Pick<ResultRow, "row" | "candidate">,
  queued: ReadonlyMap<string, QueuedScore>,
): QueuedScore | null {
  const waiting = queued.get(row.row.person_id)
  if (waiting) return waiting
  const filed = row.candidate?.human_score ?? null
  return filed === null ? null : { score: filed, note: row.candidate?.human_note ?? "" }
}

/** What export and copy compare and write: the person's own score, else the overall. */
export function exportScoreOf(queued: ReadonlyMap<string, QueuedScore>): ScoreOf {
  return (row) => yourScore(row, queued)?.score ?? row.overall
}

/** The run as the server will return it once `record` (a score) is stored: the row shows it at once. */
export function withScore(payload: SearchRunPayload, record: FeedbackRecord): SearchRunPayload {
  const judgment = record.human_judgment
  if (!judgment) return payload
  const candidates = payload.search.candidates.map((candidate) =>
    candidate.person_id === record.person_id
      ? { ...candidate, human_score: fiveScore(judgment, payload.ratings.legacy), human_note: record.comment }
      : candidate,
  )
  return { ...payload, search: { ...payload.search, candidates } }
}

function failureOf(reply: FeedbackReply): FeedbackFailure | null {
  if (reply.status === "submitted") return null
  return reply.api.status === NEEDS_AUTH ? "needs_auth" : "failed"
}

export interface Flushed {
  left: FeedbackRecord[]
  failure: FeedbackFailure | null
}

/**
 * Posts `runId`'s records in queue order, as results.js sent only the open run's, and returns what
 * is left: every other run's records, and from the first record the server did not submit on, the
 * run's rest, so a later score for a person never lands before an earlier one.
 */
export async function flushFeedback(
  queue: readonly FeedbackRecord[],
  runId: string,
  post: (record: FeedbackRecord) => Promise<FeedbackReply>,
): Promise<Flushed> {
  const sent = new Set<FeedbackRecord>()
  let failure: FeedbackFailure | null = null
  for (const record of queue) {
    if (record.run_id !== runId) continue
    try {
      failure = failureOf(await post(record))
    } catch {
      failure = "failed"
    }
    if (failure) break
    sent.add(record)
  }
  return { left: queue.filter((record) => !sent.has(record)), failure }
}

function parseJudgment(raw: unknown): HumanJudgment | null | undefined {
  if (raw === undefined || raw === "") return null
  if (typeof raw !== "string") return undefined
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return undefined
  }
  if (!isRecord(value) || typeof value.score !== "number") return undefined
  const scale = value.scale ?? LEGACY_SCALE
  if (scale !== SCORE_SCALE && scale !== LEGACY_SCALE) return undefined
  return { score: value.score, scale }
}

/** One stored entry as a record, or null when it is not one results.js or this page wrote. */
export function parseQueued(raw: unknown): FeedbackRecord | null {
  if (!isRecord(raw)) return null
  const { run_id, person_id, comment } = raw
  if (typeof run_id !== "string" || typeof person_id !== "string" || typeof comment !== "string") return null
  const judgment = parseJudgment(raw.human_judgment)
  if (judgment === undefined) return null
  return { run_id, person_id, comment, human_judgment: judgment }
}

/** The stored queue, skipping entries neither page wrote; empty when storage is blocked. */
export function readQueue(): FeedbackRecord[] {
  const parse = (raw: unknown) =>
    Array.isArray(raw) ? raw.map(parseQueued).filter((record) => record !== null) : null
  return readStored("local", FEEDBACK_STORAGE_KEY, parse) ?? []
}

export function writeQueue(queue: readonly FeedbackRecord[]): void {
  writeStored("local", FEEDBACK_STORAGE_KEY, queue.map(formValues))
}
