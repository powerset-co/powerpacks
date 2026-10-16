import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { RUN } from "@/testing/searches-fixture"

import { RunHeader } from "./RunHeader"

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

// A quick search saved with its screened people, still ranking.
const SCREENED = { ...RUN.search, jd_text: "", team: [] }

describe("RunHeader", () => {
  it("says a running search is running, with the people it has", () => {
    render(<RunHeader search={SCREENED} status="running" people={1434} actions={null} />)
    const badge = screen.getByText("Running")
    expect(badge.dataset.status).toBe("running")
    expect(screen.getByText("1,434 people").hasAttribute("data-people-count")).toBe(true)
  })

  it("says a completed search is complete", () => {
    render(<RunHeader search={SCREENED} status="completed" people={25} actions={null} />)
    expect(screen.getByText("Search complete").dataset.status).toBe("completed")
    expect(screen.queryByText("Running")).toBeNull()
  })
})
