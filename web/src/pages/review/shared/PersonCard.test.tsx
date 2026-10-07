import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { fakeReview, reviewCandidate, reviewPerson, syntheticCandidate } from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

import { PersonCard } from "./PersonCard"

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("<p>Met at Acme.</p>"))),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderCard(person: ReviewPerson, candidate: ReviewCandidate | null, personOnly = false) {
  const { container } = render(
    <ReviewHarness review={fakeReview()}>
      <PersonCard person={person} candidate={candidate} personOnly={personOnly} />
    </ReviewHarness>,
  )
  const facts = () =>
    [...container.querySelectorAll(".details > dl > div")].map((row) => [
      row.querySelector("dt")?.textContent,
      row.querySelector("dd")?.textContent,
    ])
  return { container, facts }
}

describe("PersonCard", () => {
  it("draws the person: sources, name, labels, the LinkedIn link, the facts and the dossier", async () => {
    const { container, facts } = renderCard(reviewPerson(), reviewCandidate())
    expect([...container.querySelectorAll(".source")].map((badge) => badge.textContent)).toEqual([
      "Gmail",
      "iMessage",
    ])
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Jordan Bravo")
    expect([...container.querySelectorAll(".person-label")].map((label) => label.textContent)).toEqual([
      "Founder",
      "Close friend",
    ])
    const link = screen.getByRole("link", { name: "View LinkedIn" })
    expect(link.parentElement?.className).toBe("name-row")
    expect(link.getAttribute("href")).toBe("https://www.linkedin.com/in/jordan-bravo")
    expect(link.getAttribute("target")).toBe("_blank")
    expect(facts()).toEqual([
      ["Contact", "jordan@example.com · +15550100"],
      ["Summary", "Founder at Example Labs"],
      ["Location", "Springfield"],
      ["Work", "Founder, Example LabsEngineer, Acme"],
      ["Education", "Example University"],
    ])
    await waitFor(() =>
      expect(container.querySelector(".details .dossier-text p")?.textContent).toBe("Met at Acme."),
    )
  })

  it("draws initials with the candidate's picture over them", () => {
    const { container } = renderCard(reviewPerson(), reviewCandidate())
    const avatar = container.querySelector(".profile-card > .avatar")
    expect(avatar?.textContent).toBe("JB")
    expect(avatar?.querySelector("img")?.getAttribute("src")).toBe(
      "https://media.example.com/jordan-bravo.jpg",
    )
  })

  it("draws initials alone for a profile with no picture", () => {
    const { container } = renderCard(reviewPerson(), reviewCandidate({ avatar_url: "" }))
    expect(container.querySelector(".avatar img")).toBeNull()
    expect(container.querySelector(".avatar")?.textContent).toBe("JB")
  })

  it("leaves the dossier to a holder that draws it itself", () => {
    const { container } = render(
      <ReviewHarness review={fakeReview()}>
        <PersonCard person={reviewPerson()} candidate={reviewCandidate()} dossier={false} />
      </ReviewHarness>,
    )
    expect(container.querySelector(".dossier-text")).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
    expect(container.querySelectorAll(".details dt")).toHaveLength(5)
  })

  it("draws no picture and no link for a researched profile", () => {
    const { container } = renderCard(reviewPerson(), syntheticCandidate())
    expect(container.querySelector(".avatar img")).toBeNull()
    expect(screen.queryByRole("link")).toBeNull()
  })

  it("leaves out the facts a profile lacks", () => {
    const bare = reviewCandidate({
      headline: "--",
      location: "",
      experiences: [],
      education: [],
    })
    const { container, facts } = renderCard(reviewPerson({ sources: [], labels: [], contacts: "" }), bare)
    expect(facts()).toEqual([])
    expect(container.querySelector(".eyebrow-row")).toBeNull()
    expect(container.querySelector(".person-labels")).toBeNull()
  })

  it("draws a person with no candidate from the parent alone", () => {
    const { container, facts } = renderCard(reviewPerson({ name: "" }), null)
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("This person")
    expect(container.querySelector(".avatar")?.textContent).toBe("?")
    // The contact is the person's, so a card with no candidate still shows it.
    expect(facts()).toEqual([["Contact", "jordan@example.com · +15550100"]])
  })

  it("draws the person alone on a person-only card: the parent's name, the contact, the dossier", () => {
    const person = reviewPerson({ name: "Jordan B" })
    const { container, facts } = renderCard(person, reviewCandidate(), true)
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Jordan B")
    expect(screen.queryByRole("link")).toBeNull()
    expect(facts()).toEqual([["Contact", "jordan@example.com · +15550100"]])
    // No option is confirmed yet, so the avatar draws the parent's initials, not an option's photo.
    expect(container.querySelector(".avatar img")).toBeNull()
    expect(container.querySelector(".avatar")?.textContent).toBe("JB")
    expect(container.querySelector(".details .dossier-text")).toBeTruthy()
  })

  it("names an unknown source as written", () => {
    const { container } = renderCard(reviewPerson({ sources: ["whatsapp", "signal"] }), reviewCandidate())
    expect([...container.querySelectorAll(".source")].map((badge) => badge.textContent)).toEqual([
      "WhatsApp",
      "signal",
    ])
    expect(container.querySelector(".source-signal i")?.className).toBe("")
  })

  it("folds labels past three into +N, whose tooltip lists the rest and takes focus", () => {
    const labels = ["Founder", "Investor", "Classmate", "Coworker", "Family"]
    const { container } = renderCard(reviewPerson({ labels }), reviewCandidate())
    expect(container.querySelectorAll(".person-label")).toHaveLength(3)
    const more = screen.getByRole("button", { name: "More labels: Coworker · Family" })
    expect(more.textContent).toBe("+2Coworker · Family")
    expect(more.tabIndex).toBe(0)
    expect(screen.getByRole("tooltip").textContent).toBe("Coworker · Family")
  })

  it("folds a long Work list", () => {
    const experiences = ["a", "b", "c", "d"]
    renderCard(reviewPerson(), reviewCandidate({ experiences, education: [] }))
    fireEvent.click(screen.getByRole("button", { name: "+ show 1 more" }))
    expect(screen.getByRole("button", { name: "show fewer" })).toBeTruthy()
  })
})
