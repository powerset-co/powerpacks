import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { motionMedia, pageProgress } from "@/testing/review-fixture"

import { stubListLayout } from "./list-layout"
import { renderWorth, rowNamed, where, worthServer, type WorthServer } from "./worth-fixture"

let server: WorthServer

beforeEach(() => {
  stubListLayout()
  server = worthServer({ yes: [rowNamed("Avery Fox")] })
  vi.stubGlobal("fetch", server.fetch)
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const PAGE = pageProgress({ worth_pending: 3, worth_yes: 5, worth_no: 2 })

const tabs = () => screen.getAllByRole("link").filter((link) => link.classList.contains("decision-tab"))
const tabText = () => tabs().map((tab) => tab.textContent)
const activeTab = () => tabs().filter((tab) => tab.classList.contains("active"))

// W1
describe("WorthStage: the tabs", () => {
  it("shows Review / Yes / No with their counts", () => {
    const { container } = renderWorth("review", server, { progress: PAGE })
    expect(container.querySelector(".worth-stage > nav.decision-tabs")).toBeTruthy()
    expect(tabText()).toEqual(["Review3", "Yes5", "No2"])
    expect(tabs().map((tab) => tab.querySelector("span")?.textContent)).toEqual(["3", "5", "2"])
    expect(tabs().map((tab) => tab.getAttribute("data-tab"))).toEqual(["review", "yes", "no"])
  })

  it.each(["review", "yes", "no"] as const)("marks the %s tab when it is on screen", (tab) => {
    renderWorth(tab, server, { progress: PAGE })
    expect(activeTab().map((link) => link.getAttribute("data-tab"))).toEqual([tab])
  })

  it("links each tab to its screen", () => {
    renderWorth("review", server, { progress: PAGE })
    expect(tabs().map((tab) => tab.getAttribute("href"))).toEqual([
      "/?stage=worth&view=review",
      "/?stage=worth&view=yes",
      "/?stage=worth&view=no",
    ])
  })

  it("keeps a deliberately opened screen one: the links carry preview=1", () => {
    renderWorth("review", server, { progress: PAGE, preview: true })
    expect(tabs().map((tab) => tab.getAttribute("href"))).toEqual([
      "/?stage=worth&view=review&preview=1",
      "/?stage=worth&view=yes&preview=1",
      "/?stage=worth&view=no&preview=1",
    ])
  })

  it("opens a tab in place: the address changes, the document does not load", () => {
    renderWorth("review", server, { progress: PAGE })
    expect(where()).toBe("/?stage=worth&view=review")
    fireEvent.click(screen.getByRole("link", { name: /^Yes/ }))
    expect(where()).toBe("/?stage=worth&view=yes")
  })
})

describe("WorthStage: the tab's content", () => {
  it("is the card queue with its typeahead on Review", async () => {
    const { container } = renderWorth("review", server, { progress: PAGE })
    await screen.findByRole("heading", { level: 2, name: "Casey Delta" })
    await screen.findByRole("searchbox", { name: "Search people by name" })
    const children = [...(container.querySelector(".worth-stage")?.children ?? [])]
    expect(children.map((child) => child.className)).toEqual(["decision-tabs", "worth-search", "worth-panel"])
    expect(container.querySelector(".worth-panel > .worth-card")).toBeTruthy()
    expect(server.reads("/api/review/worth-table")).toEqual([])
  })

  it.each(["yes", "no"] as const)("is the decided pile's list on %s, with no typeahead", async (tab) => {
    const { container } = renderWorth(tab, server, { progress: PAGE })
    await waitFor(() =>
      expect(container.querySelector(".worth-panel > .decision-list > .decision-table")).toBeTruthy(),
    )
    expect(container.querySelector(".decision-table")?.getAttribute("data-view")).toBe(tab)
    expect(screen.queryByRole("searchbox")).toBeNull()
    expect(server.reads("/api/review/worth-table")).toEqual([`/api/review/worth-table?view=${tab}&offset=0`])
    expect(server.reads("/api/review/worth-card")).toEqual([])
    expect(server.reads("/api/review/worth-pending")).toEqual([])
  })
})
