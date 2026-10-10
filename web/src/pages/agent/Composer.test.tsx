import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { Composer } from "./Composer"

afterEach(cleanup)

it("retains a draft when sending fails and clears it only after success", async () => {
  const send = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
  const saveDraft = vi.fn()
  render(
    <Composer
      running={false}
      onSend={send}
      footer="Visible to set"
      initialText="Keep this draft"
      onDraftChange={saveDraft}
    />,
  )
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
  expect(screen.getByRole("textbox").getAttribute("placeholder")).toBe("Ask about anyone in your network…")
  expect(screen.getByDisplayValue("Keep this draft")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Send" }))
  await waitFor(() => expect(saveDraft).toHaveBeenCalledWith(""))
  expect(screen.queryByDisplayValue("Keep this draft")).toBeNull()
})

it("does not send while a delivery is pending", () => {
  const send = vi.fn()
  render(<Composer running={false} disabled onSend={send} footer="Visible to set" initialText="Wait" />)
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" })
  expect(send).not.toHaveBeenCalled()
})
