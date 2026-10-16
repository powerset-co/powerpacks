import { afterEach, describe, expect, it, vi } from "vitest"

import { BASE, PAYLOAD } from "@/testing/people-fixture"
import { PERSON_COLUMNS } from "@/types/people"

import { decodePeople, fetchPersonDetail, writeWorth } from "./people"

afterEach(() => vi.unstubAllGlobals())

describe("decodePeople", () => {
  it("decodes columns by name and derives last, warmthBucket, search", () => {
    const [jordan] = decodePeople(PAYLOAD)
    expect(jordan).toMatchObject({
      // Unknown until the saved logbooks are read.
      logbook: null,
      parent_id: "p1",
      name: "Jordan Bravo",
      channels: ["gmail", "linkedin"],
      worth: "no",
      last: "< 1 year",
      warmthBucket: "Close (2–3)",
      search: "jordan bravo engineer acme ",
    })
  })

  it("lists every Person column the page decodes", () => {
    expect([...PERSON_COLUMNS].sort()).toEqual(Object.keys(BASE).sort())
  })

  it("rejects a payload missing a column", () => {
    const columns = PAYLOAD.columns.filter((column) => column !== "reason" && column !== "tags")
    expect(() => decodePeople({ ...PAYLOAD, columns })).toThrow(
      "People payload is missing columns: reason, tags",
    )
  })
})

describe("requests", () => {
  it("carries the server's error message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(new Response(JSON.stringify({ error: "person not found" }), { status: 404 })),
      ),
    )
    await expect(fetchPersonDetail("p9")).rejects.toThrow("person not found")
  })

  it("posts the human's worth call and returns the rows as the server holds them", async () => {
    const rows = [{ parent_id: "p1", worth: "yes", worth_source: "human", reason: "human_worth", tags: [] }]
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(new Response(JSON.stringify({ rows }))),
    )
    vi.stubGlobal("fetch", fetchMock)
    expect(await writeWorth(["p1"], "yes")).toEqual(rows)
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/people/worth")
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({ ids: ["p1"], worth: "yes" }))
  })
})
