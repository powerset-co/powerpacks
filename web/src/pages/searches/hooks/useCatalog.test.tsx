import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, renderHook } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { CARDS } from "@/testing/searches-fixture"

import { useCatalog } from "./useCatalog"

let fetchMock = vi.fn()

beforeEach(() => {
  vi.useFakeTimers()
  fetchMock = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ searches: CARDS }))))
  vi.stubGlobal("fetch", fetchMock)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function renderCatalog(agentRunning: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return renderHook(({ running }: { running: boolean }) => useCatalog(running), {
    wrapper,
    initialProps: { running: agentRunning },
  })
}

describe("useCatalog", () => {
  it("asks again every few seconds while the agent runs", async () => {
    renderCatalog(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(3_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(3_000)
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it("asks once while the agent is idle", async () => {
    renderCatalog(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("stops asking when the agent stops", async () => {
    const { rerender } = renderCatalog(true)
    await vi.advanceTimersByTimeAsync(3_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    rerender({ running: false })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
