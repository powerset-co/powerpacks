import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { FactList } from "./FactList"

afterEach(cleanup)

const shownItems = () => screen.getAllByRole("listitem").map((item) => item.textContent)

describe("FactList", () => {
  it("shows every entry of a short list and no toggle", () => {
    render(<FactList items={["Founder, Example Labs", "Engineer, Acme", "Intern, Initech"]} />)
    expect(shownItems()).toEqual(["Founder, Example Labs", "Engineer, Acme", "Intern, Initech"])
    expect(screen.queryByRole("button")).toBeNull()
  })

  it("shows three, then the rest behind show more, and folds back", () => {
    render(<FactList items={["a", "b", "c", "d", "e"]} />)
    expect(shownItems()).toEqual(["a", "b", "c"])
    fireEvent.click(screen.getByRole("button", { name: "+ show 2 more" }))
    expect(shownItems()).toEqual(["a", "b", "c", "d", "e"])
    fireEvent.click(screen.getByRole("button", { name: "show fewer" }))
    expect(shownItems()).toEqual(["a", "b", "c"])
    expect(screen.getByRole("button", { name: "+ show 2 more" })).toBeTruthy()
  })

  it("drops blank entries before counting", () => {
    render(<FactList items={["a", " ", "b", "c", "", "d"]} />)
    expect(shownItems()).toEqual(["a", "b", "c"])
    expect(screen.getByRole("button").textContent).toBe("+ show 1 more")
  })
})
