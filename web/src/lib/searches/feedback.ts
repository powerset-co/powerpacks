// Feedback on a search or one of its candidates: the record POST /searches/feedback accepts
// (results_web/server.py _save_feedback), and the queue of records not yet sent. The queue lives
// in localStorage under the key results.js uses, stored as the same form values, so either page
// replays what the other queued.

import type { Candidate, Ratings } from "@/types/searches"

export const FEEDBACK_STORAGE_KEY = "powerpacks:pending-feedback:v1"

export const SCORE_SCALE = 5

// human_ratings.convert_rating: a judgment without a scale was saved on the old ten-point rubric.
const LEGACY_SCALE = 10

export interface HumanJudgment {
  score: number
  scale: typeof SCORE_SCALE | typeof LEGACY_SCALE
}

/** One feedback row. `person_id` is "" for feedback on the whole search; a judgment needs a person. */
export interface FeedbackRecord {
  run_id: string
  person_id: string
  comment: string
  human_judgment: HumanJudgment | null
}

/** The server's answer to a stored record: "submitted" reached Powerset, "saved_locally" did not. */
export interface FeedbackReply {
  ok: boolean
  status: "submitted" | "saved_locally"
}

export type FeedbackOutcome = "sent" | "queued"

export const OUTCOME_MESSAGE: Record<FeedbackOutcome, string> = {
  sent: "Sent.",
  queued: "Saved to send later.",
}

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

/**
 * Posts `queue` in order and returns what is left: the first record the server did not submit and
 * everything after it, so a later score for a person never lands before an earlier one.
 */
export async function flushFeedback(
  queue: readonly FeedbackRecord[],
  post: (record: FeedbackRecord) => Promise<FeedbackReply>,
): Promise<FeedbackRecord[]> {
  for (const [index, record] of queue.entries()) {
    try {
      const reply = await post(record)
      if (reply.status !== "submitted") return queue.slice(index)
    } catch {
      return queue.slice(index)
    }
  }
  return []
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
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

// Storage can be blocked (private mode, quota): a read is then empty, a write best effort.

export function readQueue(): FeedbackRecord[] {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(FEEDBACK_STORAGE_KEY) ?? "[]")
    if (!Array.isArray(raw)) return []
    return raw.map(parseQueued).filter((record) => record !== null)
  } catch {
    return []
  }
}

export function writeQueue(queue: readonly FeedbackRecord[]): void {
  try {
    localStorage.setItem(FEEDBACK_STORAGE_KEY, JSON.stringify(queue.map(formValues)))
  } catch {
    // Storage blocked: the queue just won't survive a reload.
  }
}
