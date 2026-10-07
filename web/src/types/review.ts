// The Review API's shapes, field for field with packs/ingestion/primitives/deep_context_v2/review/payloads.py.
// tests/test_deep_context_review_api.py pins the two together.

/** The four review screens. `done` has no step of its own; it lights step 3. */
export type ReviewView = "linkedin"

/** The worth screen's tabs: the pending card queue, then the two decided piles. */

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

export type EnrichmentMode = "running" | "approval" | "completed" | "failed" | "preparing"

/** The Enrich screen's one panel. */
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
  /** Unused by the LinkedIn screen; "" from the server. */
  tab: string
  title: string
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
  /** Every merged record's emails and phones, de-duplicated and joined with " · "; "" on a pile row. */
  contacts: string
}

/** One LinkedIn (or researched) profile a parent might be. */
export interface ReviewCandidate {
  /** The key POST /api/review/decide, /retarget and /feedback take as `pub`. */
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
  /** The profile's picture; "" when it has none or is a researched profile. */
  avatar_url: string
  confidence: number | null
  verdict: string
  reason: string
}

/** The debug carousel's position (`?debug=1`). */
export interface QueuePosition {
  index: number
  total: number
}

/** What replaces the LinkedIn card when the queue has nothing to show. */
export interface LinkedinFinished {
  synthesize_pending: boolean
}

/** GET /api/review/linkedin-card, and `next` on a decide: exactly one of card / finished. */
export interface LinkedinCardPayload {
  card: { person: ReviewPerson; candidates: ReviewCandidate[] } | null
  finished: LinkedinFinished | null
  /** People left to check, this card included; people out for re-research are not counted. */
  pending: number
  queue: QueuePosition | null
}

export type LinkedinDecision = "keep" | "detach" | "fix" | "exclude" | "reset"

/** POST /api/review/decide. */
export interface DecideResult {
  ok: true
  pub: string
  action: string
  approved: string
  new_url: string
  resolved_pubs: string[]
  /** The card to show next; its `pending` is the count the page repaints. */
  next: LinkedinCardPayload
}

/** The step an enrichment run is on, in the order it takes them; "" when none is running. */

/** The one feedback kind the review cards file (feedback.py `FEEDBACK_ACTIONS`). */
export type FeedbackAction = "general"
