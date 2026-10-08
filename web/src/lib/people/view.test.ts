import { describe, expect, it } from "vitest"

import { DEFAULT_VIEW, readView, writeView, type PeopleView } from "./view"

describe("view query parameters", () => {
  it("round-trips multiple facet values, search, tab and descending sort", () => {
    const view: PeopleView = {
      tab: "no",
      filters: new Map([["worth", new Set(["yes", "maybe"])]]),
      text: "Acme & partners + friends",
      sort: { key: "last", dir: -1 },
    }
    const params = writeView(view, new URLSearchParams("person=p1"))
    expect(readView(new URLSearchParams(params.toString()))).toEqual(view)
    expect(params.get("person")).toBe("p1")
  })

  it("uses defaults for missing or invalid tab and sort", () => {
    expect(readView(new URLSearchParams())).toEqual(DEFAULT_VIEW)
    expect(readView(new URLSearchParams("tab=gone&sort=gone&dir=gone&filter.gone=x"))).toEqual(DEFAULT_VIEW)
  })

  it("removes cleared filters, search and sort while preserving other parameters", () => {
    const params = writeView(
      DEFAULT_VIEW,
      new URLSearchParams("filter.worth=yes&q=acme&sort=last&dir=desc&person=p1"),
    )
    expect(params.toString()).toBe("person=p1&tab=confirm")
  })
})
