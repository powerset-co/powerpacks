// The review page's routes on the Python review server: the JSON routes under /api/review/
// and the form routes (deep_context/review/api.py), and the status, event and dossier routes beside them.

import { body, failure } from "@/lib/api/http"
import { SAVE_FAILED } from "@/lib/review/copy"
import { isRecord } from "@/lib/utils"
import type {
  DecideResult,
  FeedbackAction,
  LinkedinCardPayload,
  LinkedinDecision,
  ReviewEvent,
  ReviewPage,
  ReviewStatus,
  ReviewView,
  WorthCardPayload,
  WorthPendingEntry,
  WorthResult,
  WorthDetails,
  WorthTablePayload,
} from "@/types/review"

const API = "/api/review/"
const FORM = { "Content-Type": "application/x-www-form-urlencoded" }
/** Every read is fresh: the queues move under the page. */
const FRESH: RequestInit = { cache: "no-store" }

const HTTP_NOT_FOUND = 404
/** /feedback's `status` when Powerset has no usable sign-in. */
const NEEDS_AUTH = "needs_auth"

/**
 * A request the server refused. `message` is the server's words; `http` is the response code
 * (404 on a picked worth card: no longer pending); `status` is the JSON body's `status`, ""
 * without one ("needs_auth" on /feedback: offer the sign-in).
 */
export class ReviewError extends Error {
  readonly http: number
  readonly status: string

  constructor(message: string, http: number, status: string) {
    super(message)
    this.name = "ReviewError"
    this.http = http
    this.status = status
  }

  /** The picked person is no longer pending (decided elsewhere, or the parent is gone). */
  get gone(): boolean {
    return this.http === HTTP_NOT_FOUND
  }

  get needsAuth(): boolean {
    return this.status === NEEDS_AUTH
  }
}

/** The JSON error body's `status`; "" for a text body or a body without one. */
async function bodyStatus(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json()
    return isRecord(payload) && typeof payload.status === "string" ? payload.status : ""
  } catch {
    return ""
  }
}

/** The server's message (`failure`), with the response code and the body's `status`. A JSON
 *  body with a status and no message says the status. */
async function refusal(response: Response, fallback: string): Promise<ReviewError> {
  const status = await bodyStatus(response.clone())
  const { message } = await failure(response, fallback)
  return new ReviewError(message || status || fallback, response.status, status)
}

async function get<T>(path: string, fallback: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { ...FRESH, signal })
  if (!response.ok) throw await refusal(response, fallback)
  return body<T>(response)
}

/** A form POST: the values URL-encoded. A refusal throws the server's words. */
async function post(path: string, values: Record<string, string>): Promise<Response> {
  const response = await fetch(path, { method: "POST", headers: FORM, body: new URLSearchParams(values) })
  if (!response.ok) throw await refusal(response, SAVE_FAILED)
  return response
}

function query(values: Record<string, string>): string {
  const filled = Object.entries(values).filter(([, value]) => value !== "")
  return filled.length ? `?${new URLSearchParams(filled).toString()}` : ""
}

/** The screen for the URL's `stage` and `view`, passed as written: the server picks the view. */
export function fetchReviewPage(stage: string, view: string, signal?: AbortSignal): Promise<ReviewPage> {
  return get<ReviewPage>(`${API}page${query({ stage, view })}`, "Couldn't load the review.", signal)
}

/** Which card of a queue to read. */
export interface CardQuery {
  /** Keys (worth) or slugs (LinkedIn) the queue leaves out: the card on screen, saves in flight. */
  exclude?: readonly string[]
  /** The queue position; the first card without one. */
  index?: number
  /** Ask for the carousel's position (`queue`). */
  debug?: boolean
}

export interface WorthCardQuery extends CardQuery {
  /** One pending person by worth key (the typeahead). */
  pick?: string
}

function cardQuery({ exclude = [], index = 0, debug = false }: CardQuery): Record<string, string> {
  return { exclude: exclude.join(","), index: index ? String(index) : "", debug: debug ? "1" : "" }
}

/** The next undecided person. A `pick` that is no longer pending throws a `ReviewError` that is `gone`. */
export function fetchWorthCard(card: WorthCardQuery = {}, signal?: AbortSignal): Promise<WorthCardPayload> {
  const values = { pick: card.pick ?? "", ...cardQuery(card) }
  return get<WorthCardPayload>(`${API}worth-card${query(values)}`, "Could not load card", signal)
}

/** The typeahead's names, in queue order. */
export async function fetchWorthPending(signal?: AbortSignal): Promise<WorthPendingEntry[]> {
  const payload = await get<{ pending: WorthPendingEntry[] }>(
    `${API}worth-pending`,
    "Could not load people",
    signal,
  )
  return payload.pending
}

/** The person and profile an opened pile row shows. A parent that is gone throws a
 *  `ReviewError` that is `gone`. */
export function fetchWorthDetails(slug: string, signal?: AbortSignal): Promise<WorthDetails> {
  return get<WorthDetails>(`${API}worth-details${query({ slug })}`, "Could not load details", signal)
}

/** One page of a decided pile, from `offset`. */
export function fetchWorthTable(
  view: "yes" | "no",
  offset: number,
  signal?: AbortSignal,
): Promise<WorthTablePayload> {
  const values = { view, offset: String(offset) }
  return get<WorthTablePayload>(`${API}worth-table${query(values)}`, "Could not load people", signal)
}

export function fetchLinkedinCard(card: CardQuery = {}, signal?: AbortSignal): Promise<LinkedinCardPayload> {
  return get<LinkedinCardPayload>(
    `${API}linkedin-card${query(cardQuery(card))}`,
    "Could not load card",
    signal,
  )
}

export interface DecideRequest {
  /** The candidate's `row_key`. */
  pub: string
  decision: LinkedinDecision
  /** The parent the card was drawn from. */
  parent_slug: string
  /** `fix`: the profile URL to apply. */
  new_url?: string
}

/** Saves a LinkedIn decision; the answer carries the next card, read after the write. */
export async function postDecide({ new_url, ...rest }: DecideRequest): Promise<DecideResult> {
  const values = new_url === undefined ? rest : { ...rest, new_url }
  return body<DecideResult>(await post(`${API}decide`, values))
}

/** Approves the enrichment plan and launches it. A click that reaches this spends money. */
export interface WorthRequest {
  /** The person's `worth_key`. */
  pub: string
  worth: "yes" | "no" | "restore"
  parent_slug: string
  /** The card's optional "why" box. */
  note?: string
}

export async function postWorth({ note, ...rest }: WorthRequest): Promise<WorthResult> {
  const values = note === undefined ? rest : { ...rest, note }
  return body<WorthResult>(await post("/worth", values))
}

/** Marks a stage complete; the caller then runs the stage transition. */
export async function completeStage(stage: Exclude<ReviewView, "done">): Promise<void> {
  await post("/complete", { stage })
}

export interface RetargetRequest {
  pub: string
  parent_slug: string
  guidance: string
}

/** Queues a PAID re-research of the person from the typed guidance. */
export async function postRetarget(request: RetargetRequest): Promise<void> {
  await post("/retarget", { ...request })
}

export interface FeedbackRequest {
  pub: string
  parent_slug: string
  comment: string
  action: FeedbackAction
}

/** Files feedback on a person. A `ReviewError` that `needsAuth` means Powerset needs a sign-in. */
export async function postFeedback(request: FeedbackRequest): Promise<void> {
  await post("/feedback", { ...request })
}

/** Opens the Powerset sign-in in the browser on this machine. */
export async function openSignIn(): Promise<void> {
  await post("/auth/login", {})
}

export function fetchStatus(signal?: AbortSignal): Promise<ReviewStatus> {
  return get<ReviewStatus>("/api/status", "Couldn't read the review status.", signal)
}

/**
 * The server's change stream (/api/events), until the returned function is called. `onEvent`
 * gets each message, null for one that is not JSON; `onOpen` runs on every (re)connect.
 */
export function watchEvents(onEvent: (event: ReviewEvent | null) => void, onOpen: () => void): () => void {
  const source = new EventSource("/api/events")
  source.onmessage = (message: MessageEvent<string>) => onEvent(parseEvent(message.data))
  source.onopen = onOpen
  return () => source.close()
}

function parseEvent(data: string): ReviewEvent | null {
  try {
    const payload: unknown = JSON.parse(data)
    return isReviewEvent(payload) ? payload : null
  } catch {
    return null
  }
}

function isReviewEvent(payload: unknown): payload is ReviewEvent {
  return isRecord(payload) && typeof payload.seq === "number"
}

/** The person's dossier as an HTML fragment (name and contact left out: the card shows them);
 *  null when the server has none. A network failure throws. */
export async function fetchDossier(slug: string, signal?: AbortSignal): Promise<string | null> {
  const response = await fetch(`/api/dossier${query({ slug, skip: "1" })}`, { signal })
  return response.ok ? response.text() : null
}
