import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { AgentEvent } from "@/types/agent"

import { useCodexAccount } from "./useCodexAccount"

const LOGIN = {
  loginId: "l1",
  userCode: "ABCD-EFGH",
  verificationUrl: "https://auth.openai.com/codex/device",
}

// The agent-event handler the hook registered, for the test to feed a sign-in completion.
let handler: ((event: AgentEvent) => void) | null = null

const codexApi = vi.hoisted(() => ({
  fetchCodexStatus: vi.fn(),
  startCodexLogin: vi.fn(),
  cancelCodexLogin: vi.fn(),
  logoutCodex: vi.fn(),
}))
const installApi = vi.hoisted(() => ({ focusApp: vi.fn(), openExternal: vi.fn() }))

vi.mock("@/lib/api/codex", () => ({
  ...codexApi,
  onAgentEvent: (next: (event: AgentEvent) => void) => {
    handler = next
    return () => {
      handler = null
    }
  },
}))
vi.mock("@/lib/api/install", () => installApi)

function renderAccount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return renderHook(() => useCodexAccount(), { wrapper })
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe("useCodexAccount", () => {
  it("shows the device code and opens the browser, then clears it when the sign-in lands", async () => {
    codexApi.fetchCodexStatus.mockResolvedValue({ installed: true, account: null })
    codexApi.startCodexLogin.mockResolvedValue(LOGIN)
    installApi.openExternal.mockResolvedValue(undefined)
    installApi.focusApp.mockResolvedValue(undefined)
    const { result } = renderAccount()
    await waitFor(() => expect(result.current.status).toEqual({ installed: true, account: null }))

    act(() => result.current.connect())
    await waitFor(() => expect(result.current.login).toEqual(LOGIN))
    expect(installApi.openExternal).toHaveBeenCalledWith(LOGIN.verificationUrl)

    codexApi.fetchCodexStatus.mockResolvedValue({
      installed: true,
      account: { kind: "chatgpt", email: "casey@example.com", plan: "plus" },
    })
    act(() => handler?.({ type: "loginCompleted", success: true, error: null }))
    expect(result.current.login).toBeNull()
    expect(result.current.error).toBeNull()
    expect(installApi.focusApp).toHaveBeenCalled()
    await waitFor(() =>
      expect(result.current.status?.installed && result.current.status.account).toBeTruthy(),
    )
  })

  it("cancel ends the sign-in with Codex and drops the code", async () => {
    codexApi.fetchCodexStatus.mockResolvedValue({ installed: true, account: null })
    codexApi.startCodexLogin.mockResolvedValue(LOGIN)
    codexApi.cancelCodexLogin.mockResolvedValue(undefined)
    installApi.openExternal.mockResolvedValue(undefined)
    const { result } = renderAccount()
    act(() => result.current.connect())
    await waitFor(() => expect(result.current.login).toEqual(LOGIN))

    act(() => result.current.cancel())
    expect(result.current.login).toBeNull()
    expect(codexApi.cancelCodexLogin).toHaveBeenCalledWith("l1")
  })

  it("reports a failed completion and a refused browser", async () => {
    codexApi.fetchCodexStatus.mockResolvedValue({ installed: true, account: null })
    codexApi.startCodexLogin.mockResolvedValue(LOGIN)
    installApi.openExternal.mockRejectedValue(new Error("No browser."))
    installApi.focusApp.mockResolvedValue(undefined)
    const { result } = renderAccount()
    act(() => result.current.connect())
    await waitFor(() => expect(result.current.error).toBe("No browser."))
    expect(result.current.login).toEqual(LOGIN)

    act(() => handler?.({ type: "loginCompleted", success: false, error: "Code expired." }))
    expect(result.current.login).toBeNull()
    expect(result.current.error).toBe("Code expired.")
  })
})
