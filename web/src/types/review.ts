// The Review API's shapes, field for field with packs/ingestion/primitives/deep_context/review/api.py.
// tests/test_deep_context_review_api.py pins the two together.

/** The four review screens. `done` has no step of its own; it lights step 3. */
export type ReviewView = "worth" | "enrich" | "linkedin" | "done"

/** The worth screen's tabs: the pending card queue, then the two decided piles. */
export type WorthTab = "review" | "yes" | "no"

/** The counts a decision click repaints: the worth tabs and the step badges. */
export interface DecisionProgress {
  worth_pending: number
  worth_yes: number
  worth_no: number
  linkedin_pending: number
}

/** GET /api/review/page `progress`: `DecisionProgress` plus what only a page load shows. */
export interface PageProgress extends DecisionProgress {
  linkedin_done: number
  rejected: number
  synthesize_pending: number
}

/** One stepper step. It draws its check when `complete` and `count` is 0; else its number. */
export interface ReviewStep {
  number: 1 | 2 | 3
  label: string
  stage: Exclude<ReviewView, "done">
  complete: boolean
  count: number
}

export type EnrichmentMode = "running" | "approval" | "completed" | "failed" | "preparing"

/** The Enrich screen's one panel (rendering.py `render_enrichment`). */
export interface EnrichmentPanel {
  mode: EnrichmentMode
  /** running: research lookups finished / planned. */
  completed: number
  total: number
  /** approval: the button's words ("Approve $2.85" or the $0 continue). */
  approval_label: string
  /** failed: the pipeline's last error. */
  error: string
}

/** GET /api/review/page?stage=&view=: everything around the stage content. */
export interface ReviewPage {
  /** The screen to draw: the requested stage, the store's current stage when none was
   *  asked for, and Enrich when the worth queue is asked for but already empty. */
  view: ReviewView
  /** The worth tab, "" on every other screen. */
  tab: WorthTab | ""
  title: string
  steps: [ReviewStep, ReviewStep, ReviewStep]
  progress: PageProgress
  enrichment: EnrichmentPanel
  state_token: string
  /** Synthesis has not run, so this screen shows the command to run instead of its content:
   *  Done, and Enrich once its plan is complete. (The worth and LinkedIn queues say it on
   *  their own card reads.) */
  needs_synthesis: boolean
  /** Enrich and Done watch /api/events and re-read /api/status; the other screens never do. */
  external_updates: boolean
}

/** A parent as every review card shows it. */
export interface ReviewPerson {
  parent_id: string
  slug: string
  name: string
  /** Message sources in display order: "gmail" | "imessage" | "whatsapp". */
  sources: string[]
  /** Label badge titles that cleared the threshold, strongest first (all of them). */
  labels: string[]
  /** The key POST /worth takes as `pub`. */
  worth_key: string
}

/** One LinkedIn (or researched) profile a parent might be. */
export interface ReviewCandidate {
  /** The key POST /decide, /retarget and /feedback take as `pub`. */
  row_key: string
  name: string
  /** The profile link; "" for a researched profile with no LinkedIn. */
  url: string
  headline: string
  location: string
  experiences: string[]
  education: string[]
  /** A researched profile: no LinkedIn, no avatar. */
  synthetic: boolean
  /** Matched emails and phones, already de-duplicated and joined with " · ". */
  contacts: string
  /** The profile's picture; "" when it has none or is a researched profile. */
  avatar_url: string
  /** What the judge wants the reviewer to settle about this profile; "" asks the usual
   *  "Is this the right profile?". */
  question: string
}

/** The debug carousel's position (`?debug=1`). */
export interface QueuePosition {
  index: number
  total: number
}

/** A person with the profile shown beside them: a worth card, and what an opened pile row
 *  reads (GET /api/review/worth-details?slug=). */
export interface WorthDetails {
  person: ReviewPerson
  candidate: ReviewCandidate | null
}

/** GET /api/review/worth-card: the next undecided person, or why there is none. */
export interface WorthCardPayload {
  card: WorthDetails | null
  /** No card because synthesis has not run (the handoff panel), not because the queue is done. */
  synthesize_pending: boolean
  queue: QueuePosition | null
}

/** GET /api/review/worth-pending: the typeahead's names, in queue order. */
export interface WorthPendingEntry {
  key: string
  name: string
}

/** One row of a decided pile. A pile page is light: the row's person carries no sources, and
 *  its profile is read from worth-details when the row is opened. */
export interface DecisionRow {
  person: ReviewPerson
  /** Why it sits in this pile: the human's note or call, else the machine's reason. */
  reason: string
}

/** GET /api/review/worth-table?view=&offset=: one page of a decided pile. */
export interface WorthTablePayload {
  rows: DecisionRow[]
  total: number
}

/** What replaces the LinkedIn card when the queue has nothing to show. */
export interface LinkedinFinished {
  synthesize_pending: boolean
  linkedin_done: number
  /** Nothing pending at all: show the go-back handoff, not Finish. */
  linkedin_complete: boolean
  retargets_in_flight: number
  /** Finish presses itself once background work settles. */
  auto_continue: boolean
}

/** GET /api/review/linkedin-card, and `next` on a decide: exactly one of card / finished. */
export interface LinkedinCardPayload {
  card: { person: ReviewPerson; candidates: ReviewCandidate[]; failure_note: string } | null
  finished: LinkedinFinished | null
  /** Parents still pending, in-flight re-research included. */
  pending: number
  queue: QueuePosition | null
}

/** POST /worth. */
export interface WorthResult {
  ok: true
  pub: string
  effective: string
  progress: DecisionProgress
  next_stage: "enrich" | "worth"
}

export type LinkedinDecision = "keep" | "detach" | "fix" | "exclude" | "reset"

/** POST /api/review/decide. */
export interface DecideResult {
  ok: true
  pub: string
  action: string
  approved: string
  new_url: string
  progress: DecisionProgress
  resolved_pubs: string[]
  next: LinkedinCardPayload
}

/** POST /api/review/approve-enrichment. */
export interface ApproveResult {
  ok: true
  enrichment: EnrichmentPanel
}

/** GET /api/status. */
export interface ReviewStatus {
  stage: ReviewView
  next_action: string
  state_token: string
}

/** One /api/events message. `job` is the running enrichment's last receipt, else null. */
export interface ReviewEvent {
  seq: number
  replay?: boolean
  job: {
    status: string
    phase?: string
    counts?: { total?: number; completed?: number }
    progress?: { phase_done?: number; phase_total?: number }
  } | null
}

/** The one feedback kind the review cards file (feedback.py `FEEDBACK_ACTIONS`). */
export type FeedbackAction = "general"
