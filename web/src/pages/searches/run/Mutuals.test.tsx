import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { useMutuals } from "../hooks/useMutuals"
import { MutualsLine, MutualsSection } from "./Mutuals"

// The two places the drawer shows them: the line under the location, the list under the header.
function Mutuals({ linkedinUrl }: { linkedinUrl: string }) {
  const mutuals = useMutuals(linkedinUrl)
  return (
    <>
      <MutualsLine mutuals={mutuals} />
      <MutualsSection mutuals={mutuals} />
    </>
  )
}

const URL_ = "https://www.linkedin.com/in/jordan-bravo"

function answer(body: unknown, status = 200) {
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
    Promise.resolve(new Response(JSON.stringify(body), { status })),
  )
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("Mutuals", () => {
  it("asks for one person on click and lists the count and the people", async () => {
    const fetchMock = answer({
      status: "looked_up",
      count: 12,
      people: [
        {
          name: "Ann Lee",
          headline: "Engineer at Stripe",
          location: "SF",
          url: "https://www.linkedin.com/in/ann",
        },
      ],
      requests_today: 2,
      day_cap: 100,
    })
    render(<Mutuals linkedinUrl={URL_} />)
    expect(fetchMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "Get mutuals" }))
    expect(screen.getByRole("button", { name: "Getting mutuals…" })).toHaveProperty("disabled", true)
    await waitFor(() => expect(screen.getByText("12 mutual connections")).toBeTruthy())
    expect(screen.queryByRole("button")).toBeNull()
    expect(screen.getByRole("link", { name: "Ann Lee" }).getAttribute("href")).toBe(
      "https://www.linkedin.com/in/ann",
    )
    expect(screen.getByText("Engineer at Stripe")).toBeTruthy()
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({ url: URL_ }))
  })

  it("says so for a connection, a spent day and a failure", async () => {
    answer({ status: "connected", requests_today: 0, day_cap: 100 })
    render(<Mutuals linkedinUrl={URL_} />)
    fireEvent.click(screen.getByRole("button", { name: "Get mutuals" }))
    await waitFor(() => expect(screen.getByText("You're connected")).toBeTruthy())
    cleanup()

    answer({ status: "over_budget", requests_today: 100, day_cap: 100 })
    render(<Mutuals linkedinUrl={URL_} />)
    fireEvent.click(screen.getByRole("button", { name: "Get mutuals" }))
    await waitFor(() => expect(screen.getByText(/100 LinkedIn lookups are used up/)).toBeTruthy())
    cleanup()

    answer({ error: "LinkedIn is signed out" }, 409)
    render(<Mutuals linkedinUrl={URL_} />)
    fireEvent.click(screen.getByRole("button", { name: "Get mutuals" }))
    await waitFor(() => expect(screen.getByText("LinkedIn is signed out")).toBeTruthy())
  })

  it("starts over for the next person", async () => {
    answer({ status: "connected", requests_today: 0, day_cap: 100 })
    const { rerender } = render(<Mutuals linkedinUrl={URL_} />)
    fireEvent.click(screen.getByRole("button", { name: "Get mutuals" }))
    await waitFor(() => expect(screen.getByText("You're connected")).toBeTruthy())
    rerender(<Mutuals linkedinUrl="https://www.linkedin.com/in/casey-delta" />)
    expect(screen.getByRole("button", { name: "Get mutuals" })).toBeTruthy()
  })
})
