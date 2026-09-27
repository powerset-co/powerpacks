import { beforeEach, describe, expect, it } from "vitest"

import { readView, writeView } from "./view"

const KEY = "powerpacks:people-filters:v2"
const SAVED = { tab: "no", filters: { worth: ["yes"] }, text: "acme", sort: { key: "last", dir: -1 } }

const store = (value: unknown) => sessionStorage.setItem(KEY, JSON.stringify(value))

beforeEach(() => sessionStorage.clear())

describe("saved view", () => {
  it("round-trips the view", () => {
    writeView({
      tab: "no",
      filters: new Map([["worth", new Set(["yes"])]]),
      text: "acme",
      sort: { key: "last", dir: -1 },
    })
    const view = readView()
    expect(view?.tab).toBe("no")
    expect([...(view?.filters.get("worth") ?? [])]).toEqual(["yes"])
    expect(view?.sort).toEqual({ key: "last", dir: -1 })
  })

  it("rejects a saved unknown sort key or direction", () => {
    store({ ...SAVED, sort: { key: "gone", dir: 1 } })
    expect(readView()).toBeNull()
    store({ ...SAVED, sort: { key: "name", dir: 2 } })
    expect(readView()).toBeNull()
  })

  it("rejects an unknown tab or facet, non-string values, and junk", () => {
    store({ ...SAVED, tab: "maybe" })
    expect(readView()).toBeNull()
    store({ ...SAVED, filters: { gone: ["x"] } })
    expect(readView()).toBeNull()
    store({ ...SAVED, filters: { worth: [1] } })
    expect(readView()).toBeNull()
    sessionStorage.setItem(KEY, "{")
    expect(readView()).toBeNull()
  })
})
