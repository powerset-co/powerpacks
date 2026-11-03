import { describe, expect, it } from "vitest"

import { resultRow } from "@/testing/searches-fixture"
import type { PondCandidate, Position } from "@/types/searches"

import { currentRole, roleLine, shownHeadline } from "./person"

function position(fields: Partial<Position>): Position {
  return {
    title: "",
    company: "",
    company_url: "",
    start_date: "",
    end_date: "",
    is_current: false,
    description: "",
    headcount: 0,
    stage: "",
    funding: 0,
    logo_url: "",
    ...fields,
  }
}

function person(fields: Partial<PondCandidate>): PondCandidate {
  return resultRow("p-jordan", "Jordan Bravo", { pond: { positions: [], headline: "", ...fields } }).row
}

describe("currentRole", () => {
  it("reads the current position, logo included, not the headline", () => {
    const row = person({
      title: "Engineer",
      company: "Old Co",
      headline: "Builder of things",
      positions: [
        position({ title: "Advisor", company: "Past Co" }),
        position({
          title: "Software Engineer",
          company: "Roblox",
          is_current: true,
          logo_url: "https://x/logo.png",
        }),
      ],
    })
    expect(currentRole(row)).toEqual({
      title: "Software Engineer",
      company: "Roblox",
      logo: "https://x/logo.png",
    })
  })

  it("falls back to the run's title and company, with that company's logo", () => {
    const row = person({
      title: "Staff Engineer",
      company: "Stripe",
      positions: [position({ title: "Staff Engineer", company: "Stripe", logo_url: "https://x/s.png" })],
    })
    expect(currentRole(row)).toEqual({ title: "Staff Engineer", company: "Stripe", logo: "https://x/s.png" })
    expect(roleLine(currentRole(row))).toBe("Staff Engineer · Stripe")
  })
})

describe("shownHeadline", () => {
  it("drops a headline that only repeats the role", () => {
    const row = person({ title: "Staff Engineer", company: "Stripe", headline: "Staff Engineer at Stripe" })
    expect(shownHeadline(row, currentRole(row))).toBe("")
  })

  it("keeps a headline that says more", () => {
    const row = person({ title: "Staff Engineer", company: "Stripe", headline: "Payments infra, ex-Dropbox" })
    expect(shownHeadline(row, currentRole(row))).toBe("Payments infra, ex-Dropbox")
  })
})
