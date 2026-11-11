import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"

import { fetchSearchScope } from "@/lib/api/searches"
import { pick } from "@/testing/select"
import type { Company, CompanySearchResponse } from "@/types/companies"
import { CompaniesPage } from "./CompaniesPage"

vi.mock("@/lib/api/searches", () => ({ fetchSearchScope: vi.fn() }))
// jsdom has no layout or ResizeObserver: the checkbox watches its size, and the tables'
// virtualizer reads the viewport's height to mount rows.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 800 })
  if (!("ResizeObserver" in globalThis))
    Object.defineProperty(globalThis, "ResizeObserver", {
      value: class {
        observe = () => undefined
        unobserve = () => undefined
        disconnect = () => undefined
      },
    })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

const STRIPE: Company = {
  id: "c-stripe",
  name: "Stripe",
  headcount: 8000,
  city: "San Francisco",
  state: "CA",
  country: "United States",
  logo_url: null,
  people_count: 2,
}

function json(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

/** The JSON a request carried. */
function sent(init: RequestInit | undefined): unknown {
  return typeof init?.body === "string" ? JSON.parse(init.body) : null
}

function list(companies: Company[]): CompanySearchResponse {
  return { companies, total: companies.length, limit: 100, offset: 0 }
}

const SCOPE = {
  sets: [{ set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40 }],
  local_db: "/idx/local-search.duckdb",
}

const PEOPLE = [
  {
    id: "p1",
    name: "Ada Lovelace",
    public_identifier: "ada",
    position_title: "Engineer",
    is_current: true,
    tenure_years: 3,
  },
  {
    id: "p2",
    name: "Alan Turing",
    public_identifier: null,
    position_title: "Researcher",
    is_current: false,
    tenure_years: 1,
  },
]

/** The page over a mocked server: a list request answers Stripe, a company request its people. */
function setup() {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  const fetch = vi.fn((_url: string, init?: RequestInit) => {
    const people = typeof init?.body === "string" && init.body.includes('"include_people":true')
    return Promise.resolve(
      json(list([people ? { ...STRIPE, people_offset: 0, people_has_more: false, people: PEOPLE } : STRIPE])),
    )
  })
  vi.stubGlobal("fetch", fetch)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <CompaniesPage />
    </QueryClientProvider>,
  )
  const requests = () => fetch.mock.calls.map(([, init]) => sent(init))
  return { fetch, requests }
}

const listRequest = (scope: string, name: string, sort = "people", dir = "desc") => ({
  scope,
  name,
  position_type: "all",
  sort,
  dir,
  limit: 100,
  offset: 0,
})

it("loads every company on open, on the local index", async () => {
  const { fetch, requests } = setup()
  const results = await screen.findByRole("region", { name: "Companies" })
  expect(within(results).getByText("San Francisco, CA, United States")).toBeTruthy()
  expect(screen.getByText("1 company")).toBeTruthy()
  expect(fetch.mock.calls[0]?.[0]).toBe("/api/companies/search")
  expect(requests()).toEqual([listRequest("local", "")])
  // No LinkedIn icons in the company list.
  expect(within(results).queryByRole("link")).toBeNull()
})

it("asks for the typed name once typing settles", async () => {
  const { requests } = setup()
  await screen.findByRole("region", { name: "Companies" })
  fireEvent.change(screen.getByRole("searchbox", { name: "Search companies" }), {
    target: { value: "strpie" },
  })
  expect(requests()).toEqual([listRequest("local", "")])
  await waitFor(() => expect(requests()).toContainEqual(listRequest("local", "strpie")))
})

it("a header click sorts the list by it, a second click flips it", async () => {
  const { requests } = setup()
  const results = await screen.findByRole("region", { name: "Companies" })
  const headcount = within(results).getByRole("columnheader", { name: "Headcount" })
  expect(within(results).getByRole("columnheader", { name: "People" }).getAttribute("aria-sort")).toBe(
    "descending",
  )
  fireEvent.click(within(headcount).getByRole("button"))
  await waitFor(() => expect(requests()).toContainEqual(listRequest("local", "", "headcount", "desc")))
  expect(headcount.getAttribute("aria-sort")).toBe("descending")
  fireEvent.click(within(headcount).getByRole("button"))
  await waitFor(() => expect(requests()).toContainEqual(listRequest("local", "", "headcount", "asc")))
  fireEvent.click(
    within(within(results).getByRole("columnheader", { name: "Company Name" })).getByRole("button"),
  )
  await waitFor(() => expect(requests()).toContainEqual(listRequest("local", "", "name", "asc")))
})

it("reloads the list for a new scope", async () => {
  const { requests } = setup()
  await screen.findByRole("region", { name: "Companies" })
  pick("Search in", /Team/)
  await waitFor(() => expect(requests()).toContainEqual(listRequest("t-1", "")))
})

it("opens a company's people with the scope its list was loaded from", async () => {
  const { requests } = setup()
  const results = await screen.findByRole("region", { name: "Companies" })
  fireEvent.click(within(results).getByRole("button", { name: "Stripe" }))
  const people = await screen.findByRole("region", { name: "People" })
  await within(people).findByText("Ada Lovelace")
  expect(requests().at(-1)).toEqual({
    scope: "local",
    id: "c-stripe",
    name: "Stripe",
    include_people: true,
    position_type: "all",
    people_limit: 50,
    people_offset: 0,
    people_sort: "current",
    people_dir: "desc",
    limit: 10,
  })
  // A LinkedIn icon for the person with a profile, none for the one without.
  expect(
    within(people)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href")),
  ).toEqual(["https://www.linkedin.com/in/ada"])
  // The name box narrows the loaded page.
  fireEvent.change(within(people).getByLabelText("Search people by name"), { target: { value: "tur" } })
  expect(within(people).queryByText("Ada Lovelace")).toBeNull()
  expect(within(people).getByText("Alan Turing")).toBeTruthy()
})
