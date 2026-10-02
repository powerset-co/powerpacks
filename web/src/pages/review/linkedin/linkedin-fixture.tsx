// The LinkedIn suites' review server and the stage rendered against it. Every request goes to
// this mock: POST /retarget and POST /feedback cost money on the real server and are never
// called there. A test names the answer for each route it expects; any other request fails.

import { render } from "@testing-library/react"
import { vi } from "vitest"

import {
  fakeReview,
  jsonResponse,
  linkedinCard,
  reviewCandidate,
  reviewPerson,
} from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"
import type { LinkedinCardPayload, ReviewCandidate } from "@/types/review"

import type { Review } from "../hooks/useReview"
import { LinkedinStage } from "./LinkedinStage"

/** One request the stage made. */
export interface Call {
  method: string
  /** The path with its query, as asked. */
  url: string
  /** A POST's form fields. */
  form: Record<string, string>
}

type Route = (call: Call) => Response | Promise<Response>
/** A JSON payload, a ready response (cloned per request), or a function that makes one. */
type Answer = object | Response | Route

function isRoute(answer: Answer): answer is Route {
  return typeof answer === "function"
}

function respond(answer: Answer, call: Call): Response | Promise<Response> {
  if (isRoute(answer)) return answer(call)
  if (answer instanceof Response) return answer.clone()
  return jsonResponse(answer)
}

/** The mocked review server: `answer("POST /retarget", …)` per route, keyed by method and path. */
export function reviewServer() {
  const answers = new Map<string, Answer>()
  const calls: Call[] = []

  const fetchMock = vi.fn((url: string, init?: RequestInit): Promise<Response> => {
    const { pathname } = new URL(url, "http://review.test")
    const form = init?.body instanceof URLSearchParams ? Object.fromEntries(init.body) : {}
    const call: Call = { method: init?.method ?? "GET", url, form }
    calls.push(call)
    const answer = answers.get(`${call.method} ${pathname}`)
    if (answer === undefined) return Promise.reject(new Error(`unexpected request: ${call.method} ${url}`))
    return Promise.resolve(respond(answer, call))
  })

  return {
    fetch: fetchMock,
    answer: (route: string, answer: Answer) => void answers.set(route, answer),
    /** The URLs read from `path`, in order. */
    gets: (path: string) =>
      calls.filter((call) => call.method === "GET" && call.url.startsWith(path)).map((call) => call.url),
    /** The forms posted to `path`, in order. */
    posts: (path: string) =>
      calls.filter((call) => call.method === "POST" && call.url === path).map((call) => call.form),
    /** Forgets the answers and the calls, and answers every dossier read. */
    reset: () => {
      answers.clear()
      calls.length = 0
      fetchMock.mockClear()
      answers.set("GET /api/dossier", new Response("<p>Met at Acme.</p>"))
    },
  }
}

/** An answer a test holds back until it has looked at the card mid-request. */
export function gate() {
  let open: (response: Response) => void = () => undefined
  const answer = new Promise<Response>((resolve) => {
    open = resolve
  })
  return { answer, open: (response: Response) => open(response) }
}

/** A refusal as /retarget, /complete and /feedback send it: plain text. */
export function refusal(text: string, status = 400): Response {
  return new Response(text, { status })
}

/** The person after Jordan Bravo in the queue. */
export function caseyCard(overrides: Partial<LinkedinCardPayload> = {}): LinkedinCardPayload {
  const person = reviewPerson({
    parent_id: "parent-casey",
    slug: "casey-delta",
    name: "Casey Delta",
    worth_key: "worth-casey",
    labels: [],
    contacts: "casey@example.com",
  })
  const candidate = reviewCandidate({
    row_key: "casey-delta-1",
    name: "Casey Delta",
    url: "https://www.linkedin.com/in/casey-delta",
  })
  return linkedinCard({
    card: { person, candidates: [candidate] },
    pending: 3,
    ...overrides,
  })
}

/** Jordan Bravo with several profiles to pick from. */
export function severalCard(candidates: ReviewCandidate[]): LinkedinCardPayload {
  return linkedinCard({ card: { person: reviewPerson(), candidates } })
}

/** The page's `Review` with every action a spy. */
export function spyReview(overrides: Partial<Review> = {}): Review {
  return fakeReview({
    toast: vi.fn(),
    toastError: vi.fn(),
    applyProgress: vi.fn(),
    transition: vi.fn(),
    leaveAndReload: vi.fn(),
    setGuidanceDraft: vi.fn(),
    setCompleting: vi.fn(),
    ...overrides,
  })
}

export function renderStage(review: Review = spyReview()) {
  const view = render(
    <ReviewHarness review={review}>
      <LinkedinStage />
    </ReviewHarness>,
  )
  return { ...view, review }
}
