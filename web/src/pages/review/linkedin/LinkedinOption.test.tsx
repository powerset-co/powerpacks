import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { reviewCandidate, reviewPerson, syntheticCandidate } from "@/testing/review-fixture"
import type { ReviewCandidate } from "@/types/review"

import { LinkedinOption } from "./LinkedinOption"

afterEach(cleanup)

function renderOption(candidate: ReviewCandidate, person = reviewPerson(), disabled = false) {
  const onUse = vi.fn()
  const { container } = render(
    <ul>
      <LinkedinOption person={person} candidate={candidate} disabled={disabled} onUse={onUse} />
    </ul>,
  )
  const option = must(container.querySelector("li"))
  const facts = () =>
    [...option.querySelectorAll(".details > dl > div")].map((row) => [
      row.querySelector("dt")?.textContent,
      row.querySelector("dd")?.textContent,
    ])
  const copy = () =>
    [...must(option.querySelector(".profile-copy")).children].map((child) => child.textContent)
  return { option, facts, copy, onUse }
}

describe("LinkedinOption (L2)", () => {
  it("draws a fetched LinkedIn profile: the icon link in its name, headline, Work and Education", () => {
    const { option, facts, copy } = renderOption(reviewCandidate())
    expect(option.className).toBe("linkedin-option option-linkedin")
    expect(copy()).toEqual(["Jordan Bravo", "Founder at Example Labs"])
    const link = screen.getByRole("link", { name: "View LinkedIn" })
    expect(link.className).toBe("linkedin-label")
    expect(link.parentElement?.className).toBe("name-row")
    expect([link.getAttribute("href"), link.getAttribute("target"), link.getAttribute("rel")]).toEqual([
      "https://www.linkedin.com/in/jordan-bravo",
      "_blank",
      "noreferrer",
    ])
    expect(option.querySelector(".profile-copy > p")?.textContent).toBe("Founder at Example Labs")
    // Contact, Summary and Location belong to the person above, never to an option.
    expect(facts()).toEqual([
      ["Work", "Founder, Example LabsEngineer, Acme"],
      ["Education", "Example University"],
    ])
    expect(option.querySelector(".avatar img")?.getAttribute("src")).toBe(
      "https://media.example.com/jordan-bravo.jpg",
    )
  })

  it("draws a researched profile: no link, no picture, its research summary as a fact", () => {
    const { option, facts, copy } = renderOption(syntheticCandidate({ headline: "Runs Example Labs" }))
    expect(option.className).toBe("linkedin-option option-synthetic")
    expect(copy()).toEqual(["Jordan Bravo", "Researched profile — no LinkedIn confirmed"])
    expect(option.querySelector(".profile-copy > span")?.className).toBe("option-kind")
    expect(screen.queryByRole("link")).toBeNull()
    expect(facts()).toEqual([
      ["Work", "Founder, Example LabsEngineer, Acme"],
      ["Education", "Example University"],
      ["Research summary", "Runs Example Labs"],
    ])
    expect(option.querySelector(".avatar img")).toBeNull()
  })

  it("draws a LinkedIn candidate not fetched yet: a note in place of the link", () => {
    const candidate = reviewCandidate({ url: "", headline: "--", experiences: [], education: [] })
    const { option, facts, copy } = renderOption(candidate)
    expect(option.className).toBe("linkedin-option option-linkedin")
    expect(copy()).toEqual(["Jordan Bravo", "LinkedIn — profile not fetched yet"])
    expect(option.querySelector(".profile-copy > span")?.className).toBe("option-kind option-empty")
    expect(facts()).toEqual([])
  })

  it("leaves out a blank research summary", () => {
    const { facts } = renderOption(syntheticCandidate({ headline: "", experiences: [], education: [] }))
    expect(facts()).toEqual([])
  })

  it("names the option after the person when the candidate has no name, then after nobody", () => {
    const first = renderOption(reviewCandidate({ name: "" }))
    expect(first.option.querySelector("h3")?.textContent).toBe("Jordan Bravo")
    cleanup()
    const second = renderOption(reviewCandidate({ name: "" }), reviewPerson({ name: "" }))
    expect(second.option.querySelector("h3")?.textContent).toBe("This person")
  })

  it("folds a long Work list behind show more", () => {
    const experiences = ["Founder, Example Labs", "Engineer, Acme", "Intern, Acme", "Barista", "Tutor"]
    const { option } = renderOption(reviewCandidate({ experiences }))
    expect(option.querySelectorAll(".fact-list li:not([hidden])")).toHaveLength(4)
    fireEvent.click(screen.getByRole("button", { name: "+ show 2 more" }))
    expect(option.querySelectorAll(".fact-list li:not([hidden])")).toHaveLength(6)
  })

  it("hands its Use this profile press to the card, and none while a decision is saving", () => {
    const enabled = renderOption(reviewCandidate())
    const use = screen.getByRole<HTMLButtonElement>("button", { name: "Use this profile" })
    expect(use.className).toBe("button button-primary")
    expect(use.closest(".binary-actions")?.parentElement).toBe(enabled.option)
    fireEvent.click(use)
    expect(enabled.onUse).toHaveBeenCalledOnce()
    cleanup()

    const saving = renderOption(reviewCandidate(), reviewPerson(), true)
    const locked = screen.getByRole<HTMLButtonElement>("button", { name: "Use this profile" })
    expect(locked.disabled).toBe(true)
    fireEvent.click(locked)
    expect(saving.onUse).not.toHaveBeenCalled()
  })
})
