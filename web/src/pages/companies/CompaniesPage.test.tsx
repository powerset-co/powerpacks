import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
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
function sent(init: RequestInit | undefined): string {
  return typeof init?.body === "string" ? init.body : ""
}

function list(companies: Company[]): CompanySearchResponse {
  return { companies, total: companies.length, limit: 100, offset: 0 }
}

it("suggests from the local index, lists the matches, and opens a company's people", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ sets: [], local_db: "/idx/local-search.duckdb" })
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (url.startsWith("/api/companies/suggest"))
      return Promise.resolve(json({ suggestions: [{ id: "c-stripe", display_value: "Stripe" }] }))
    if (!sent(init).includes('"include_people":true')) return Promise.resolve(json(list([STRIPE])))
    return Promise.resolve(
      json(
        list([
          {
            ...STRIPE,
            people_offset: 0,
            people_has_more: false,
            people: [
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
            ],
          },
        ]),
      ),
    )
  })
  vi.stubGlobal("fetch", fetch)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <CompaniesPage />
    </QueryClientProvider>,
  )

  // The picker starts on the local index, so the typo goes to the local suggest.
  await screen.findByRole("combobox", { name: "Search in" })
  fireEvent.change(screen.getByPlaceholderText("e.g., Stripe, OpenAI..."), { target: { value: "strpie" } })
  fireEvent.focus(screen.getByPlaceholderText("e.g., Stripe, OpenAI..."))
  fireEvent.click(await screen.findByRole("button", { name: "Stripe" }, { timeout: 2000 }))
  expect(fetch.mock.calls[0]?.[0]).toBe("/api/companies/suggest?q=strpie&scope=local")
  // Picking a suggestion does not search; Search does.
  expect(fetch).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole("button", { name: "Search" }))
  const results = await screen.findByRole("region", { name: "Results" })
  expect(JSON.parse(sent(fetch.mock.calls[1]?.[1]))).toEqual({
    scope: "local",
    name: "Stripe",
    position_type: "all",
    limit: 100,
    offset: 0,
  })
  expect(within(results).getByText("San Francisco, CA, United States")).toBeTruthy()
  // No LinkedIn icons in the company list.
  expect(within(results).queryByRole("link")).toBeNull()

  fireEvent.click(within(results).getByRole("button", { name: "Stripe" }))
  const people = await screen.findByRole("region", { name: "People" })
  await within(people).findByText("Ada Lovelace")
  expect(JSON.parse(sent(fetch.mock.calls[2]?.[1]))).toEqual({
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

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <CompaniesPage />
    </QueryClientProvider>,
  )
}

it("a new scope drops the pick and asks the new scope for suggestions", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({
    sets: [{ set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40 }],
    local_db: "/idx/local-search.duckdb",
  })
  const fetch = vi.fn((url: string) =>
    Promise.resolve(
      json({
        suggestions: [
          {
            id: url.includes("scope=local") ? "c-local" : "c-cloud",
            display_value: url.includes("scope=local") ? "Stripe" : "Stripe Inc",
          },
        ],
      }),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  renderPage()
  await screen.findByRole("combobox", { name: "Search in" })
  const box = () => screen.getByPlaceholderText("e.g., Stripe, OpenAI...")
  fireEvent.change(box(), { target: { value: "strpie" } })
  fireEvent.focus(box())
  fireEvent.click(await screen.findByRole("button", { name: "Stripe" }, { timeout: 2000 }))
  expect(screen.getByRole("button", { name: "Search" })).toHaveProperty("disabled", false)

  pick("Search in", /Team/)
  expect(screen.getByRole("button", { name: "Search" })).toHaveProperty("disabled", true)
  expect(box()).toHaveProperty("value", "")
  fireEvent.change(box(), { target: { value: "strpie" } })
  fireEvent.focus(box())
  await screen.findByRole("button", { name: "Stripe Inc" }, { timeout: 2000 })
  expect(fetch.mock.calls.at(-1)?.[0]).toBe("/api/companies/suggest?q=strpie&scope=t-1")
})

it("hides the old results while a new search runs", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ sets: [], local_db: "/idx/local-search.duckdb" })
  let release: (response: Response) => void = () => undefined
  const fetch = vi.fn((url: string) => {
    if (url.startsWith("/api/companies/suggest"))
      return Promise.resolve(json({ suggestions: [{ id: "c-stripe", display_value: "Stripe" }] }))
    if (fetch.mock.calls.filter(([called]) => called === "/api/companies/search").length === 1)
      return Promise.resolve(json(list([STRIPE])))
    return new Promise<Response>((resolve) => {
      release = resolve
    })
  })
  vi.stubGlobal("fetch", fetch)
  renderPage()
  await screen.findByRole("combobox", { name: "Search in" })
  fireEvent.change(screen.getByPlaceholderText("e.g., Stripe, OpenAI..."), { target: { value: "strpie" } })
  fireEvent.focus(screen.getByPlaceholderText("e.g., Stripe, OpenAI..."))
  fireEvent.click(await screen.findByRole("button", { name: "Stripe" }, { timeout: 2000 }))
  fireEvent.click(screen.getByRole("button", { name: "Search" }))
  await screen.findByRole("region", { name: "Results" })

  fireEvent.click(screen.getByRole("button", { name: "Search" }))
  expect(screen.queryByRole("region", { name: "Results" })).toBeNull()
  release(json(list([STRIPE])))
  await screen.findByRole("region", { name: "Results" })
})
