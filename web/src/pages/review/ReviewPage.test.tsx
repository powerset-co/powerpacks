import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { useEffect } from "react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { errorResponse, jsonResponse, motionMedia, pageProgress, reviewPage } from "@/testing/review-fixture"
import type { ReviewPage as ReviewPageData } from "@/types/review"

import { useReview, type Review } from "./hooks/useReview"
import { ReviewPage } from "./ReviewPage"

// The stage is another suite's business: a probe here that hands the test the page's `Review`.
const seen = vi.hoisted((): { review: Review | null; mounts: number } => ({ review: null, mounts: 0 }))

function Probe() {
  const review = useReview()
  useEffect(() => {
    seen.review = review
  })
  useEffect(() => {
    seen.mounts += 1
  }, [])
  return <div data-probe="linkedin" />
}

vi.mock("./linkedin/LinkedinStage", () => ({
  LinkedinStage: () => <Probe />,
}))

/** The page server: one page per `stage` asked for ("" is the store's own stage). */
const server: { pages: Record<string, ReviewPageData | Response> } = { pages: {} }

const fetchMock = vi.fn((url: string, _init?: RequestInit): Promise<Response> => {
  const { pathname, searchParams } = new URL(url, "http://review.test")
  if (pathname !== "/api/review/page") return Promise.reject(new Error(`unexpected request: ${url}`))
  const page = must(server.pages[searchParams.get("stage") ?? ""], `a page for ${url}`)
  return Promise.resolve(page instanceof Response ? page.clone() : jsonResponse(page))
})

const requests = (path: string) =>
  fetchMock.mock.calls.map(([url]) => url).filter((url) => url.startsWith(path))
const review = () => must(seen.review, "the stage's review")
const probe = () => document.querySelector("[data-probe='linkedin']")

function renderPage(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/" element={<ReviewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  seen.review = null
  seen.mounts = 0
  server.pages = { "": reviewPage(), linkedin: reviewPage() }
  fetchMock.mockClear()
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ReviewPage: the screen", () => {
  it("opens the Check LinkedIn screen with the URL's settings", async () => {
    const { container } = renderPage("/?stage=linkedin&preview=1&debug=1&index=3")
    await waitFor(() => expect(probe()).toBeTruthy())
    expect(requests("/api/review/page")).toEqual(["/api/review/page?stage=linkedin"])
    const root = must(container.querySelector(".review-page"))
    expect(root.getAttribute("data-stage")).toBe("linkedin")
    expect(root.getAttribute("data-preview")).toBe("true")
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Check LinkedIn")
    expect(document.title).toBe("Check LinkedIn · Powerpacks")
    expect([review().preview, review().debug, review().index]).toEqual([true, true, 3])
  })

  it("lands on the store's stage when the URL names none", async () => {
    const { container } = renderPage("/")
    await waitFor(() => expect(probe()).toBeTruthy())
    expect(requests("/api/review/page")).toEqual(["/api/review/page"])
    expect(container.querySelector(".review-page")?.getAttribute("data-stage")).toBe("linkedin")
    expect([review().preview, review().debug, review().index]).toEqual([false, false, 0])
  })

  it("shows the synthesis handoff instead of the stage when synthesis has not run", async () => {
    server.pages[""] = reviewPage({
      progress: pageProgress({ synthesize_pending: 4 }),
      needs_synthesis: true,
    })
    renderPage("/")
    await waitFor(() => expect(screen.getByRole("heading", { name: "Synthesis has not run" })).toBeTruthy())
    expect(probe()).toBeNull()
    expect(screen.getByText("bin/deep-context-v2 run")).toBeTruthy()
  })

  it("draws its own top bar: the brand reopens the review, and no page tabs", async () => {
    const { container } = renderPage("/?stage=linkedin")
    await waitFor(() => expect(probe()).toBeTruthy())
    const brand = must(container.querySelector(".topbar .brand"))
    expect(brand.textContent).toBe("POWERPACKS")
    expect(brand.getAttribute("href")).toBe("/")
    expect(container.querySelector("[data-nav]")).toBeNull()
    expect(screen.queryAllByRole("navigation")).toHaveLength(0)
  })

  it("says so when the screen cannot load", async () => {
    server.pages.linkedin = errorResponse("review store is locked", 500)
    renderPage("/?stage=linkedin")
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Could not load the review" })).toBeTruthy(),
    )
    expect(screen.getByText("review store is locked")).toBeTruthy()
  })
})

describe("ReviewPage: the toast", () => {
  it("shows a stage's message and replaces it with the next", async () => {
    renderPage("/?stage=linkedin")
    await waitFor(() => expect(probe()).toBeTruthy())
    act(() => review().toast("Added"))
    expect(screen.getByRole("status").textContent).toBe("Added")
    act(() => review().toastError("stale or mismatched person card"))
    expect(screen.getByRole("status").textContent).toBe("stale or mismatched person card")
  })

  it("fades the stage and reads the screen again on leave-and-reload", async () => {
    renderPage("/?stage=linkedin")
    await waitFor(() => expect(probe()).toBeTruthy())
    act(() => review().leaveAndReload("Saved"))
    expect(screen.getByRole("status").textContent).toBe("Saved")
    await waitFor(() => expect(requests("/api/review/page")).toHaveLength(2))
    await waitFor(() => expect(seen.mounts).toBe(2))
    expect(document.querySelector(".stage")?.className).toBe("stage")
  })
})
