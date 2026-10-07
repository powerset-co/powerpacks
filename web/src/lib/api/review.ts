// The review page's routes on the Python page server: the JSON routes under /api/review/ and
// the form routes (deep_context_v2/review/api.py), and the dossier route beside them.

import { body, failure } from "@/lib/api/http"
import { SAVE_FAILED } from "@/lib/review/copy"
import { isRecord } from "@/lib/utils"
import type {
  DecideResult,
  FeedbackAction,
  LinkedinCardPayload,
  LinkedinDecision,
  ReviewPage,
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
 * (404: the person is no longer pending); `status` is the JSON body's `status`, "" without
 * one ("needs_auth" on /feedback: offer the sign-in).
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
  /** Slugs the queue leaves out: the card on screen, saves in flight. */
  exclude?: readonly string[]
  /** The queue position; the first card without one. */
  index?: number
  /** Ask for the carousel's position (`queue`). */
  debug?: boolean
}

function cardQuery({ exclude = [], index = 0, debug = false }: CardQuery): Record<string, string> {
  return { exclude: exclude.join(","), index: index ? String(index) : "", debug: debug ? "1" : "" }
}

/** The next person to check. */
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

/** The person's dossier as an HTML fragment (name and contact left out: the card shows them);
 *  null when the server has none. A network failure throws. */
export async function fetchDossier(slug: string, signal?: AbortSignal): Promise<string | null> {
  const response = await fetch(`/api/dossier${query({ slug, skip: "1" })}`, { signal })
  return response.ok ? response.text() : null
}
