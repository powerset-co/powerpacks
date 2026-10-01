import { describe, expect, it } from "vitest"

import { reviewCandidate, reviewPerson, syntheticCandidate } from "@/testing/review-fixture"

import {
  avatarKey,
  avatarName,
  displayName,
  foldFacts,
  foldLabels,
  labelTooltip,
  profileUrl,
  summaryOf,
} from "./person"

describe("displayName", () => {
  const person = reviewPerson({ name: "Jordan B" })

  it("prefers the candidate's full name", () => {
    expect(displayName(person, reviewCandidate({ name: "Jordan Bravo" }), false)).toBe("Jordan Bravo")
  })

  it("falls back to the parent's name, then to a placeholder", () => {
    expect(displayName(person, reviewCandidate({ name: "" }), false)).toBe("Jordan B")
    expect(displayName(person, null, false)).toBe("Jordan B")
    expect(displayName(reviewPerson({ name: "" }), null, false)).toBe("This person")
  })

  it("names the parent on a person-only card", () => {
    expect(displayName(person, reviewCandidate({ name: "Jordan Bravo" }), true)).toBe("Jordan B")
    expect(displayName(reviewPerson({ name: "" }), reviewCandidate(), true)).toBe("This person")
  })
})

describe("profileUrl", () => {
  it("links a fetched candidate", () => {
    expect(profileUrl(reviewCandidate(), false)).toBe("https://www.linkedin.com/in/jordan-bravo")
  })

  it("has no link for a researched profile, no candidate, or a person-only card", () => {
    expect(profileUrl(syntheticCandidate({ url: "https://example.com/research" }), false)).toBe("")
    expect(profileUrl(null, false)).toBe("")
    expect(profileUrl(reviewCandidate(), true)).toBe("")
  })
})

describe("summaryOf", () => {
  it("is the headline unless the profile had none", () => {
    expect(summaryOf(reviewCandidate({ headline: "Founder" }))).toBe("Founder")
    expect(summaryOf(reviewCandidate({ headline: "--" }))).toBe("")
    expect(summaryOf(reviewCandidate({ headline: "" }))).toBe("")
  })
})

describe("the avatar", () => {
  it("draws the candidate's name, else the parent's", () => {
    expect(avatarName(reviewPerson({ name: "Jordan B" }), reviewCandidate({ name: "Casey Delta" }))).toBe(
      "Casey Delta",
    )
    expect(avatarName(reviewPerson({ name: "Jordan B" }), reviewCandidate({ name: "" }))).toBe("Jordan B")
    expect(avatarName(reviewPerson({ name: "Jordan B" }), null)).toBe("Jordan B")
  })

  it("asks for a fetched candidate's picture only", () => {
    expect(avatarKey(reviewCandidate())).toBe("jordan-bravo-1")
    expect(avatarKey(syntheticCandidate())).toBe("")
    expect(avatarKey(reviewCandidate({ row_key: "" }))).toBe("")
    expect(avatarKey(null)).toBe("")
  })
})

describe("foldFacts", () => {
  it("shows three and folds the rest, dropping blank entries", () => {
    expect(foldFacts(["a", " ", "b", "", "c", "d", "e"])).toEqual({
      shown: ["a", "b", "c"],
      rest: ["d", "e"],
    })
  })

  it("folds nothing for three or fewer", () => {
    expect(foldFacts(["a", "b", "c"])).toEqual({ shown: ["a", "b", "c"], rest: [] })
  })
})

describe("foldLabels", () => {
  it("shows three and lists the rest in the tooltip", () => {
    const { shown, rest } = foldLabels(["Founder", "Investor", "Classmate", "Coworker", "Family"])
    expect(shown).toEqual(["Founder", "Investor", "Classmate"])
    expect(labelTooltip(rest)).toBe("Coworker · Family")
  })
})
