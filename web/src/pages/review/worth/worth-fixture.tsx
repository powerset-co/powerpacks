// The worth suites' review server and the stage rendered against it (worth-harness.tsx). The
// server keeps the three piles (pending, yes, no) and answers the worth routes from them, as
// api.py and server.py do: the card queue with `pick` / `exclude` / `index` / `debug`, the
// pending names, a decided pile's pages, an opened row's details, the dossier, and POST
// /worth, which moves the person.

import { render } from "@testing-library/react"
import { vi } from "vitest"

import {
  decisionRow,
  errorResponse,
  fakeReview,
  jsonResponse,
  pageProgress,
  reviewCandidate,
  reviewPerson,
  worthCard,
  worthDetails,
  worthResult,
} from "@/testing/review-fixture"
import type { DecisionProgress, DecisionRow, ReviewPerson, WorthTab } from "@/types/review"

import type { Review } from "../hooks/useReview"
import type { Pile } from "./piles"
import { WorthHarness } from "./worth-harness"

type Answer = Response | Promise<Response>

/** "Casey Delta" → parent-casey, casey-delta, worth-casey. */
export function personNamed(name: string, overrides: Partial<ReviewPerson> = {}): ReviewPerson {
  const first = (name.split(" ")[0] ?? name).toLowerCase()
  return reviewPerson({
    parent_id: `parent-${first}`,
    slug: name.toLowerCase().replaceAll(" ", "-"),
    name,
    worth_key: `worth-${first}`,
    ...overrides,
  })
}

/** A decided pile's row as a page carries it: the person without sources, and the reason. */
export function rowNamed(name: string, overrides: Partial<DecisionRow> = {}): DecisionRow {
  return decisionRow({ person: personNamed(name, { sources: [] }), ...overrides })
}

/** A promise a test opens when it wants a held answer to go out. */
export function gate() {
  let open: () => void = () => undefined
  const opened = new Promise<void>((resolve) => {
    open = resolve
  })
  return { opened, open: () => open() }
}

/** A refusal as server.py sends it on POST /worth: plain text with its status code. */
export function refusal(text: string, status = 400): Response {
  return new Response(text, { status })
}

const byName = (a: ReviewPerson, b: ReviewPerson) => a.name.toLowerCase().localeCompare(b.name.toLowerCase())
const keyOf = (person: ReviewPerson) => person.worth_key.toLowerCase()

interface Piles {
  pending: ReviewPerson[]
  yes: DecisionRow[]
  no: DecisionRow[]
  /** Synthesis has not run: an empty queue says so. */
  synthesizePending: boolean
  /** Rows per page of a decided pile (the real server's is 100). */
  pageSize: number
}

export function worthServer(start: Partial<Piles> = {}) {
  const piles: Piles = {
    pending: [personNamed("Jordan Bravo"), personNamed("Casey Delta"), personNamed("Riley Echo")],
    yes: [],
    no: [],
    synthesizePending: false,
    pageSize: 100,
    ...start,
  }

  const progress = (): DecisionProgress => ({
    worth_pending: piles.pending.length,
    worth_yes: piles.yes.length,
    worth_no: piles.no.length,
    linkedin_pending: 4,
  })

  /** GET /api/review/worth-card, as api.py picks the card. */
  function readCard(query: URLSearchParams): Response {
    const pick = (query.get("pick") ?? "").toLowerCase()
    const excluded = new Set((query.get("exclude") ?? "").toLowerCase().split(",").filter(Boolean))
    const picked = pick ? piles.pending.filter((person) => keyOf(person) === pick) : piles.pending
    if (pick && !picked.length) return errorResponse("gone", 404)
    const queue = picked.filter((person) => !excluded.has(keyOf(person))).sort(byName)
    const index = Number(query.get("index") ?? "0") % Math.max(1, queue.length)
    const person = queue[index]
    if (!person) return jsonResponse(worthCard({ card: null, synthesize_pending: piles.synthesizePending }))
    const candidate = reviewCandidate({ row_key: `${person.slug}-1`, name: person.name })
    const position = query.get("debug") === "1" ? { index, total: queue.length } : null
    return jsonResponse(worthCard({ card: { person, candidate }, queue: position }))
  }

  /** GET /api/review/worth-table: one page of a pile from `offset`. */
  function readTable(query: URLSearchParams): Response {
    const pile = query.get("view") === "no" ? piles.no : piles.yes
    const offset = Number(query.get("offset") ?? "0")
    return jsonResponse({ rows: pile.slice(offset, offset + piles.pageSize), total: pile.length })
  }

  /** GET /api/review/worth-details: a decided person with their sources and their profile. */
  function readDetails(query: URLSearchParams): Response {
    const slug = query.get("slug")
    const row = [...piles.yes, ...piles.no].find((other) => other.person.slug === slug)
    if (!row) return errorResponse("gone", 404)
    const person = { ...row.person, sources: ["gmail", "imessage"] }
    const candidate = reviewCandidate({ row_key: `${person.slug}-1`, name: person.name })
    return jsonResponse(worthDetails({ person, candidate }))
  }

  /** POST /worth: the person leaves their pile for the one named. */
  function save(form: URLSearchParams): Response {
    const pub = form.get("pub") ?? ""
    const to: Pile = form.get("worth") === "no" ? "no" : "yes"
    const from = to === "yes" ? piles.no : piles.yes
    const pending = piles.pending.find((person) => person.worth_key === pub)
    const row = from.find((other) => other.person.worth_key === pub)
    piles.pending = piles.pending.filter((person) => person !== pending)
    if (row) from.splice(from.indexOf(row), 1)
    const moved = row ?? (pending ? decisionRow({ person: pending }) : null)
    if (!moved) return refusal("worth row not found", 404)
    piles[to].push(moved)
    const counts = progress()
    const next_stage = counts.worth_pending === 0 ? "enrich" : "worth"
    return jsonResponse(worthResult({ pub, effective: to, progress: counts, next_stage }))
  }

  const card = vi.fn((query: URLSearchParams): Answer => readCard(query))
  const pending = vi.fn((): Answer => {
    const names = [...piles.pending].sort(byName).map(({ worth_key, name }) => ({ key: worth_key, name }))
    return jsonResponse({ pending: names })
  })
  const table = vi.fn((query: URLSearchParams): Answer => readTable(query))
  const details = vi.fn((query: URLSearchParams): Answer => readDetails(query))
  const worth = vi.fn((form: URLSearchParams): Answer => save(form))
  const dossier = vi.fn((_query: URLSearchParams): Answer => new Response("<p>Met at Acme.</p>"))

  const fetch = vi.fn((url: string, init?: RequestInit): Promise<Response> => {
    const { pathname, searchParams } = new URL(url, "http://review.test")
    const form = init?.body instanceof URLSearchParams ? init.body : new URLSearchParams()
    const routes: Record<string, () => Answer> = {
      "/api/review/worth-card": () => card(searchParams),
      "/api/review/worth-pending": () => pending(),
      "/api/review/worth-table": () => table(searchParams),
      "/api/review/worth-details": () => details(searchParams),
      "/api/dossier": () => dossier(searchParams),
      "/worth": () => worth(form),
    }
    const route = routes[pathname]
    if (!route) return Promise.reject(new Error(`unexpected request: ${url}`))
    return Promise.resolve().then(route)
  })

  /** Holds the next POST /worth until the gate opens; it then saves, or answers `instead`. */
  function holdSave(instead?: () => Response) {
    const held = gate()
    worth.mockImplementationOnce(async (form) => {
      await held.opened
      return instead ? instead() : save(form)
    })
    return held
  }

  /** Every GET sent to a path, in order, as written. */
  const reads = (path: string) => fetch.mock.calls.map(([url]) => url).filter((url) => url.startsWith(path))
  /** Every POST /worth body, in order. */
  const saves = () => worth.mock.calls.map(([form]) => Object.fromEntries(form))

  return {
    piles,
    progress,
    card,
    pending,
    table,
    details,
    worth,
    dossier,
    fetch,
    reads,
    saves,
    readCard,
    readTable,
    save,
    holdSave,
  }
}

export type WorthServer = ReturnType<typeof worthServer>

/** The three tabs as they read: "Review3", "Yes5", "No2". */
export const tabText = () => [...document.querySelectorAll(".decision-tab")].map((tab) => tab.textContent)

/** The address the stage's links opened (worth-harness.tsx shows it). */
export const where = () => document.querySelector("[data-where]")?.textContent

/** The page's `Review` with every action a spy, and the counts the server starts with. */
export function spiedReview(server: WorthServer, overrides: Partial<Review> = {}) {
  const spies = {
    toast: vi.fn<Review["toast"]>(),
    toastError: vi.fn<Review["toastError"]>(),
    applyProgress: vi.fn<Review["applyProgress"]>(),
    transition: vi.fn<Review["transition"]>(),
    reload: vi.fn<Review["reload"]>(),
    leaveAndReload: vi.fn<Review["leaveAndReload"]>(),
  }
  const review = fakeReview({ progress: pageProgress(server.progress()), ...spies, ...overrides })
  return { review, ...spies }
}

/** The worth stage on `tab`, against `server`. */
export function renderWorth(tab: WorthTab, server: WorthServer, overrides: Partial<Review> = {}) {
  const spied = spiedReview(server, overrides)
  const path = `/review?stage=worth&view=${tab}`
  const view = render(<WorthHarness tab={tab} review={spied.review} path={path} />)
  return { ...view, ...spied }
}
