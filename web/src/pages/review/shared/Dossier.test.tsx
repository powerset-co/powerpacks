import { cleanup, render, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { fakeReview } from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"

import { Dossier } from "./Dossier"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderDossier(answer: () => Promise<Response>) {
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) => answer())
  vi.stubGlobal("fetch", fetchMock)
  const { container } = render(
    <ReviewHarness review={fakeReview()}>
      <Dossier slug="jordan-bravo" className="row-facts" />
    </ReviewHarness>,
  )
  const text = () => container.querySelector(".dossier-text")
  return { fetchMock, text }
}

describe("Dossier", () => {
  it("says Loading, then injects the server's HTML, asking once", async () => {
    const { fetchMock, text } = renderDossier(() =>
      Promise.resolve(new Response("<h4>Summary</h4><p>Met at <strong>Acme</strong>.</p>")),
    )
    expect(text()?.textContent).toBe("Loading…")
    expect(text()?.getAttribute("aria-busy")).toBe("true")
    expect(text()?.className).toBe("dossier-text row-facts")
    await waitFor(() => expect(text()?.querySelector("h4")?.textContent).toBe("Summary"))
    expect(text()?.querySelector("strong")?.textContent).toBe("Acme")
    expect(text()?.hasAttribute("aria-busy")).toBe(false)
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/dossier?slug=jordan-bravo&skip=1"])
  })

  it("says so when the server has none", async () => {
    const { text } = renderDossier(() => Promise.resolve(new Response("", { status: 404 })))
    await waitFor(() => expect(text()?.textContent).toBe("No details found"))
    expect(text()?.hasAttribute("aria-busy")).toBe(false)
  })

  it("says so when the request fails", async () => {
    const { text } = renderDossier(() => Promise.reject(new TypeError("Failed to fetch")))
    await waitFor(() => expect(text()?.textContent).toBe("Could not load details"))
  })
})
