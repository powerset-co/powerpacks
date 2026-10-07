// Synthetic review payloads for the vitest suites: one builder per shape in types/review.ts,
// every key present as the server sends it. Each takes the fields a test cares about.

import type {
  DecideResult,
  DecisionProgress,
  EnrichmentPanel,
  LinkedinCardPayload,
  LinkedinFinished,
  PageProgress,
  QueuePosition,
  ReviewCandidate,
  ReviewPage,
  ReviewPerson,
} from "@/types/review"

import type { Review } from "@/pages/review/hooks/useReview"

export function reviewPerson(overrides: Partial<ReviewPerson> = {}): ReviewPerson {
  return {
    parent_id: "parent-jordan",
    slug: "jordan-bravo",
    name: "Jordan Bravo",
    sources: ["gmail", "imessage"],
    labels: ["Founder", "Close friend"],
    worth_key: "worth-jordan",
    contacts: "jordan@example.com · +15550100",
    ...overrides,
  }
}

export function reviewCandidate(overrides: Partial<ReviewCandidate> = {}): ReviewCandidate {
  return {
    row_key: "jordan-bravo-1",
    name: "Jordan Bravo",
    url: "https://www.linkedin.com/in/jordan-bravo",
    headline: "Founder at Example Labs",
    location: "Springfield",
    experiences: ["Founder, Example Labs", "Engineer, Acme"],
    education: ["Example University"],
    synthetic: false,
    avatar_url: "https://media.example.com/jordan-bravo.jpg",
    confidence: null,
    verdict: "",
    reason: "",
    ...overrides,
  }
}

/** A researched profile: no LinkedIn, no picture. */
export function syntheticCandidate(overrides: Partial<ReviewCandidate> = {}): ReviewCandidate {
  return reviewCandidate({
    row_key: "jordan-bravo-research",
    url: "",
    synthetic: true,
    avatar_url: "",
    ...overrides,
  })
}

export function decisionProgress(overrides: Partial<DecisionProgress> = {}): DecisionProgress {
  return { worth_pending: 3, worth_yes: 5, worth_no: 2, linkedin_pending: 4, ...overrides }
}

export function pageProgress(overrides: Partial<PageProgress> = {}): PageProgress {
  return { ...decisionProgress(), linkedin_done: 6, rejected: 2, synthesize_pending: 0, ...overrides }
}

function enrichmentPanel(): EnrichmentPanel {
  return { mode: "completed", completed: 0, total: 0, approval_label: "", error: "" }
}

/** The Check LinkedIn screen. */
export function reviewPage(overrides: Partial<ReviewPage> = {}): ReviewPage {
  return {
    view: "linkedin",
    tab: "",
    title: "Check LinkedIn",
    progress: pageProgress(),
    enrichment: enrichmentPanel(),
    state_token: "token-1",
    needs_synthesis: false,
    external_updates: false,
    ...overrides,
  }
}

export function queuePosition(overrides: Partial<QueuePosition> = {}): QueuePosition {
  return { index: 0, total: 3, ...overrides }
}

export function linkedinFinished(overrides: Partial<LinkedinFinished> = {}): LinkedinFinished {
  return {
    synthesize_pending: false,
    ...overrides,
  }
}

/** A LinkedIn card with one candidate; pass `card.candidates` for several, or
 *  `{ card: null, finished: linkedinFinished() }` for the empty queue. */
export function linkedinCard(overrides: Partial<LinkedinCardPayload> = {}): LinkedinCardPayload {
  return {
    card: { person: reviewPerson(), candidates: [reviewCandidate()] },
    finished: null,
    pending: 4,
    queue: null,
    ...overrides,
  }
}

export function decideResult(overrides: Partial<DecideResult> = {}): DecideResult {
  return {
    ok: true,
    pub: "jordan-bravo-1",
    action: "keep",
    approved: "yes",
    new_url: "",
    resolved_pubs: ["jordan-bravo-1"],
    next: linkedinCard({ pending: 3 }),
    ...overrides,
  }
}

/** A JSON answer, as the review server sends it. */
export function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } })
}

/** A refusal: `{"error": text}` with its status code. */
export function errorResponse(text: string, status = 409): Response {
  return jsonResponse({ error: text }, status)
}

const noop = () => undefined

/** `matchMedia` for jsdom, which has none: `vi.stubGlobal("matchMedia", motionMedia())`. Reduced
 *  motion by default, as the browser tests run: no fade waits, overlays unmount at once. */
export function motionMedia(reduced = true) {
  return (media: string) => ({ matches: reduced, media, addEventListener: noop, removeEventListener: noop })
}

/** The page's `Review` for a stage or shared component under test (testing/review-harness.tsx
 *  provides it): every action does nothing; pass `vi.fn()` for the ones a test watches. */
export function fakeReview(overrides: Partial<Review> = {}): Review {
  return {
    progress: pageProgress(),
    preview: false,
    debug: false,
    index: 0,
    fadeMs: 0,
    toast: noop,
    toastError: noop,
    applyProgress: noop,
    reload: noop,
    leaveAndReload: noop,
    ...overrides,
  }
}
