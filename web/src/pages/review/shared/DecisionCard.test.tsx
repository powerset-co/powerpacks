import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { useState } from "react"
import { afterEach, describe, expect, it } from "vitest"

import { DecisionCard } from "./DecisionCard"

afterEach(cleanup)

function Note() {
  const [text, setText] = useState("")
  return <input aria-label="note" value={text} onChange={(event) => setText(event.target.value)} />
}

describe("DecisionCard", () => {
  const card = (key: string, swapping = false) => (
    <DecisionCard cardKey={key} swapping={swapping} className="identity-card worth-card">
      <Note />
    </DecisionCard>
  )

  it("is a plain frame for the first card", () => {
    render(card("jordan"))
    expect(screen.getByRole("article").className).toBe("decision-card identity-card worth-card")
  })

  it("fades its contents while swapping and keeps them", () => {
    const { rerender } = render(card("jordan"))
    fireEvent.change(screen.getByLabelText("note"), { target: { value: "met at Acme" } })
    rerender(card("jordan", true))
    expect(screen.getByRole("article").classList.contains("swapping")).toBe(true)
    expect(screen.getByLabelText<HTMLInputElement>("note").value).toBe("met at Acme")
  })

  it("remounts the contents for the next card and has them rise in from then on", () => {
    const { rerender } = render(card("jordan"))
    const frame = screen.getByRole("article")
    fireEvent.change(screen.getByLabelText("note"), { target: { value: "met at Acme" } })
    rerender(card("casey"))
    expect(screen.getByRole("article")).toBe(frame)
    expect(frame.className).toBe("decision-card identity-card worth-card entering")
    expect(screen.getByLabelText<HTMLInputElement>("note").value).toBe("")
    rerender(card("jordan"))
    expect(frame.classList.contains("entering")).toBe(true)
  })
})
