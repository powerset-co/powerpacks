import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { DataTable } from "./DataTable"

describe("DataTable", () => {
  it("draws the head, every row of a short table, and the counts", () => {
    render(
      <DataTable
        head={["Name", "Company"]}
        rows={[
          ["Jordan Bravo", "Example Co"],
          ["Casey Delta", "Other Co"],
        ]}
      />,
    )
    expect(screen.getByText("2 rows · 2 columns")).toBeTruthy()
    expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Name", "Company"])
    expect(screen.getAllByRole("row")).toHaveLength(3)
    expect(screen.getByText("Casey Delta")).toBeTruthy()
  })
  it("renders cells through the given renderer", () => {
    render(<DataTable head={["Name"]} rows={[["x"]]} cell={(text) => <b>{text.toUpperCase()}</b>} />)
    expect(screen.getByText("X").tagName).toBe("B")
  })
})
