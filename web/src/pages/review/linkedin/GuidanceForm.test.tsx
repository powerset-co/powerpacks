import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { createRef } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { ReviewHarness } from "@/testing/review-harness"

import { GuidanceForm } from "./GuidanceForm"
import { spyReview } from "./linkedin-fixture"

afterEach(cleanup)

interface Flags {
  disabled?: boolean
}

function renderForm({ disabled = false }: Flags = {}) {
  const review = spyReview()
  const onFix = vi.fn()
  const onRetarget = vi.fn()
  const { container } = render(
    <ReviewHarness review={review}>
      <GuidanceForm
        open
        onToggle={vi.fn()}
        field={createRef<HTMLTextAreaElement>()}
        disabled={disabled}
        onFix={onFix}
        onRetarget={onRetarget}
      />
    </ReviewHarness>,
  )
  const submit = (text: string) => {
    fireEvent.change(must(container.querySelector("textarea")), { target: { value: text } })
    fireEvent.click(screen.getByRole("button", { name: "Retarget" }))
  }
  return { container, review, onFix, onRetarget, submit }
}

describe("GuidanceForm: one box, two routes (P0.1)", () => {
  it.each([
    ["https://www.linkedin.com/in/jordan-bravo-2", "https://www.linkedin.com/in/jordan-bravo-2"],
    ["linkedin.com/in/jordan.bravo_2", "linkedin.com/in/jordan.bravo_2"],
    ["HTTP://UK.LINKEDIN.COM/IN/JORDAN", "HTTP://UK.LINKEDIN.COM/IN/JORDAN"],
    ["wrong one, use www.linkedin.com/in/jordan-b/ please", "www.linkedin.com/in/jordan-b"],
    ["  https://linkedin.com/in/jordan-b?trk=x  ", "https://linkedin.com/in/jordan-b"],
  ])("applies the profile URL in %j through the free route, alone", (text, url) => {
    const { onFix, onRetarget, submit } = renderForm()
    submit(text)
    expect(onFix).toHaveBeenCalledExactlyOnceWith(url)
    expect(onRetarget).not.toHaveBeenCalled()
  })

  it.each([
    ["the founder of Example Labs", "the founder of Example Labs"],
    ["  works at Acme, lives in Springfield \n", "works at Acme, lives in Springfield"],
    ["see linkedin.com/company/example-labs", "see linkedin.com/company/example-labs"],
    ["their handle is jordan-bravo on LinkedIn", "their handle is jordan-bravo on LinkedIn"],
  ])("sends %j to the paid re-research, trimmed", (text, guidance) => {
    const { onFix, onRetarget, submit } = renderForm()
    submit(text)
    expect(onRetarget).toHaveBeenCalledExactlyOnceWith(guidance)
    expect(onFix).not.toHaveBeenCalled()
  })

  it.each(["", "   ", "\n\t"])("sends nothing for %j", (text) => {
    const { container, onFix, onRetarget, submit } = renderForm()
    submit(text)
    fireEvent.submit(must(container.querySelector("form")))
    expect(onFix).not.toHaveBeenCalled()
    expect(onRetarget).not.toHaveBeenCalled()
  })

  it("takes no press while Retarget is off", () => {
    const { onFix, onRetarget, submit } = renderForm({ disabled: true })
    submit("the founder of Example Labs")
    submit("linkedin.com/in/jordan-b")
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Retarget" }).disabled).toBe(true)
    expect(onRetarget).not.toHaveBeenCalled()
    expect(onFix).not.toHaveBeenCalled()
  })
})

describe("GuidanceForm", () => {
  it("draws the box open, with its summary, its field and Retarget", () => {
    const { container } = renderForm()
    const box = must(container.querySelector("details"))
    expect(box.className).toBe("retarget-guidance")
    expect(box.open).toBe(true)
    expect(box.querySelector("summary")?.textContent).toBe("Wrong person? Provide LinkedIn or re-research")
    expect(box.querySelector("form")?.className).toBe("retarget-form")
    const field = must(box.querySelector("textarea"))
    expect([field.name, field.maxLength, field.required]).toEqual(["guidance", 2000, true])
    const submit = screen.getByRole<HTMLButtonElement>("button", { name: "Retarget" })
    expect([submit.className, submit.type]).toEqual(["button button-primary", "submit"])
  })
})
