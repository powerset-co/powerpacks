import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import * as auth from "@/lib/api/feedback"
import * as api from "@/lib/api/searches"
import { SetsError } from "@/lib/api/sets"

import { BroadcastDialog } from "./BroadcastDialog"

describe("BroadcastDialog", () => {
  it("shows the pinned candidates, who gets them and how many, then sends", async () => {
    vi.spyOn(api, "fetchAskPreview").mockResolvedValue({
      pinned: [
        {
          public_identifier: "jordan-bravo-1a2b",
          linkedin_url: "https://linkedin.com/in/jordan-bravo-1a2b",
          name: "Jordan Bravo",
          local_rank: 1,
        },
      ],
      skipped: 1,
      candidates: [
        {
          public_identifier: "jordan-bravo-1a2b",
          name: "Jordan Bravo",
          owners: [{ operator_id: "op-1", name: "Casey Delta" }],
        },
      ],
      operators: [{ operator_id: "op-1", name: "Casey Delta", candidates: 1 }],
    })
    vi.spyOn(api, "fetchAskStatus").mockResolvedValue({ ask: null })
    const send = vi
      .spyOn(api, "sendAsk")
      .mockResolvedValue({ status: "sent", ask: { ask_id: "a1", question: "Q?", candidates: [] } })
    const onToast = vi.fn()

    render(<BroadcastDialog runId="run-1" title="Backend Engineer" pinned={2} onToast={onToast} />)
    fireEvent.click(screen.getByRole("button", { name: "Ask the set about Backend Engineer" }))

    await waitFor(() => expect(screen.getAllByText("Casey Delta")).toHaveLength(2))
    expect(screen.getByText("1 contact")).toBeTruthy()
    expect(screen.getByText("1 pinned without a LinkedIn URL, not sent.")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "Send" }))
    await waitFor(() =>
      expect(send).toHaveBeenCalledWith("run-1", "Would you recommend them, and would you intro?"),
    )
    await waitFor(() => expect(onToast).toHaveBeenCalledWith({ message: "Asked about 0 candidates." }))
  })

  it("offers the Powerset sign-in when the ask needs one, then loads the preview", async () => {
    const preview = vi
      .spyOn(api, "fetchAskPreview")
      .mockRejectedValueOnce(new SetsError("needs_auth", true))
      .mockResolvedValue({ pinned: [], skipped: 0, candidates: [], operators: [] })
    vi.spyOn(api, "fetchAskStatus").mockResolvedValue({ ask: null })
    const signIn = vi.spyOn(auth, "signIn").mockResolvedValue(undefined)

    render(<BroadcastDialog runId="run-1" title="Backend Engineer" pinned={1} onToast={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "Ask the set about Backend Engineer" }))

    await waitFor(() => expect(screen.getByText("Sign in to Powerset to ask the set.")).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Powerset" }))
    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText("Nothing pinned with a LinkedIn URL.")).toBeTruthy())
    expect(preview).toHaveBeenCalledTimes(2)
    expect(screen.queryByText("Sign in to Powerset to ask the set.")).toBeNull()
  })
})
