import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { AgentEvent, CodexStatus } from "@/types/agent"

import { CodexAccountProvider } from "./CodexAccountProvider"
import { useCodexAccount } from "./useCodexAccount"

const LOGIN = {
  loginId: "l1",
  authUrl: "https://auth.openai.com/oauth/authorize?client_id=x",
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
    <QueryClientProvider client={client}>
      <CodexAccountProvider>{children}</CodexAccountProvider>
    </QueryClientProvider>
  )
  return renderHook(() => useCodexAccount(), { wrapper })
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.useRealTimers()
})

describe("useCodexAccount", () => {
  it("keeps a pending login across pages and receives completion with no account page open", async () => {
    codexApi.fetchCodexStatus.mockResolvedValue({ installed: true, account: null })
    codexApi.startCodexLogin.mockResolvedValue(LOGIN)
    installApi.openExternal.mockResolvedValue(undefined)
    installApi.focusApp.mockResolvedValue(undefined)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    function Account() {
      const account = useCodexAccount()
      return <button onClick={account.connect}>{account.login?.loginId ?? "Connect"}</button>
    }
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <CodexAccountProvider>{children}</CodexAccountProvider>
      </QueryClientProvider>
    )
    const view = render(<Account />, { wrapper })
    fireEvent.click(screen.getByRole("button", { name: "Connect" }))
    await screen.findByRole("button", { name: "l1" })
    view.rerender(<div>People</div>)
    view.rerender(<Account />)
    expect(screen.getByRole("button", { name: "l1" })).toBeTruthy()
    view.rerender(<div>People</div>)
    act(() => handler?.({ type: "loginCompleted", success: true, error: null }))
    expect(installApi.focusApp).toHaveBeenCalledTimes(1)
    view.rerender(<Account />)
    expect(await screen.findByRole("button", { name: "Connect" })).toBeTruthy()
    expect(codexApi.startCodexLogin).toHaveBeenCalledTimes(1)
  })

  it("opens the sign-in page in the browser, then clears it when the sign-in lands", async () => {
    codexApi.fetchCodexStatus.mockResolvedValue({ installed: true, account: null })
    codexApi.startCodexLogin.mockResolvedValue(LOGIN)
    installApi.openExternal.mockResolvedValue(undefined)
    installApi.focusApp.mockResolvedValue(undefined)
    const { result } = renderAccount()
    await waitFor(() => expect(result.current.status).toEqual({ installed: true, account: null }))

    act(() => result.current.connect())
    await waitFor(() => expect(result.current.login).toEqual(LOGIN))
    expect(installApi.openExternal).toHaveBeenCalledWith(LOGIN.authUrl)

    // Codex's account is read again after the sign-in lands; until it answers, the sign-in holds.
    let answer!: (status: CodexStatus) => void
    codexApi.fetchCodexStatus.mockReturnValue(
      new Promise<CodexStatus>((resolve) => {
        answer = resolve
      }),
    )
    act(() => handler?.({ type: "loginCompleted", success: true, error: null }))
    expect(result.current.login).toEqual(LOGIN)
    expect(result.current.error).toBeNull()
    expect(installApi.focusApp).toHaveBeenCalled()
    act(() =>
      answer({ installed: true, account: { kind: "chatgpt", email: "casey@example.com", plan: "plus" } }),
    )
    await waitFor(() => expect(result.current.login).toBeNull())
    await waitFor(() =>
      expect(result.current.status?.installed && result.current.status.account).toBeTruthy(),
    )
  })

  it("cancel ends the sign-in with Codex and drops it", async () => {
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

  it("ends a sign-in nobody finishes after two minutes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    codexApi.fetchCodexStatus.mockResolvedValue({ installed: true, account: null })
    codexApi.startCodexLogin.mockResolvedValue(LOGIN)
    codexApi.cancelCodexLogin.mockResolvedValue(undefined)
    installApi.openExternal.mockResolvedValue(undefined)
    const { result } = renderAccount()
    act(() => result.current.connect())
    await waitFor(() => expect(result.current.login).toEqual(LOGIN))

    act(() => {
      vi.advanceTimersByTime(119_000)
    })
    expect(codexApi.cancelCodexLogin).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(1_000)
    })
    expect(codexApi.cancelCodexLogin).toHaveBeenCalledWith("l1")
    expect(result.current.login).toBeNull()
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
    expect(result.current.error).toBe("Code expired.")
    await waitFor(() => expect(result.current.login).toBeNull())
  })
})
