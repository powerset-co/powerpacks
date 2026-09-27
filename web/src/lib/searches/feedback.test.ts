import { beforeEach, describe, expect, it, vi } from "vitest"

import type { Candidate } from "@/types/searches"

import {
  buildScoreFeedback,
  buildSearchFeedback,
  FEEDBACK_STORAGE_KEY,
  fiveScore,
  flushFeedback,
  formValues,
  parseQueued,
  queuedScores,
  readQueue,
  writeQueue,
  type FeedbackRecord,
  type FeedbackReply,
} from "./feedback"
import { MemoryStorage } from "./memory-storage"

// human_ratings.LEGACY_SCORES as the server sends it.
const LEGACY = { "1": 1, "2": 2, "3": 2, "4": 2, "7": 3, "8": 4, "9": 5, "10": 5 }

const CASEY: Pick<Candidate, "person_id"> = { person_id: "casey" }

const SUBMITTED: FeedbackReply = { ok: true, status: "submitted" }
const SAVED_LOCALLY: FeedbackReply = { ok: true, status: "saved_locally" }

function score(person: string, value: number, comment = ""): FeedbackRecord {
  return { run_id: "jordan-role", person_id: person, comment, human_judgment: { score: value, scale: 5 } }
}

beforeEach(() => vi.stubGlobal("localStorage", new MemoryStorage()))

describe("builders", () => {
  it("scores a candidate on the five-point scale with a trimmed note", () => {
    expect(buildScoreFeedback("jordan-role", CASEY, 5, " Relevant experience ")).toEqual({
      run_id: "jordan-role",
      person_id: "casey",
      comment: "Relevant experience",
      human_judgment: { score: 5, scale: 5 },
    })
  })

  it("sends search feedback without a person or a judgment", () => {
    const record = buildSearchFeedback("jordan-role", "Too many recruiters ")
    expect(record).toEqual({
      run_id: "jordan-role",
      person_id: "",
      comment: "Too many recruiters",
      human_judgment: null,
    })
    expect(formValues(record)).toEqual({
      run_id: "jordan-role",
      person_id: "",
      comment: "Too many recruiters",
    })
  })

  it("posts the judgment as JSON, as results.js does", () => {
    expect(formValues(score("casey", 4, "Yes"))).toEqual({
      run_id: "jordan-role",
      person_id: "casey",
      comment: "Yes",
      human_judgment: '{"score":4,"scale":5}',
    })
  })
})

describe("legacy scores", () => {
  // tests/test_deep_search_results_web.py: a score queued by the old page as {"score": 7}
  // shows and sends as 3/5; {"score": 4} becomes 2; a saved 8 shows as 4.
  it("reads a judgment without a scale as the ten-point rubric", () => {
    const record = parseQueued({
      run_id: "jordan-role",
      person_id: "casey",
      comment: "Queued by old page",
      human_judgment: JSON.stringify({ score: 7 }),
    })
    expect(record?.human_judgment).toEqual({ score: 7, scale: 10 })
  })

  it("converts ten-point scores through ratings.legacy and keeps five-point ones", () => {
    expect(fiveScore({ score: 7, scale: 10 }, LEGACY)).toBe(3)
    expect(fiveScore({ score: 4, scale: 10 }, LEGACY)).toBe(2)
    expect(fiveScore({ score: 8, scale: 10 }, LEGACY)).toBe(4)
    expect(fiveScore({ score: 4, scale: 5 }, LEGACY)).toBe(4)
    expect(fiveScore({ score: 6, scale: 10 }, LEGACY)).toBeNull()
  })

  it("shows the latest queued score per person for one run", () => {
    const old: FeedbackRecord = {
      ...score("casey", 7, "Queued by old page"),
      human_judgment: { score: 7, scale: 10 },
    }
    const other: FeedbackRecord = { ...score("casey", 1), run_id: "other-role" }
    const scores = queuedScores(
      [score("casey", 5), old, other, buildSearchFeedback("jordan-role", "x")],
      "jordan-role",
      LEGACY,
    )
    expect([...scores]).toEqual([["casey", { score: 3, note: "Queued by old page" }]])
  })
})

describe("queue storage", () => {
  it("reads results.js entries and writes them back in its form", () => {
    const stored = [
      {
        run_id: "jordan-role",
        person_id: "casey",
        comment: "Queued by old page",
        human_judgment: '{"score":7}',
      },
      { run_id: "jordan-role", person_id: "", comment: "Search note" },
    ]
    localStorage.setItem(FEEDBACK_STORAGE_KEY, JSON.stringify(stored))
    const queue = readQueue()
    expect(queue.map((record) => record.human_judgment)).toEqual([{ score: 7, scale: 10 }, null])
    writeQueue(queue)
    expect(JSON.parse(localStorage.getItem(FEEDBACK_STORAGE_KEY) ?? "")).toEqual([
      { ...stored[0], human_judgment: '{"score":7,"scale":10}' },
      stored[1],
    ])
  })

  it("skips entries it cannot read and survives a broken store", () => {
    localStorage.setItem(
      FEEDBACK_STORAGE_KEY,
      JSON.stringify([
        { run_id: "jordan-role" },
        { run_id: "a", person_id: "b", comment: "", human_judgment: "{" },
      ]),
    )
    expect(readQueue()).toEqual([])
    localStorage.setItem(FEEDBACK_STORAGE_KEY, "not json")
    expect(readQueue()).toEqual([])
  })
})

describe("flushFeedback", () => {
  it("posts every record in order and leaves nothing", async () => {
    const post = vi.fn((_record: FeedbackRecord) => Promise.resolve(SUBMITTED))
    const queue = [score("casey", 2), score("casey", 5), buildSearchFeedback("jordan-role", "note")]
    expect(await flushFeedback(queue, post)).toEqual([])
    expect(post.mock.calls.map(([record]) => record)).toEqual(queue)
  })

  it("keeps the first failure and everything after it, unsent", async () => {
    const queue = [score("casey", 2), score("jordan", 4), score("casey", 5)]
    const post = vi.fn((record: FeedbackRecord) =>
      record.person_id === "jordan" ? Promise.reject(new Error("offline")) : Promise.resolve(SUBMITTED),
    )
    expect(await flushFeedback(queue, post)).toEqual(queue.slice(1))
    expect(post).toHaveBeenCalledTimes(2)
  })

  it("keeps a record the server saved but could not send to Powerset", async () => {
    const queue = [score("casey", 2), score("jordan", 4)]
    const post = vi.fn((_record: FeedbackRecord) => Promise.resolve(SAVED_LOCALLY))
    expect(await flushFeedback(queue, post)).toEqual(queue)
    expect(post).toHaveBeenCalledTimes(1)
  })
})
