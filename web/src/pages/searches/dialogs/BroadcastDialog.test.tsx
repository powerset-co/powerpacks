import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import * as api from "@/lib/api/searches"

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
      .mockResolvedValue({ status: "uploaded", ask: { ask_id: "a1", question: "Q?", candidates: [] } })
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
    await waitFor(() => expect(onToast).toHaveBeenCalledWith({ message: "Asked 0 candidates." }))
  })
})
