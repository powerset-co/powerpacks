// Synthetic review payloads for the vitest suites: one builder per shape in types/review.ts,
// every key present as the server sends it. Each takes the fields a test cares about.

import type {
  ApproveResult,
  DecideResult,
  DecisionProgress,
  DecisionRow,
  EnrichmentPanel,
  LinkedinCardPayload,
  LinkedinFinished,
  PageProgress,
  QueuePosition,
  ReviewCandidate,
  ReviewEvent,
  ReviewPage,
  ReviewPerson,
  ReviewStatus,
  ReviewStep,
  ReviewView,
  WorthCardPayload,
  WorthPendingEntry,
  WorthResult,
  WorthTablePayload,
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
    contacts: "jordan@example.com · +15550100",
    avatar_url: "https://media.example.com/jordan-bravo.jpg",
    question: "",
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

export function enrichmentPanel(overrides: Partial<EnrichmentPanel> = {}): EnrichmentPanel {
  return {
    mode: "approval",
    completed: 0,
    total: 0,
    approval_label: "Approve $2.85",
    error: "",
    ...overrides,
  }
}

const TITLES: Readonly<Record<ReviewView, string>> = {
  worth: "Add People",
  enrich: "Enrich Contacts",
  linkedin: "Check LinkedIn",
  done: "All Set",
}

/** The three steps as `full_page` builds them from the progress: nothing complete while
 *  synthesis is pending, each count the stage's pending people. */
export function reviewSteps(
  progress: PageProgress = pageProgress(),
  enrichment: EnrichmentPanel = enrichmentPanel(),
): [ReviewStep, ReviewStep, ReviewStep] {
  const synthesized = !progress.synthesize_pending
  return [
    {
      number: 1,
      label: "Review Decisions",
      stage: "worth",
      complete: synthesized && !progress.worth_pending,
      count: progress.worth_pending,
    },
    {
      number: 2,
      label: "Enrich Contacts",
      stage: "enrich",
      complete: synthesized && enrichment.mode === "completed",
      count: 0,
    },
    {
      number: 3,
      label: "Check LinkedIn",
      stage: "linkedin",
      complete: synthesized && !progress.linkedin_pending,
      count: progress.linkedin_pending,
    },
  ]
}

/** One screen. The title, tab, steps and `external_updates` follow the view unless overridden. */
export function reviewPage(view: ReviewView = "worth", overrides: Partial<ReviewPage> = {}): ReviewPage {
  const progress = overrides.progress ?? pageProgress()
  const enrichment = overrides.enrichment ?? enrichmentPanel()
  return {
    view,
    tab: view === "worth" ? "review" : "",
    title: TITLES[view],
    steps: reviewSteps(progress, enrichment),
    progress,
    enrichment,
    state_token: "token-1",
    needs_synthesis: false,
    external_updates: view === "enrich" || view === "done",
    ...overrides,
  }
}

export function queuePosition(overrides: Partial<QueuePosition> = {}): QueuePosition {
  return { index: 0, total: 3, ...overrides }
}

/** A worth card for one person; `worthCard({ card: null })` is the empty queue. */
export function worthCard(overrides: Partial<WorthCardPayload> = {}): WorthCardPayload {
  return {
    card: { person: reviewPerson(), candidate: reviewCandidate() },
    synthesize_pending: false,
    queue: null,
    ...overrides,
  }
}

export function worthPending(): WorthPendingEntry[] {
  return [
    { key: "worth-casey", name: "Casey Delta" },
    { key: "worth-jordan", name: "Jordan Bravo" },
    { key: "worth-riley", name: "Riley Echo" },
  ]
}

export function decisionRow(overrides: Partial<DecisionRow> = {}): DecisionRow {
  return { person: reviewPerson(), candidate: reviewCandidate(), reason: "You said yes", ...overrides }
}

export function worthTable(overrides: Partial<WorthTablePayload> = {}): WorthTablePayload {
  const casey = reviewPerson({
    parent_id: "parent-casey",
    slug: "casey-delta",
    name: "Casey Delta",
    worth_key: "worth-casey",
    labels: [],
  })
  return {
    rows: [decisionRow({ person: casey, candidate: null, reason: "Worth adding" }), decisionRow()],
    total: 2,
    ...overrides,
  }
}

export function linkedinFinished(overrides: Partial<LinkedinFinished> = {}): LinkedinFinished {
  return {
    synthesize_pending: false,
    linkedin_done: 6,
    linkedin_complete: false,
    retargets_in_flight: 0,
    auto_continue: true,
    ...overrides,
  }
}

/** A LinkedIn card with one candidate; pass `card.candidates` for several, or
 *  `{ card: null, finished: linkedinFinished() }` for the empty queue. */
export function linkedinCard(overrides: Partial<LinkedinCardPayload> = {}): LinkedinCardPayload {
  return {
    card: { person: reviewPerson(), candidates: [reviewCandidate()], failure_note: "" },
    finished: null,
    pending: 4,
    queue: null,
    ...overrides,
  }
}

export function worthResult(overrides: Partial<WorthResult> = {}): WorthResult {
  return {
    ok: true,
    pub: "worth-jordan",
    effective: "yes",
    progress: decisionProgress({ worth_pending: 2, worth_yes: 6 }),
    next_stage: "worth",
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
    progress: decisionProgress({ linkedin_pending: 3 }),
    resolved_pubs: ["jordan-bravo-1"],
    next: linkedinCard({ pending: 3 }),
    ...overrides,
  }
}

export function approveResult(overrides: Partial<EnrichmentPanel> = {}): ApproveResult {
  return { ok: true, enrichment: enrichmentPanel({ mode: "running", completed: 0, total: 12, ...overrides }) }
}

export function reviewStatus(overrides: Partial<ReviewStatus> = {}): ReviewStatus {
  return { stage: "enrich", next_action: "approve_enrichment", state_token: "token-1", ...overrides }
}

/** A mid-run enrichment event: `completed` of `total` lookups done. */
export function runningEvent(completed = 3, total = 12): ReviewEvent {
  return { seq: 1, job: { status: "running", counts: { total, completed } } }
}

/** A plain change with no job (a decision, a finished run). */
export function changeEvent(overrides: Partial<ReviewEvent> = {}): ReviewEvent {
  return { seq: 2, job: null, ...overrides }
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
    transition: noop,
    reload: noop,
    leaveAndReload: noop,
    syncStatus: noop,
    noteServerStage: noop,
    setGuidanceDraft: noop,
    setCompleting: noop,
    ...overrides,
  }
}

/**
 * /api/events for jsdom, which has no EventSource: `vi.stubGlobal("EventSource", FakeEventSource)`.
 * `FakeEventSource.opened` lists every stream a test opened (clear it between tests); `open()`
 * and `emit()` play the server.
 */
export class FakeEventSource {
  static opened: FakeEventSource[] = []

  readonly url: string
  closed = false
  onmessage: ((message: MessageEvent<string>) => void) | null = null
  onopen: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeEventSource.opened.push(this)
  }

  /** The stream (re)connected. */
  open(): void {
    this.onopen?.()
  }

  /** One message; a string is sent as written (an unreadable message). */
  emit(event: ReviewEvent | string): void {
    const data = typeof event === "string" ? event : JSON.stringify(event)
    this.onmessage?.(new MessageEvent("message", { data }))
  }

  close(): void {
    this.closed = true
  }
}
